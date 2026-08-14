"""Every registry claim, held against an INDEPENDENT executable authority.

This file exists because a shared registry is not evidence (re-audit `82636da..9ac574f`, systemic
prevention requirement). If the generator and the test both read the same wrong value they agree
with each other and stay wrong. So nothing here compares the registry to itself: each literal
written in `docs/contracts/` is checked against the thing that actually governs it — a Pydantic
model, a Settings default, a runtime function, a route table, a shipped canonical record, or, for
the blocker, by executing the factory and observing that it refuses.

THE MAP IS CLOSED (re-audit `6feca36..4f23f23` F4). `AUTHORITY_VERIFIERS` holds exactly one
verifier per claim id, and `test_the_verifier_map_is_closed_over_both_registries` asserts its keys
equal the registries' ids exactly. Adding a claim without a verifier fails; deleting a claim while
leaving its verifier behind fails. The previous arrangement was a pile of independently named
tests, which meant an unverified claim was invisible — it simply had no test, and nothing said so.

`test_contract_rendering.py` is the other half: it proves the rendered PDF displays these values.
"""

import ast
import dataclasses
import hashlib
import math
import re
import time
from pathlib import Path
from unittest import mock

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from docs.contracts import ClaimState, playbook
from docs.contracts.operations import OPERATIONS, Procedure
from docs.contracts.signing_example import sign as example_sign
from docs.contracts.wire import WIRE
from pydantic import ValidationError as PydanticValidationError

from kyc_tool.api import schemas
from kyc_tool.api.schemas import (
    PAYLOAD_MODELS,
    DecisionCallback,
    EventEnvelope,
    EventType,
    GatesBody,
)
from kyc_tool.config import (
    STUB_ADAPTERS_PROFILE,
    STUB_EMAIL_PROVIDER,
    STUB_OCR_ENGINE,
    ProcessRole,
    ProductionConfigError,
    Settings,
    hmac_extra_key_violations,
    production_config_violations,
    validate_process_role,
)
from kyc_tool.ops import cutover
from kyc_tool.outbox import publisher
from kyc_tool.queue.backoff import saturating_backoff_seconds
from kyc_tool.security import (
    DIRECTION_INBOUND,
    DIRECTION_OUTBOUND,
    MAX_HMAC_SKEW_SECONDS,
    canonical_v2,
    sign_v2,
)

from .test_production_config import hardened

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "kyc_tool"


# ── the closed map ────────────────────────────────────────────────────────────────────────────────
AUTHORITY_VERIFIERS: dict[str, object] = {}


def verifies(claim_id: str):
    """Register the ONE verifier for `claim_id`.

    One id per function, deliberately. A verifier shared across several claims can check one of
    them and still look like coverage for the rest — the same "agrees with itself" failure this
    file exists to prevent, moved up a level.
    """

    def register(fn):
        if claim_id in AUTHORITY_VERIFIERS:
            raise AssertionError(f"{claim_id} already has a verifier: {AUTHORITY_VERIFIERS[claim_id]}")
        AUTHORITY_VERIFIERS[claim_id] = fn
        return fn

    return register


def _declared_routes() -> set[tuple[str, str]]:
    """(method, path) for every route the app declares, WITHOUT booting the app.

    `create_app()` opens a database connection, which a unit test must not require. The routers are
    importable objects, and the two probes are registered by decorator inside the factory, so those
    are read structurally from the AST — a decorator literal, not a substring of the file.
    """
    from kyc_tool.api import routes_events, routes_metrics, routes_ops, routes_read

    found = set()
    for module in (routes_events, routes_metrics, routes_ops, routes_read):
        for route in module.router.routes:
            for method in route.methods:
                found.add((method, route.path))

    tree = ast.parse((SRC / "api" / "app.py").read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            call = decorator if isinstance(decorator, ast.Call) else None
            if call is None or not isinstance(call.func, ast.Attribute):
                continue
            target = call.func.value
            if isinstance(target, ast.Name) and target.id == "app" and call.args:
                literal = call.args[0]
                if isinstance(literal, ast.Constant) and isinstance(literal.value, str):
                    found.add((call.func.attr.upper(), literal.value))
    return found


# ── signing ───────────────────────────────────────────────────────────────────────────────────────
def _vector_fields(vector: dict) -> dict:
    return {
        "key_id": vector["key_id"],
        "direction": vector["direction"],
        "method": vector["method"],
        "path_qs": vector["path_qs"],
        "timestamp": vector["timestamp"],
        "slot": vector["slot"],
        "body": vector["body"],
    }


@verifies("WIRE.SIGN.COMPANION")
def _the_companion_file_is_the_bytes_the_document_names():
    """Execute the SHIPPED FILE, not the module it was generated from.

    Re-audit `4f23f23..122cc67` finding 4. The document used to promise the printed block was
    copyable; the test that "proved" it reconstructed indentation from glyph x-offsets and Courier
    advance widths, which is a decoder no reader has. Exact `pdftotext` output does not compile.
    So the document now names a file and its digest, and this executes exactly those bytes.
    """
    from docs.contracts.companion import (
        ARTIFACT_NAME,
        artifact_digest,
        artifact_path,
        artifact_text,
    )

    path = artifact_path()
    assert path.exists(), f"{ARTIFACT_NAME} is not in the repo; run docs.contracts.companion"
    on_disk = path.read_text()
    assert on_disk == artifact_text(), (
        f"{ARTIFACT_NAME} has drifted from the snippet it is generated from; regenerate it"
    )

    # the digest the document prints is over the RUNNABLE BODY, and it is correct
    body = on_disk.split('"""', 2)[2].lstrip("\n")
    assert hashlib.sha256(body.encode()).hexdigest() == artifact_digest()
    assert artifact_digest() in on_disk, "the file does not state its own digest"

    # and the file runs and reproduces the published vector
    namespace: dict = {}
    exec(compile(on_disk, str(path), "exec"), namespace)  # noqa: S102
    v = WIRE.value("WIRE.SIGN.VECTOR")
    assert namespace["sign"](v["secret"], **_vector_fields(v)) == v["signature"]

    claim = WIRE.value("WIRE.SIGN.COMPANION")
    assert ARTIFACT_NAME in claim
    assert "shasum -a 256" in claim
    assert "not supported" in claim, "the document still promises copying off the page"


@verifies("WIRE.SIGN.VECTOR")
def _vector_is_bound_end_to_end():
    """Body, byte count, body hash, ordered canonical block, signature, and the published snippet
    all recomputed from kyc_tool.security. Change any one part and this fails, which is what
    'bound' means: an earlier version checked the digest and the hash independently, so an altered
    body carrying a stale hash passed."""
    v = WIRE.value("WIRE.SIGN.VECTOR")
    fields = _vector_fields(v)

    assert len(v["body"]) == v["body_bytes"]
    assert hashlib.sha256(v["body"]).hexdigest() == v["body_sha256"]

    canonical = canonical_v2(**fields)
    assert tuple(canonical.split("\n")) == v["canonical_lines"], "canonical block drifted or reordered"
    assert canonical.split("\n")[-1] == v["body_sha256"], "the canonical tail is not this body's hash"

    assert sign_v2(v["secret"], **fields) == v["signature"]
    # the snippet TechCraft copies, executed against the shipped signer
    assert example_sign(v["secret"], **fields) == v["signature"]


@verifies("WIRE.SIGN.DIRECTIONS")
def _direction_tokens_match_the_signer_literals():
    directions = WIRE.value("WIRE.SIGN.DIRECTIONS")
    assert directions["platform_to_tool"] == DIRECTION_INBOUND
    assert directions["tool_to_platform"] == DIRECTION_OUTBOUND
    assert WIRE.value("WIRE.SIGN.VECTOR")["direction"] == DIRECTION_INBOUND


@verifies("WIRE.SIGN.CANONICAL")
def _canonical_field_order_matches_the_signer():
    """The documented field names, in order, against a canonical string built from placeholders."""
    documented = WIRE.value("WIRE.SIGN.CANONICAL")
    probe = canonical_v2(
        key_id="K",
        direction=DIRECTION_INBOUND,
        method="M",
        path_qs="P",
        timestamp="T",
        slot="S",
        body=b"",
    ).split("\n")
    # The published NAMES, exactly and in order. Checking only the count and the literal "v2"
    # let `key_id` be renamed to `NOT_KEY_ID` on the page (re-audit `4f23f23..97deeae` F3): an
    # integrator building the canonical string from the document would sign a different message
    # from the one we verify, and every request would 401 with no way to see why.
    assert tuple(documented) == (
        "v2", "key_id", "direction", "method", "path?query", "timestamp", "slot",
        "sha256(body) hex",
    ), "the published canonical field names changed"
    assert len(documented) == len(probe) == 8
    assert documented[0] == probe[0] == "v2"
    for position, placeholder in ((1, "K"), (2, DIRECTION_INBOUND), (3, "M"), (4, "P"), (5, "T"), (6, "S")):
        assert probe[position] == placeholder, f"canonical position {position} moved"
    assert probe[7] == hashlib.sha256(b"").hexdigest()


@verifies("WIRE.SIGN.SKEW_SECONDS")
def _skew_window_matches_the_verifier():
    assert WIRE.value("WIRE.SIGN.SKEW_SECONDS") == MAX_HMAC_SKEW_SECONDS


@verifies("WIRE.SIGN.ROTATION")
def _rotation_publishes_an_executable_procedure_for_each_direction():
    """Both directions, both orders, and the reason each order is what it is.

    Re-audit `4f23f23..122cc67` finding 9. The previous version named the asymmetry but was not
    executable: inbound said "add a second key id, cut over, then retire the old one" and omitted
    the PROMOTION — a literal reading removes the entry while the old key is still ACTIVE, which
    leaves no active key at all. Outbound told TechCraft to retire once callbacks appear under the
    new key, which in a mixed fleet does not prove an old-key signer is gone.
    """
    lines = WIRE.value("WIRE.SIGN.ROTATION")
    inbound = next(line for line in lines if line.startswith("INBOUND"))
    outbound = next(line for line in lines if line.startswith("OUTBOUND"))

    # inbound: the overlap map exists and enforces the active key's floor
    assert hmac_extra_key_violations({"old": "s" * 32}, "active") == []
    assert any("weak" in v for v in hmac_extra_key_violations({"old": "s" * 31}, "active"))
    assert any("blank" in v for v in hmac_extra_key_violations({"": "s" * 32}, "active"))
    assert any("collides" in v for v in hmac_extra_key_violations({"active": "s" * 32}, "active"))

    # ...and the published order includes the promotion, in the right place
    assert "PROMOTE" in inbound, "the inbound procedure still omits promoting the new key"
    assert inbound.index("PROMOTE") < inbound.index("remove the old"), (
        "the old entry is removed before the new key is promoted"
    )
    assert any("no active key" in line for line in lines), (
        "nothing explains why deleting before promoting is unsafe"
    )

    # outbound: exactly ONE signer, so the overlap cannot live here and the drain is required
    publisher = (SRC / "outbox" / "publisher.py").read_text()
    assert publisher.count("hmac_outbound_secret") == 1, (
        "the publisher now references more than one outbound secret; if it can hold an overlap, "
        "the drained procedure below is no longer the only safe one"
    )
    assert "hmac_outbound_extra_keys" not in set(Settings.model_fields)
    assert "OVERLAP IS YOURS TO HOLD" in outbound
    assert outbound.index("You start accepting") < outbound.index("attest ZERO publishers")
    assert outbound.index("attest ZERO publishers") < outbound.index("deploy the sole new signer")
    assert outbound.index("deploy the sole new signer") < outbound.index("retire the old key")
    assert any("mixed fleet" in line and "dead-letter" in line for line in lines), (
        "nothing explains why one new-key callback is not evidence"
    )

    # the drain is a real, named capability, not an aspiration
    from kyc_tool.ops import cutover

    assert hasattr(cutover, "OUTBOX_MAX_ATTEMPTS_CUTOVER")
    assert "attest zero publishers" in " ".join(
        OPERATIONS.value("OPS.CUTOVER.OUTBOX_CEILING")).lower()


@verifies("WIRE.SIGN.V1_SUNSET")
def _v1_sunset_dates_are_unset_in_our_config():
    """The document once published 2026-09-01 / 2026-10-01 as commitments. Those dates came from
    the `hardened()` TEST FIXTURE; the shipped config leaves both unset. Assert the shipped file,
    so republishing a fixture value fails here."""
    env = (REPO / ".env.example").read_text()
    for variable in ("KYC_HMAC_V1_INBOUND_SUNSET_AT", "KYC_HMAC_V1_OUTBOUND_SUNSET_AT"):
        line = next(line for line in env.splitlines() if line.startswith(variable + "="))
        value = line.split("=", 1)[1].split("#")[0].strip()
        assert value == "", f"{variable} now ships a value; the claim says both are unset"
    claim = WIRE.value("WIRE.SIGN.V1_SUNSET")
    assert "unset in our config" in claim
    assert not re.search(r"20\d\d-\d\d-\d\d", claim), "the claim is publishing a concrete date again"
    assert "observation window" in claim


# ── ingestion ─────────────────────────────────────────────────────────────────────────────────────
@verifies("WIRE.INGEST.PATH")
def _ingest_path_is_a_declared_route():
    method, _, path = WIRE.value("WIRE.INGEST.PATH").partition(" ")
    assert (method, path) in _declared_routes(), "the documented ingest route is not registered"


@verifies("WIRE.INGEST.HEADERS")
def _documented_headers_are_the_ones_the_verifier_reads():
    """Signs a request using EXACTLY the four documented header names and asserts it verifies —
    then renames each in turn and asserts a 401. A name-presence check against the source would
    pass on a header the verifier no longer reads; this fails unless each name is load-bearing."""
    from fastapi import HTTPException

    from kyc_tool.api import auth

    documented = WIRE.value("WIRE.INGEST.HEADERS")
    assert len(documented) == len(set(documented)) == 4
    idempotency, timestamp_header, key_id_header, signature_header = documented

    settings = hardened()
    path_qs = "/v1/cases/c/events"
    body = b'{"probe":1}'
    slot = "slot-1"
    stamp = str(int(time.time()))
    signature = sign_v2(
        settings.hmac_inbound_secret,
        key_id=settings.hmac_inbound_key_id,
        direction=DIRECTION_INBOUND,
        method="POST",
        path_qs=path_qs,
        timestamp=stamp,
        slot=slot,
        body=body,
    )
    good = {
        idempotency: slot,
        timestamp_header: stamp,
        key_id_header: settings.hmac_inbound_key_id,
        signature_header: signature,
    }

    def _request(headers):
        class _App:
            class state:
                session_factory = None

        class _Req:
            method = "POST"
            app = _App()
            scope = {"path": path_qs, "query_string": b"", "raw_path": path_qs.encode()}

            class url:
                path = path_qs
                query = ""

        request = _Req()
        request.headers = headers
        return request

    auth.require_valid_signature(settings, _request(good), body)  # must not raise

    for name in documented:
        renamed = {("X-Wrong-" + k if k == name else k): value for k, value in good.items()}
        with pytest.raises(HTTPException) as excinfo:
            auth.require_valid_signature(settings, _request(renamed), body)
        assert excinfo.value.status_code == 401, f"renaming {name} did not change the outcome"


@verifies("WIRE.INGEST.STATUS")
def _documented_status_codes_are_the_ones_ingest_returns():
    """202 for a queued event and 200 for replay-or-inline are the pair most likely to be
    misbuilt, so they are asserted against the source of the outcomes themselves."""
    documented = dict(WIRE.value("WIRE.INGEST.STATUS"))
    source = (SRC / "events" / "ingest.py").read_text()
    assert 202 in documented and 200 in documented
    assert "IngestOutcome(202" in source, "ingest no longer returns 202 for a queued event"
    assert "IngestOutcome(200" in source
    assert "queued" in documented[202]
    assert "replay" in documented[200].lower() and "inline" in documented[200].lower()
    for code in (400, 401, 409, 422, 404):
        assert code in documented


@verifies("WIRE.INGEST.EXTRA_FIELDS")
def _extra_field_policy_matches_the_models():
    claim = WIRE.value("WIRE.INGEST.EXTRA_FIELDS")
    assert EventEnvelope.model_config.get("extra") == claim["envelope"] == "forbid"
    for model in PAYLOAD_MODELS.values():
        assert model.model_config.get("extra") == claim["payload"] == "allow"


@verifies("WIRE.INGEST.ORDERING")
def _ingest_ordering_is_the_case_lock_it_claims():
    """The claim's whole point is that same-case order is lock-acquisition order, not send order.
    Its authority is the case-level FOR UPDATE admission in ingest; if that lock goes, the claim
    is describing a guarantee nothing provides."""
    source = (SRC / "events" / "ingest.py").read_text()
    assert "with_for_update=True" in source, "ingest no longer takes the case lock to admit events"
    assert "Case" in source
    claim = WIRE.value("WIRE.INGEST.ORDERING").lower()
    assert "lock-acquisition order" in claim
    assert "need not match your send order" in claim


@verifies("WIRE.EVENT.TABLE")
def _event_table_matches_the_payload_models_exactly():
    """Names AND field lists, both directions. An earlier guard only checked that accepted names
    appeared somewhere in the prose, so a documented-but-rejected event, or a required field that
    quietly moved, both passed."""
    table = {row[0]: row for row in WIRE.value("WIRE.EVENT.TABLE")}
    assert set(table) == set(EventType.__args__) == set(PAYLOAD_MODELS)

    for event_type, model in PAYLOAD_MODELS.items():
        required = tuple(sorted(f for f, i in model.model_fields.items() if i.is_required()))
        optional = tuple(sorted(f for f, i in model.model_fields.items() if not i.is_required()))
        _, documented_required, documented_optional, _ = table[event_type]
        assert tuple(sorted(documented_required)) == required, f"{event_type} required fields"
        assert tuple(sorted(documented_optional)) == optional, f"{event_type} optional fields"


@verifies("WIRE.ACTOR.SENSITIVE")
def _sensitive_actor_rule_matches_the_guard():
    from kyc_tool.events.review_guard import reviewer_actor_reason

    claim = WIRE.value("WIRE.ACTOR.SENSITIVE")
    assert set(claim["events"]) == {"website.review_completed", "reviewer.manual_approve"}
    ok = {"type": claim["actor_type"], "id": "rev-1"}
    assert reviewer_actor_reason(ok, {"reviewer_id": "rev-1"}) is None
    # every failure the prose promises
    assert reviewer_actor_reason({"type": "user", "id": "rev-1"}, {"reviewer_id": "rev-1"})
    assert reviewer_actor_reason(ok, {"reviewer_id": "REV-1"})  # case-sensitive
    assert reviewer_actor_reason({"type": "reviewer", "id": "  "}, {"reviewer_id": "  "})
    assert reviewer_actor_reason(ok, {"reviewer_id": "other"})


# ── callbacks ─────────────────────────────────────────────────────────────────────────────────────
@verifies("WIRE.CALLBACK.PATH")
def _callback_path_is_the_suffix_the_publisher_appends():
    documented = WIRE.value("WIRE.CALLBACK.PATH")
    method, _, target = documented.partition(" ")
    assert method == "POST"
    suffix = target.split(">", 1)[1]
    publisher = (SRC / "outbox" / "publisher.py").read_text()
    assert f'/{suffix.lstrip("/")}"' in publisher.replace("'", '"'), (
        "the publisher no longer appends the documented suffix"
    )
    # the suffix is why a base URL carrying a query or fragment is refused at boot
    assert production_config_violations(hardened(platform_callback_url="https://platform.example/kyc?x=1"))


@verifies("WIRE.CALLBACK.FIELDS")
def _callback_required_fields_match_the_wire_model():
    required = {f for f, i in DecisionCallback.model_fields.items() if i.is_required()}
    assert set(WIRE.value("WIRE.CALLBACK.FIELDS")) == required


@verifies("WIRE.CALLBACK.OPTIONAL_FIELDS")
def _callback_optional_fields_match_the_wire_model():
    optional = {f for f, i in DecisionCallback.model_fields.items() if not i.is_required()}
    assert set(WIRE.value("WIRE.CALLBACK.OPTIONAL_FIELDS")) == optional


@verifies("WIRE.CALLBACK.GATES")
def _callback_gates_match_the_gates_model():
    assert set(WIRE.value("WIRE.CALLBACK.GATES")) == set(GatesBody.model_fields)


@verifies("WIRE.CALLBACK.DECISIONS")
def _documented_decisions_are_the_wire_literal():
    documented = set(WIRE.value("WIRE.CALLBACK.DECISIONS"))
    annotation = DecisionCallback.model_fields["decision"].annotation
    assert documented == set(annotation.__args__)


@verifies("WIRE.CALLBACK.RETRY")
def _retry_schedule_recomputes_from_the_shipped_backoff():
    claim = WIRE.value("WIRE.CALLBACK.RETRY")
    settings = Settings()
    assert claim["attempts"] == settings.outbox_max_attempts
    assert claim["base_seconds"] == settings.outbox_backoff_base_seconds
    delays = tuple(
        int(saturating_backoff_seconds(settings.outbox_backoff_base_seconds, n))
        for n in range(1, settings.outbox_max_attempts)
    )
    assert claim["delays"] == delays
    assert claim["backoff_total_seconds"] == sum(delays)
    walls = settings.outbox_max_attempts * 4 * settings.outbox_http_timeout_seconds
    assert claim["worst_case_seconds"] == sum(delays) + walls
    # minutes round UP: understating the window would have an operator give up too early
    assert claim["backoff_total_minutes"] == math.ceil(sum(delays) / 60)
    assert claim["worst_case_minutes"] == math.ceil(claim["worst_case_seconds"] / 60)


@verifies("WIRE.CALLBACK.DELIVERY")
def _delivery_claim_carries_all_three_outcomes():
    """A6 permits zero sends for a SUPPRESSED row, so an at-least-once-only description is an
    overclaim. Dead-lettered is the opposite error: it means sent without a witnessed success, and
    a reader who treats it as 'not sent' double-applies."""
    lines = WIRE.value("WIRE.CALLBACK.DELIVERY")
    text = " ".join(lines).lower()
    assert "at least once" in text and "zero sends" in text and "dead-letter" in text
    assert "exactly once" not in text and "exactly one" not in text
    suppressed = next(line.lower() for line in lines if "suppressed" in line.lower())
    assert "zero sends" in suppressed
    dead = next(line.lower() for line in lines if line.startswith("DEAD-LETTERED"))
    assert "does not mean zero sends" in dead, "dead-lettered must not read as never sent"
    assert str(Settings().outbox_max_attempts) in dead or "eight" in dead


@verifies("WIRE.CALLBACK.RECEIVER_TXN")
def _receiver_transaction_requires_commit_before_2xx():
    steps = [s.lower() for s in WIRE.value("WIRE.CALLBACK.RECEIVER_TXN")]
    commit = next(i for i, s in enumerate(steps) if s.startswith("commit"))
    respond = next(i for i, s in enumerate(steps) if "return 2xx" in s)
    assert commit < respond, "the receiver contract must commit BEFORE returning 2xx"
    # recording is unconditional; whether it APPLIES is a separate decision (EFFECTIVENESS)
    assert any("either way" in s for s in steps)


@verifies("WIRE.CALLBACK.EFFECTIVENESS")
def _effectiveness_table_is_executable_and_total():
    """The authority here is an ALGORITHM, not a sentence.

    The previous verifier checked that "manual" and "not" appeared in some line, which the broken
    "otherwise apply" version satisfied while replacing a newer decision with an older one
    (re-audit `4f23f23..97deeae` F2). The table is now held against
    `docs/contracts/receiver_reference.py`, which implements it and is driven through the findings'
    own scenarios by `tests/unit/test_receiver_state_machine.py`; here we prove the published rows
    and that implementation are the same object of study.
    """
    from docs.contracts.receiver_reference import Callback, LedgerState, decide
    from docs.contracts.wire import INTERIM, POST_024, RECEIVER_TRANSITIONS

    rows = WIRE.value("WIRE.CALLBACK.EFFECTIVENESS")
    assert rows is RECEIVER_TRANSITIONS
    assert {t.phase for t in rows} == {INTERIM, POST_024}
    for phase in (INTERIM, POST_024):
        assert len([t for t in rows if t.phase == phase]) == 4
    assert not any("otherwise" in t.condition.lower() for t in rows)

    # the two outcomes the finding turns on, executed
    late_older = decide(
        LedgerState(seen_run_ids=frozenset({"B"}), current_source="automatic"),
        Callback("c", "A"), phase=INTERIM)
    assert late_older.record and not late_older.effective

    manual_current = decide(
        LedgerState(current_source="manual", high_water=5),
        Callback("c", "r", decision_sequence=6), phase=POST_024)
    assert not manual_current.effective and manual_current.advance_high_water

    # consistent with the interim ordering rule, which is the same rule stated for sorting
    assert "manual approval" in WIRE.value("WIRE.ORDERING.INTERIM").lower()


@verifies("WIRE.CALLBACK.WAIT_BOUND")
def _wait_bound_says_detach_not_cancel():
    """The publisher cannot retract an in-flight request; calling the wall a cancellation would
    let the receiver assume a timed-out attempt never landed."""
    text = WIRE.value("WIRE.CALLBACK.WAIT_BOUND").lower()
    assert "stop waiting" in text and "detach" in text
    assert "may still reach you" in text
    assert "cancel" not in text


@verifies("WIRE.CALLBACK.COMPLETION")
def _completion_claim_lists_every_case_that_sends_nothing():
    """'The callback is your completion signal' is true only for an automated decision that
    produces an eligible callback. Each exception below is a case where waiting for one hangs
    forever, so each must be named."""
    text = WIRE.value("WIRE.CALLBACK.COMPLETION").lower()
    assert "not a universal completion signal" in text
    assert "manual approval" in text and "no callback at all" in text
    assert "suppressed" in text and "never sent" in text
    assert "dead-lettered" in text and "was sent" in text


# ── ordering ──────────────────────────────────────────────────────────────────────────────────────
@verifies("WIRE.ORDERING.NO_DECIDED_AT")
def _decided_at_prohibition_still_has_repo_authority():
    """If the recorded prohibition is ever removed from the repo, this claim's authority is gone
    and the document must not keep asserting it."""
    runbook = (REPO / "docs" / "RUNBOOK.md").read_text()
    findings = (REPO / "AUDIT_FINDINGS.md").read_text()
    assert "fall back to" in runbook and "decided_at" in runbook
    assert "decided_at` is transaction-start time" in findings
    claim = WIRE.value("WIRE.ORDERING.NO_DECIDED_AT").lower()
    assert "not an ordering key" in claim and "transaction-start" in claim


@verifies("WIRE.ORDERING.INTERIM")
def _interim_rule_makes_a_manual_approval_authoritative():
    text = WIRE.value("WIRE.ORDERING.INTERIM").lower()
    assert "(case_id, run_id)" in text
    assert "manual approval" in text and "authoritative" in text
    assert "can arrive after it" in text


def _alembic_script(config_path=None, script_location=None) -> ScriptDirectory:
    """Alembic's own view of the revision graph, not a directory glob.

    The previous guard globbed `REPO/migrations/versions` — a path this repo does not have, since
    it uses `alembic/versions` (re-audit `4c3015a..cccd5f7` F12). The set was therefore always
    empty and the staleness assertion was vacuously true from the day it was written: adding a real
    `024_*.py` left the PENDING verifier green. Reading through `ScriptDirectory` means a layout
    change cannot silently disarm the guard, and it sees branch structure a glob cannot.

    Gate finding 5: this used to overwrite `script_location` UNCONDITIONALLY with `REPO/alembic`,
    which substitutes a convention for the configuration. An `alembic.ini` pointing at a different
    tree that contains 024 passed while the stale `./alembic` ended at 023 — the guard was watching
    a directory Alembic would not have used. The production path now takes the configured value
    verbatim; an explicit location is a TEST-FIXTURE injection only.
    """
    cfg = Config(str(config_path or (REPO / "alembic.ini")))
    if script_location is not None:
        cfg.set_main_option("script_location", str(script_location))
    # `script_location` in the repo config is RELATIVE, so resolve it against the config's own
    # directory rather than the process cwd — a cwd-dependent helper would pass or fail by accident.
    declared = cfg.get_main_option("script_location")
    if declared and not Path(declared).is_absolute():
        base = Path(config_path or (REPO / "alembic.ini")).parent
        cfg.set_main_option("script_location", str((base / declared).resolve()))
    return ScriptDirectory.from_config(cfg)


def assert_024_unbuilt(script: ScriptDirectory, callback_model) -> None:
    """PENDING means 024 exists in NEITHER authority, and the two must agree.

    A claim that publishes requirements instead of a schema is only honest while the thing is
    genuinely unbuilt, and 024 can become half-built from either side: the migration that installs
    the ordering schema, or `decision_sequence` on the callback — the post-024 ordering authority
    per ROADMAP D1. Checking one lets the other land silently, so both are checked and either alone
    is a failure. A field without its revision is not "nearly pending"; it is an undeclared partial
    build that an integrator could discover in the wire before the document admits it exists.
    """
    heads = script.get_heads()
    assert len(heads) == 1, (
        f"the revision graph has {len(heads)} heads ({sorted(heads)}). 024 landing on a branch is "
        "exactly how it arrives without moving the single head this claim is checked against."
    )
    revisions = {revision.revision for revision in script.walk_revisions()}
    built = sorted(r for r in revisions if r.startswith("024"))
    assert not built, f"revision(s) {built} exist; WIRE.ORDERING.BOOTSTRAP_024 is stale"

    fields = set(getattr(callback_model, "model_fields", {}))
    assert "decision_sequence" not in fields, (
        "the callback model publishes `decision_sequence` — the post-024 ordering authority — "
        "while the claim still says 024 is NOT BUILT. Either the revision is missing and the wire "
        "is ahead of the schema, or the claim is stale; both are failures."
    )

    # EXECUTE the encoder rather than reading a declaration (gate finding 6). Inspecting
    # `DecisionCallback.model_fields` alone watched a model the publisher did not use: it built a
    # plain dict, so adding `decision_sequence` to the emitter put the post-024 ordering key on the
    # wire with this verifier green. The encoder is now the single path to the outbox, so running
    # it against a body that TRIES to carry the key is a statement about what can actually be
    # published, not about what somebody declared.
    # The PUBLISHER's active value against the pre-024 LITERAL held in THIS verifier
    # (re-gate-3 finding 4). Comparing it to a sibling alias in the publisher's own module let the
    # natural two-line edit — move the alias and the active value together — pass while every
    # persisted attempt advertised sequenced wire. The verifier's literal does not move with the
    # module it checks; the vocabulary itself is pinned independently by ck_attempt_wire_vocab.
    assert publisher._WIRE_VERSION == "legacy", (
        f"the publisher advertises {publisher._WIRE_VERSION!r} wire while the claim says 024 is "
        "NOT BUILT"
    )
    # REFUSED, not dropped (re-gate finding 3). Silently discarding an unmodelled field made the
    # same key vanish on one path and leak on another; a post-024 ordering key arriving before 024
    # exists is a defect to surface.
    try:
        emitted = schemas.encode_decision_callback(_sequenced_callback_attempt())
    except Exception:
        emitted = None
    assert emitted is None, (
        "the publisher's own encoder accepted `decision_sequence` while 024 is unbuilt"
    )


@verifies("WIRE.ORDERING.BOOTSTRAP_024")
def _bootstrap_is_pending_and_publishes_no_schema():
    """024 is designed but unbuilt. The document must publish requirements, never a draft schema
    an integrator could implement."""
    claim = WIRE["WIRE.ORDERING.BOOTSTRAP_024"]
    assert claim.state is ClaimState.PENDING
    assert "NOT BUILT" in claim.value
    # ...and the REQUIREMENTS survive. Reducing the claim to the bare marker deleted everything an
    # integrator needs in order to answer, while still passing every check (re-audit F3).
    for requirement in ("accepted-run ledger", "manual approval", "two-sided coverage",
                        "response digests", "signed response envelope"):
        assert requirement in claim.value, f"the 024 requirements no longer mention {requirement!r}"
    for schema_ish in ("{", "}", "schema_version", "latest_run_id", "high_water_run_id"):
        assert schema_ish not in claim.value, "the pending claim is publishing a schema again"
    assert_024_unbuilt(_alembic_script(), DecisionCallback)


@verifies("WIRE.ORDERING.SEQUENCE_DOMAINS")
def _the_two_sequence_domains_match_D1_and_the_activation_spec():
    """Static parity across D1, the ROADMAP rows, the activation spec, and what we publish.

    Re-audit `4f23f23..122cc67` finding 1. The published post-024 rule ordered on
    `event_sequence`, which ROADMAP D1 assigns to PR 2 as ingest provenance; the callback-order
    authority is PR 7b's `decision_sequence`. The two invert whenever work completes out of
    admission order, so a receiver built on the wrong one suppresses the LATER decision — and
    both are monotonic per case, so it looks correct until two runs overlap.
    """
    roadmap = re.sub(r"\s+", " ", (REPO / ".agents" / "ROADMAP.md").read_text())
    assert "PR 2 → **`event_sequence`**" in roadmap
    assert "PR 7b → **`decision_sequence`**" in roadmap

    spec = (REPO / ".agents" / "superpowers" / "specs"
            / "2026-07-22-pr7b-activation-platform-ordering-design.md").read_text()
    assert "`decision_sequence` **on the wire**" in spec
    assert "decision_sequence" in spec.split("Wire emission")[1][:400]

    lines = WIRE.value("WIRE.ORDERING.SEQUENCE_DOMAINS")
    provenance = next(line for line in lines if line.startswith("event_sequence"))
    authority = next(line for line in lines if line.startswith("decision_sequence"))
    assert "INGEST PROVENANCE" in provenance and "NEVER an ordering authority" in provenance
    assert "CALLBACK-ORDER AUTHORITY" in authority
    assert "high-water mark" in authority and "decision_sequence and nothing else" in authority

    # event_sequence really is on the wire today, and decision_sequence really is not yet
    from kyc_tool.api.schemas import DecisionCallback

    assert "event_sequence" in DecisionCallback.model_fields
    assert "decision_sequence" not in DecisionCallback.model_fields, (
        "decision_sequence now ships; the claim must stop saying it arrives with 024"
    )
    assert "arrives with the activation unit" in " ".join(lines) or "with the activation unit" in authority

    # and the transition table orders on the authority, not the provenance
    post = [t for t in WIRE.value("WIRE.CALLBACK.EFFECTIVENESS") if t.phase == "post-024"]
    ordering_rows = " ".join(t.condition + " " + t.record for t in post)
    assert "decision_sequence" in ordering_rows
    assert "event_sequence" not in ordering_rows, (
        "the post-024 rows order on ingest provenance again"
    )


@verifies("WIRE.ORDERING.PENDING_INPUTS")
def _pending_024_inputs_cover_every_live_obligation():
    """One-to-one against the obligation ids PARSED FROM THE LIVE SPEC.

    Re-audit `4f23f23..97deeae` finding 10. The document asked three questions TechCraft could
    answer completely while 024 stayed non-buildable, because O1-O4 need decisions only the
    platform can make. Parsing the spec rather than hardcoding the ids means a fifth obligation
    appearing there fails this test until the document asks for it too.
    """
    spec = (REPO / ".agents" / "superpowers" / "specs"
            / "2026-07-22-pr7b-activation-platform-ordering-design.md").read_text()
    live = spec[spec.index("## Open blockers"):]
    live = live[: live.index("\n## ", 1)] if "\n## " in live[1:] else live
    obligations = set(re.findall(r"\*\*(O\d)\b", live))
    assert obligations == {"O1", "O2", "O3", "O4"}, f"the live obligation set changed: {obligations}"

    inputs = WIRE.value("WIRE.ORDERING.PENDING_INPUTS")
    covered = {i.obligation for i in inputs}
    assert covered == obligations, (
        f"uncovered obligations {sorted(obligations - covered)}; "
        f"inputs naming nothing live {sorted(covered - obligations)}"
    )
    for item in inputs:
        assert item.owner in {"TechCraft", "IPv4.Global", "both"}, item.owner
        # EVERY input carries an executable acceptance test, and it actually rejects the answers
        # that look complete while leaving 024 unsafe (re-audit `4f23f23..122cc67` finding 10):
        # a v1 signature, a local clock, a per-case release id, a writer matrix missing the
        # inline manual approve. `answer_type` describes a shape; only this decides.
        assert callable(item.accept), f"{item.obligation}: no executable acceptance test"
        assert item.must_reject, f"{item.obligation}: names no unusable answer"
        for label, answer in item.must_reject:
            reasons = item.accept(answer)
            assert reasons, f"{item.obligation}: {label} was accepted"
            assert all(isinstance(r, str) and r for r in reasons)
        # every input must ASK something: a question mark, or an explicit request to confirm
        assert "?" in item.question or item.question.startswith(("Confirm", "Agree")), (
            f"{item.obligation}: this input asks nothing")
        assert item.answer_type.strip() and item.authority.strip() and item.blocks.strip()
        assert item.obligation in item.authority, (
            f"{item.obligation}: the authority reference does not name its own obligation"
        )
    # no duplicate question under one obligation
    questions = [(i.obligation, i.question) for i in inputs]
    assert len(questions) == len(set(questions))
    assert WIRE["WIRE.ORDERING.PENDING_INPUTS"].state is ClaimState.PENDING

    # a good answer to each one is accepted, so the constraints are not simply refusing everything
    from docs.contracts.wire import REQUIRED_WRITER_ROLES

    good = {
        ("O1", "principal"): {"principal": "platform-svc", "hmac_version": "v2", "key_id": "k1"},
        ("O1", "deadline"): {"clock": "platform db", "ttl_seconds": 900},
        ("O2", ""): {"terminal_authority": "platform", "reaper_cadence_seconds": 60,
                     "outcome_recovery": "signed redelivery with replay on restart"},
        ("O3", ""): {"scope": "global", "allocated_by": "platform"},
        ("O4", ""): {"writer_roles": tuple(REQUIRED_WRITER_ROLES), "old_image_full_stop": True},
    }
    for item in inputs:
        answers = [a for (obligation, _hint), a in good.items() if obligation == item.obligation]
        assert any(item.accept(answer) == [] for answer in answers), (
            f"{item.obligation}: no acceptable answer exists, so the constraint refuses everything"
        )

    # the specific decisions the finding said were missing
    text = " ".join(i.question for i in inputs)
    for missing in ("principal", "HMAC VERSION", "deadline", "SOLE terminal", "reaper",
                    "GLOBALLY unique", "WRITER-ROLE MATRIX", "old-image"):
        assert missing in text, f"the 024 request still does not ask about {missing!r}"


@verifies("WIRE.ORDERING.INTEGRITY_MISMATCH")
def _integrity_mismatch_is_emitted_by_nothing_today():
    """The claim says no code path emits this state yet. Proven structurally: no string CONSTANT
    anywhere in src equals it. A raw substring search would match the prose in outbox/witness.py
    that merely names the future state, and would then keep passing after it started shipping —
    exactly backwards."""
    claim = WIRE["WIRE.ORDERING.INTEGRITY_MISMATCH"]
    assert claim.state is ClaimState.PENDING
    assert "no code path emits it today" in claim.value.lower()
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Constant) and node.value == "integrity_mismatch":
                raise AssertionError(f"{path.relative_to(REPO)} now emits integrity_mismatch")


# ── retention ─────────────────────────────────────────────────────────────────────────────────────
@verifies("WIRE.RETENTION.BY_KIND")
def _retention_claim_matches_the_per_kind_behaviour():
    retention = (SRC / "workers" / "retention.py").read_text()
    documented = dict((kind, text) for kind, text in WIRE.value("WIRE.RETENTION.BY_KIND"))
    assert "redacted in place" in documented["decision callbacks"]
    poc = documented["POC verification emails"]
    assert "DELETED" in poc and "delivered or dead" in poc
    assert "delete" in retention.lower() and "redact" in retention.lower()


@verifies("WIRE.RETENTION.WINDOW_DAYS")
def _retention_window_matches_settings():
    assert WIRE.value("WIRE.RETENTION.WINDOW_DAYS") == Settings().retention_days


# ── operations ────────────────────────────────────────────────────────────────────────────────────
@verifies("OPS.BLOCKER.PRODUCTION_PROVIDERS")
def _the_production_blocker_is_demonstrably_true():
    """The strongest claim in either document, so it is proven by execution rather than by text:
    production refuses stubs, and every non-stub provider factory raises."""
    from kyc_tool.outbox.emails import make_email_sender
    from kyc_tool.workers.pipeline_worker import _make_ocr_engine, build_adapters

    assert OPERATIONS["OPS.BLOCKER.PRODUCTION_PROVIDERS"].state is ClaimState.BLOCKED

    # (a) production refuses the stub values
    for stub in (
        {"ocr_engine": STUB_OCR_ENGINE},
        {"email_provider": STUB_EMAIL_PROVIDER},
        {"adapters_profile": STUB_ADAPTERS_PROFILE},
    ):
        assert production_config_violations(hardened(**stub)), f"{stub} should be refused"

    # (b) and every production-safe value is unimplemented
    with pytest.raises(NotImplementedError):
        _make_ocr_engine("tesseract")
    with pytest.raises(NotImplementedError):
        make_email_sender("ses")
    with pytest.raises(NotImplementedError):
        build_adapters(Settings(adapters_profile="real"), store=None)


@verifies("OPS.PROCESS.COMMANDS")
def _process_commands_reference_real_modules():
    commands = OPERATIONS.value("OPS.PROCESS.COMMANDS")
    for _label, command, _note in commands:
        if command.startswith("python -m "):
            module = command.split()[2]
            path = REPO / "src" / (module.replace(".", "/") + ".py")
            assert path.exists(), f"{command} names a module that does not exist"
    # the guide's heading counts them; a command added without updating it would go unnoticed
    assert len(commands) == 6


@verifies("OPS.PROCESS.DEV_WORKER_BANNED")
def _dev_worker_is_refused_by_the_process_role_authority():
    """Executed, not described: the role gate refuses dev_worker on an OTHERWISE VALID production
    configuration, which is what 'categorically' means."""
    assert not production_config_violations(hardened()), "the fixture is no longer a valid prod config"
    with pytest.raises(ProductionConfigError) as excinfo:
        validate_process_role(hardened(), ProcessRole.DEV_WORKER)
    assert "dev_worker" in str(excinfo.value)
    validate_process_role(hardened(), ProcessRole.OUTBOX_WORKER)  # a production role passes
    # the ban is PRODUCTION-specific, exactly as the claim words it
    validate_process_role(Settings(environment="development"), ProcessRole.DEV_WORKER)


@verifies("OPS.INFRA.COMPONENTS")
def _infrastructure_rows_match_the_settings_that_back_them():
    rows = {
        component: (requirement, why)
        for component, requirement, why in OPERATIONS.value("OPS.INFRA.COMPONENTS")
    }
    settings = Settings()
    assert hasattr(settings, "s3_bucket") and hasattr(settings, "object_store")
    # the namespace split is the load-bearing part of the object-store row
    from kyc_tool.ops.sweep_staged_evidence import STAGED_PREFIX

    assert STAGED_PREFIX.rstrip("/") in rows["Object store"][1]
    assert "uploads/" in rows["Object store"][1]
    # seven years, stated in the backups row, is the retention default in days
    assert round(settings.retention_days / 365.25) == 7
    assert "seven-year" in rows["Backups"][1]
    # the email row must not contradict the blocker
    assert "none is implemented" in rows["Email"][0]


@verifies("OPS.CONFIG.DEFAULTS")
def _config_defaults_match_settings():
    settings = Settings()
    for field, documented in OPERATIONS.value("OPS.CONFIG.DEFAULTS").items():
        assert int(getattr(settings, field)) == documented, f"{field} default moved"


@verifies("OPS.CONFIG.PRODUCTION_FLOORS")
def _production_floors_are_actually_enforced():
    assert any("job_lease_seconds" in v for v in production_config_violations(hardened(job_lease_seconds=29)))
    assert any("ui_admin_token" in v for v in production_config_violations(hardened(ui_admin_token="")))
    assert any(
        "outbox_lease_seconds" in v
        for v in production_config_violations(
            hardened(outbox_lease_seconds=10, outbox_http_timeout_seconds=10)
        )
    )


@verifies("OPS.CONFIG.HMAC_SET")
def _hmac_variable_set_is_complete_and_enforced():
    from kyc_tool.config import _MIN_HMAC_SECRET_LEN

    claim = OPERATIONS["OPS.CONFIG.HMAC_SET"]
    documented = claim.value
    assert len(documented) == len(set(documented)) == 8
    # The NOTE is published advice and is therefore authoritative (re-audit F3): it stated a
    # floor and a date requirement, and nothing compared either to the code, so "at least 8
    # characters" and "dates may be naive" both rendered happily.
    assert f"at least {_MIN_HMAC_SECRET_LEN} characters" in claim.note, claim.note
    assert "timezone-aware ISO-8601" in claim.note
    assert production_config_violations(hardened(hmac_inbound_secret="x" * 8))
    assert production_config_violations(hardened(hmac_v1_inbound_sunset_at="2026-09-01T00:00:00"))
    # dropping any one of the v2 values must produce a violation
    for override in (
        {"hmac_inbound_secret": ""},
        {"hmac_outbound_secret": ""},
        {"hmac_inbound_key_id": ""},
        {"hmac_outbound_key_id": ""},
        {"hmac_v1_inbound_sunset_at": ""},
        {"hmac_v1_outbound_sunset_at": ""},
    ):
        assert production_config_violations(hardened(**override)), override
    # EXACTLY the HMAC settings. "is a real Settings field" was too weak: swapping one entry for
    # KYC_DATABASE_URL passed, because database_url is also a real field (re-audit F3). An
    # operator following that list configures a database URL and omits an authentication secret.
    fields = {name.removeprefix("KYC_").lower() for name in documented}
    expected = {f for f in Settings.model_fields if f.startswith("hmac_")} | {"platform_hmac_secret"}
    expected -= {"hmac_max_skew_seconds", "hmac_inbound_extra_keys"}  # rotation + window are separate claims
    assert fields == expected, f"documented HMAC set drifted: {fields ^ expected}"


@verifies("OPS.CONFIG.ROTATION_KEYS")
def _rotation_key_grammar_is_enforced_at_both_layers():
    """Every refusal the claim promises, executed. The untrimmed-key-id case is the one that
    leaked a working secret into a ValidationError before F1."""
    assert hmac_extra_key_violations({"old": "s" * 32}, "active") == []
    assert hmac_extra_key_violations({}, "active") == []
    for bad in ({"old": "s" * 31}, {"": "s" * 32}, {" padded ": "s" * 32}, {"active": "s" * 32}):
        assert hmac_extra_key_violations(bad, "active"), bad
    assert hmac_extra_key_violations(None, "active"), "a non-mapping must be a violation, not a crash"
    # BOTH layers, which is what the claim promises. Construction refuses outright, so the
    # boundary layer has to be reached through model_copy — the same bypass F2 came in through.
    with pytest.raises(PydanticValidationError):
        hardened(hmac_inbound_extra_keys={"old": "s" * 31})
    smuggled = hardened().model_copy(update={"hmac_inbound_extra_keys": {"old": "s" * 31}})
    assert production_config_violations(smuggled), "the production boundary accepted a weak key"


@verifies("OPS.CONFIG.M2_GATE")
def _m2_gate_matches_the_roadmap():
    # whitespace-normalized: the authoritative phrase wraps across source lines, and a raw
    # substring check would silently pass on its absence rather than its presence
    roadmap = re.sub(r"\s+", " ", (REPO / ".agents" / "ROADMAP.md").read_text())
    claim = OPERATIONS.value("OPS.CONFIG.M2_GATE").lower()
    assert "M4 complete" in roadmap and "staging E2E on real adapters" in roadmap
    for required in ("m4", "staging end-to-end", "real adapters", "platform cutover", "permanent"):
        assert required in claim
    assert "validator hardening" not in claim


@verifies("OPS.HEALTH.PROBES")
def _every_documented_probe_is_a_declared_route():
    declared = _declared_routes()
    for surface, _contract, _action in OPERATIONS.value("OPS.HEALTH.PROBES"):
        for target in surface.removeprefix("GET ").split(" and "):
            assert ("GET", target.strip()) in declared, f"{surface} is not a declared route"


@verifies("OPS.RELEASE.CLASSIFICATION")
def _release_classification_matches_the_release_it_cites():
    """The claim rests on one concrete counterexample: a release with no migration that still took
    a full window. If the cited section stops saying that, the claim has no authority."""
    deployment = re.sub(r"\s+", " ", (REPO / "docs" / "DEPLOYMENT.md").read_text())
    assert "PR 5b cutover — brief full maintenance window" in deployment
    assert "This is not a rolling deploy." in deployment
    adrs = (REPO / "docs" / "architecture-decisions.md").read_text()
    assert "ADR-004 — Review-record binding" in adrs
    claim = OPERATIONS.value("OPS.RELEASE.CLASSIFICATION").lower()
    assert "independently" in claim and "no migration" in claim


def _deployment_section_body(section: str) -> str:
    """The normalized body under a DEPLOYMENT.md heading, up to the next heading."""
    lines = (REPO / "docs" / "DEPLOYMENT.md").read_text().split("\n")
    heads = [(i, ln) for i, ln in enumerate(lines) if re.match(r"^#{1,2} ", ln)]
    matches = [idx for idx, (_i, ln) in enumerate(heads) if section in ln]
    assert len(matches) == 1, f"{section!r} matches {len(matches)} headings; it must match one"
    idx = matches[0]
    start = heads[idx][0]
    end = heads[idx + 1][0] if idx + 1 < len(heads) else len(lines)
    return " ".join("\n".join(lines[start + 1:end]).split())


def _playbook_prose(section: str) -> str:
    """The section body as a reader sees it: markdown emphasis stripped, lowercased.

    The DIGEST is taken over the raw normalized body (emphasis intact), because that is the byte
    sequence a reviewer read. Evidence quotes are matched against this softer form so a contract
    can quote a sentence without reproducing its asterisks and backticks.
    """
    return re.sub(r"[*`]", "", _deployment_section_body(section)).lower()


def _downgrade_kind(path: Path) -> str:
    """How one revision's downgrade behaves, read from its AST.

    The distinction that matters is structural and unambiguous: a revision that refuses
    UNCONDITIONALLY opens its body with a bare `raise`, so nothing can run first. A revision that
    refuses on evidence reaches its `raise` through an `if`, and reverses cleanly when the
    condition does not hold. `test_rollback_claim...` needs the same split, and getting it from the
    tree rather than from a phrase is what makes the schema answer derived instead of asserted.
    """
    tree = ast.parse(path.read_text())
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "downgrade"), None)
    if fn is None:
        return "absent"
    body = [n for n in fn.body
            if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                    and isinstance(n.value.value, str))]
    if body and isinstance(body[0], ast.Raise):
        return "refuses_always"
    if any(isinstance(n, ast.Raise) for n in ast.walk(fn)):
        return "refuses_conditionally"
    if body and all(isinstance(n, ast.Pass) for n in body):
        return "no_op"
    return "reverses"


def _revision_file(revision: str) -> Path:
    matches = sorted((REPO / "alembic" / "versions").glob(f"{revision}_*.py"))
    assert len(matches) == 1, f"revision {revision} matches {len(matches)} files"
    return matches[0]


def _forward_only_revisions_named_in(body: str) -> set[str]:
    """Revisions the playbook body mentions whose downgrade refuses UNCONDITIONALLY.

    This is what binds `migration_range` to something outside the author's control. Without it the
    range is just another authored field, and understating it makes an understated schema answer
    self-consistent: declare `migration_range=()` alongside `schema=stays` and the derivation
    agrees, because it derives over the range it was handed. A playbook that names a forward-only
    revision is describing a cutover that crosses it, so the two cannot disagree. Defeating this
    means deleting `018` from DEPLOYMENT.md, which breaks the digest and the evidence quote.

    Only UNCONDITIONAL refusers count. Bodies legitimately reference older revisions for context —
    PR 6's names 003, 005, 010, 011 and 012, none of which are forward-only — and a cutover does
    not install every revision it mentions.
    """
    versions = REPO / "alembic" / "versions"
    forward_only = set()
    for revision in set(re.findall(r"\b(0\d{2})\b", body)):
        # A three-digit token is not necessarily a revision — 024 is named throughout and is
        # unbuilt. Only numbers with a revision file behind them are classified.
        if not sorted(versions.glob(f"{revision}_*.py")):
            continue
        if _downgrade_kind(_revision_file(revision)) == "refuses_always":
            forward_only.add(revision)
    return forward_only


def _assert_range_covers_forward_only(procedure, body: str, section: str) -> None:
    """The one check binding `migration_range` to something the author does not control.

    Factored out so the RED test below runs THIS code against a tampered procedure rather than a
    hand-copied restatement of it — a second copy of a guard is a guard that can pass while the
    real one fails.
    """
    uncovered = sorted(_forward_only_revisions_named_in(body) - set(procedure.migration_range))
    assert not uncovered, (
        f"{procedure.name}: {section!r} warns about forward-only revision(s) {uncovered} that "
        f"migration_range {procedure.migration_range or '(none)'} does not contain. A cutover "
        "cannot warn about a revision it claims not to install — that combination understates the "
        "boundary AND makes the schema answer derive over the understated range."
    )


def _schema_answer_from_migrations(revisions: tuple[str, ...]) -> str:
    """The schema answer a procedure MUST publish, computed from what its migrations actually do.

    Nothing about this reads the contract. It is the independent authority the contract's `schema`
    answer is compared against — the registry does not get to tell the test what the migrations do.
    """
    if not revisions:
        return playbook.SCHEMA_STAYS
    kinds = {revision: _downgrade_kind(_revision_file(revision)) for revision in revisions}
    values = set(kinds.values())
    if values <= {"reverses", "no_op"}:
        return playbook.SCHEMA_DOWNGRADES
    if values <= {"refuses_always"}:
        # Nothing in the range can be walked back at all.
        return playbook.SCHEMA_FROZEN
    # Some revision refuses and some does not, so how far the schema got decides whether it moves.
    # That covers both PR 7b-core's shape (018-022 refuse outright, 013-017 refuse only on
    # evidence, 023 is a no-op) and a range that refuses only conditionally — which is NOT a clean
    # downgrade: whether it reverses depends on whether a witness exists.
    return playbook.SCHEMA_CONDITIONAL


@verifies("OPS.CUTOVER.PROCEDURES")
def _every_procedure_points_at_a_reviewed_playbook_body():
    """These claims deliberately do NOT restate steps, which makes the pointer load-bearing.

    Asserting the HEADING exists was not enough (re-audit `4f23f23..97deeae` finding 8): pointing
    the repo at a DEPLOYMENT.md containing only the three headings passed. So each procedure pins
    a digest of the REVIEWED BODY under its heading. An empty body, a wrong section with the same
    name, or an edit nobody re-reviewed all fail here, and re-pinning the digest is the act of
    re-reviewing.

    The digest alone was still not enough, which is the gap Claude declared open in
    `4c3015a..eaa3f8f`: `rollback` and `irreversible` were hand-written prose sitting BESIDE the
    digest, so rewriting them to prescribe the exact thing a playbook prohibits left the digest
    matching. Both are now derived from a typed `RollbackContract`, and this test holds every part
    of that contract against something outside it — the migrations for the schema answer, the
    digest-bound body for each quote, the contract's own structure for the rest.
    """
    procedures = OPERATIONS.value("OPS.CUTOVER.PROCEDURES")
    assert {p.name for p in procedures} == {
        "PR 5b full maintenance window", "Bundle-pinning activation", "Migrations 013-023",
    }
    for procedure in procedures:
        document, _, section = procedure.playbook.partition(", ")
        assert (REPO / document).exists(), f"{procedure.name} points at a missing document"
        body = _deployment_section_body(section)
        assert len(body) > 500, (
            f"{procedure.name}: the section under {section!r} is {len(body)} chars — a pointer to "
            "an empty body is worse than the summary it replaced"
        )
        digest = hashlib.sha256(body.encode()).hexdigest()
        assert digest == procedure.playbook_digest, (
            f"{procedure.name}: {section!r} changed since it was reviewed.\n"
            f"  reviewed: {procedure.playbook_digest}\n  now:      {digest}\n"
            "Re-read the section, then re-pin playbook_digest in the SAME commit."
        )
        assert procedure.blocks_start, f"{procedure.name} lists nothing that blocks starting"
        assert procedure.when.strip()
        # no step numbering: a numbered list here IS the summary this claim exists to avoid
        for prerequisite in procedure.blocks_start:
            assert not re.match(r"^\s*(?:step\s*)?\d+[.)]", prerequisite.lower()), (
                f"{procedure.name} is restating numbered steps again"
            )

        contract = procedure.rollback_contract
        # 1. TOTAL: the contract answers every question, so no control can be omitted by silence.
        answered = {f.question for f in contract.facts}
        assert answered == set(playbook.ROLLBACK_QUESTIONS), (
            f"{procedure.name} leaves {sorted(set(playbook.ROLLBACK_QUESTIONS) - answered)} "
            "unanswered"
        )
        # 2. DERIVED: the published prose is the contract's, not an author's.
        statements = contract.statements()
        assert procedure.irreversible == statements[0]
        assert procedure.rollback == statements[1:]
        # 3. QUOTED: every non-silent answer points at a sentence in the digest-bound body.
        prose = _playbook_prose(section)
        for fact in contract.facts:
            if fact.answer in playbook.SILENT_ANSWERS:
                assert not fact.evidence
                continue
            assert fact.evidence.lower() in prose, (
                f"{procedure.name}/{fact.question}: the quote\n  {fact.evidence!r}\n"
                f"is not in the reviewed body of {section!r}. An answer must come from the "
                "playbook it claims to summarize."
            )
        # 4. The RANGE is bound to the playbook, so it cannot be understated to make an
        #    understated schema answer agree with itself.
        _assert_range_covers_forward_only(procedure, body, section)
        # 5. INDEPENDENT: the schema answer is recomputed from the downgrade bodies.
        derived = _schema_answer_from_migrations(procedure.migration_range)
        assert contract.answer(playbook.SCHEMA) == derived, (
            f"{procedure.name} publishes schema={contract.answer(playbook.SCHEMA)!r} but its "
            f"migrations {procedure.migration_range or '(none)'} say {derived!r}"
        )

    by_name = {p.name: p for p in procedures}

    # PR 5b is SCOPED, not generic. A future unrelated full-window release must not inherit a
    # procedure whose rollback knowingly restores THIS release's vulnerability.
    pr5b = by_name["PR 5b full maintenance window"]
    assert "PR 5b" in pr5b.when and "do not reuse this one" in pr5b.when.lower()

    # And the release DID close a hole, which is executable, not editorial: the reviewer-actor
    # floor is in the shipped code and gates the manual-approve path. So an image-PRIOR rollback
    # reopens it, and the contract's own invariant then forces non-mutating verification. If the
    # floor is ever removed from the code, this fails and the answers must be re-derived.
    ingest = (SRC / "events" / "ingest.py").read_text()
    assert "reviewer_actor_reason" in ingest, "the PR 5b actor floor is gone from the ingest path"
    assert (SRC / "events" / "review_guard.py").exists()
    assert pr5b.rollback_contract.answer(playbook.IMAGE) == playbook.IMAGE_PRIOR
    assert pr5b.rollback_contract.answer(playbook.RESTORES) == playbook.RESTORES_YES
    assert pr5b.rollback_contract.answer(playbook.VERIFICATION) == playbook.VERIFY_NON_MUTATING

    # order inside the prerequisites still matters
    pr7b = [p.lower() for p in by_name["Migrations 013-023"].blocks_start]
    suspended = next(i for i, p in enumerate(pr7b) if "retention is suspended" in p)
    diagnostic = next(i for i, p in enumerate(pr7b) if "pre-window diagnostic" in p)
    assert suspended < diagnostic
    assert any("backup" in p for p in pr7b)

    # 013-023 is LOCAL authority; platform ordering waits for 024 (finding 5)
    when = by_name["Migrations 013-023"].when.lower()
    assert "receipt/transition-authority" in when
    assert "best-effort local supersession" in when
    assert "024" in when and "absent until" in when
    assert "ordering-authority schema" not in when

    # the control a step summary kept dropping
    assert any("load balancer" in p.lower() or "trusted path" in p.lower()
               for p in pr5b.blocks_start)
    # and the one TOTALITY recovered: every procedure must now publish that it ends resumed
    for procedure in procedures:
        assert procedure.rollback_contract.answer(playbook.ENDING) == playbook.ENDING_RESUMED


@verifies("OPS.CUTOVER.OUTBOX_CEILING")
def _outbox_ceiling_cutover_matches_the_shipped_canonical_record():
    """Rendered from the same record DEPLOYMENT, RUNBOOK, and .env.example embed, in order. This
    is the one cutover the registry does restate, because it is GENERATED from the authority
    rather than summarized from prose."""
    documented = OPERATIONS.value("OPS.CUTOVER.OUTBOX_CEILING")
    canonical = cutover.render_cutover(cutover.OUTBOX_MAX_ATTEMPTS_CUTOVER)[1:]  # drop the title
    assert list(documented) == [line.split(". ", 1)[1] for line in canonical]


@verifies("OPS.ROLLBACK.MIGRATION_BOUNDARY")
def _rollback_claim_matches_each_revision_s_actual_downgrade():
    """Derived from the migrations, not from a phrase.

    The claim said "018 and above refuse unconditionally". 018-022 do; 023's downgrade is a bare
    `pass` — it is validation-only, so stepping down from head removes its stamp and 022 blocks
    the next step. Overall safety is unchanged, but an operator planning a rollback was given a
    false per-revision account (re-audit `4f23f23..97deeae` finding 12). So the test reads each
    revision's downgrade body and requires the claim to match what is actually there.
    """
    versions = REPO / "alembic" / "versions"
    refusing, no_op = set(), set()
    for path in sorted(versions.glob("0*.py")):
        revision = path.name.split("_", 1)[0]
        tree = ast.parse(path.read_text())
        downgrade = next(
            (n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef) and n.name == "downgrade"), None)
        if downgrade is None:
            continue
        body = [n for n in downgrade.body if not isinstance(n, ast.Expr)]  # drop the docstring
        if any(isinstance(n, ast.Raise) for n in ast.walk(downgrade)):
            refusing.add(revision)
        elif all(isinstance(n, ast.Pass) for n in body) and body:
            no_op.add(revision)

    assert {"018", "019", "020", "021", "022"} <= refusing, sorted(refusing)
    assert "023" in no_op, "023 no longer downgrades as a no-op; the claim's exception is stale"

    text = " ".join(OPERATIONS.value("OPS.ROLLBACK.MIGRATION_BOUNDARY"))
    assert "018 through 022" in text, "the claim is back to an 'and above' account"
    assert "unconditionally" in text.lower() and "including staging" in text.lower()
    assert "023" in text and "validation-only" in text and "022 then blocks" in text
    lowered = text.lower()
    assert "downgrade-freely" not in lowered.replace(
        "there is no downgrade-freely rule anywhere", "")


@verifies("OPS.HMAC.ROLLOUT_ORDER")
def _hmac_rollout_puts_the_v2_smoke_before_the_observation_clock():
    steps = [s.lower() for s in OPERATIONS.value("OPS.HMAC.ROLLOUT_ORDER")]
    smoke = next(i for i, s in enumerate(steps) if "v2-only callback" in s)
    clock = next(i for i, s in enumerate(steps) if "observation clock" in s)
    assert smoke < clock, "activating the observation clock before the v2 callback smoke"
    assert any("sign-off" in s for s in steps)


@verifies("OPS.RECOVERY.REQUEUE")
def _requeue_endpoints_exist_and_take_the_admin_token():
    """Route shape is compared with path parameters normalized: the document names them for a
    reader ({id}) and the code names them for a handler ({job_id}). The literal segments and the
    arity are the contract; the parameter's spelling is not."""

    def shape(path: str) -> str:
        return re.sub(r"\{[^}]*\}", "{}", path.rstrip(".,"))

    declared = {(method, shape(path)) for method, path in _declared_routes()}
    claim = OPERATIONS.value("OPS.RECOVERY.REQUEUE")
    documented = re.findall(r"/v1/ops/requeue/\S+", claim)
    assert len(documented) == 2
    for path in documented:
        assert ("POST", shape(path)) in declared, f"{path} is not a declared route"

    # always-mounted, and behind the admin token — both are the claim, and both are structural
    ops = (SRC / "api" / "routes_ops.py").read_text()
    assert "require_admin" in ops and "_require_ops_admin(request)" in ops
    app = (SRC / "api" / "app.py").read_text()
    assert "app.include_router(ops_router)" in app
    mounted_conditionally = re.search(r"if .*:\n\s+app\.include_router\(ops_router\)", app)
    assert mounted_conditionally is None, "the ops router is now mounted conditionally"

    # the fixed retry grant is a required keyword, not a default someone can forget to pass
    import inspect

    from kyc_tool.ops.requeue_service import requeue_dead_job

    grant = inspect.signature(requeue_dead_job).parameters["attempt_grant"]
    assert grant.kind is inspect.Parameter.KEYWORD_ONLY
    assert grant.default is inspect.Parameter.empty, "the retry grant now has a silent default"


# ── the closure and the runner ────────────────────────────────────────────────────────────────────
ALL_CLAIM_IDS = frozenset(WIRE.ids) | frozenset(OPERATIONS.ids)


def test_the_verifier_map_is_closed_over_both_registries():
    """The point of the whole file. A claim without a verifier is a value the documents publish
    that nothing checks; a verifier without a claim is a check that silently stopped running."""
    unverified = ALL_CLAIM_IDS - set(AUTHORITY_VERIFIERS)
    orphaned = set(AUTHORITY_VERIFIERS) - ALL_CLAIM_IDS
    assert not unverified, f"claims with no independent authority check: {sorted(unverified)}"
    assert not orphaned, f"verifiers naming claims that no longer exist: {sorted(orphaned)}"


@pytest.mark.parametrize("claim_id", sorted(ALL_CLAIM_IDS))
def test_claim_holds_against_independent_authority(claim_id):
    AUTHORITY_VERIFIERS[claim_id]()


def test_every_claim_names_an_authority_and_a_known_state():
    for registry in (WIRE, OPERATIONS):
        for claim in registry.claims:
            assert claim.authority.strip(), f"{claim.id} names no authority"
            assert isinstance(claim.state, ClaimState)
            assert claim.id == claim.id.upper(), f"{claim.id} should be an upper-case id"


# ── the rollback contract's guards, proven to bite ─────────────────────────────────────────────
#
# Everything below exists because the previous version of this control was assertion-shaped: it
# stated invariants and never showed one failing. A guard nobody has watched reject something is
# indistinguishable from a guard that cannot.


def _valid_facts(**overrides) -> tuple:
    """A well-formed contract, with named answers swapped out. The base is deliberately the
    mildest legal shape, so any rejection below is caused by the override under test."""
    base = {
        playbook.REVERSIBILITY: (playbook.REVERSIBLE_WITH_CONDITIONS, "a"),
        playbook.SCHEMA: (playbook.SCHEMA_STAYS, "b"),
        playbook.IMAGE: (playbook.IMAGE_PRIOR, "c"),
        playbook.RESTORES: (playbook.RESTORES_NO, "d"),
        playbook.VERIFICATION: (playbook.VERIFY_UNRESTRICTED, "e"),
        playbook.WINDOW: (playbook.WINDOW_LIGHTER, "f"),
        playbook.ENDING: (playbook.ENDING_RESUMED, "g"),
    }
    base.update(overrides)
    return tuple(
        playbook.RollbackFact(q, answer, evidence) for q, (answer, evidence) in base.items()
    )


def test_the_baseline_contract_is_actually_valid():
    """Otherwise every rejection below could be the baseline failing, not the mutation."""
    assert playbook.RollbackContract(_valid_facts()).statements()


ROLLBACK_MUTATIONS = [
    (
        "restoring a vulnerability without restricting verification",
        {playbook.RESTORES: (playbook.RESTORES_YES, "d"),
         playbook.VERIFICATION: (playbook.VERIFY_UNRESTRICTED, "e")},
        "non-mutating",
    ),
    (
        "restoring a vulnerability while claiming the playbook is silent on probes",
        {playbook.RESTORES: (playbook.RESTORES_YES, "d"),
         playbook.VERIFICATION: (playbook.VERIFY_NOT_STATED, "")},
        "non-mutating",
    ),
    (
        "staying on this release's image while claiming it reopens the hole",
        {playbook.IMAGE: (playbook.IMAGE_SAME_RELEASE, "c"),
         playbook.RESTORES: (playbook.RESTORES_YES, "d"),
         playbook.VERIFICATION: (playbook.VERIFY_NON_MUTATING, "e")},
        "cannot restore what this release closed",
    ),
    (
        "a branch-dependent image on a schema that cannot move",
        {playbook.IMAGE: (playbook.IMAGE_CONDITIONAL, "c")},
        "no second branch",
    ),
    (
        "a boundary in the schema answer but not in the reversibility answer",
        {playbook.SCHEMA: (playbook.SCHEMA_CONDITIONAL, "b")},
        "same boundary seen twice",
    ),
    (
        "an irreversible cutover whose schema answer forgot the boundary",
        {playbook.REVERSIBILITY: (playbook.IRREVERSIBLE_PAST_BOUNDARY, "a")},
        "same boundary seen twice",
    ),
    (
        "calling a rollback plain when it reopens a closed hole",
        {playbook.REVERSIBILITY: (playbook.REVERSIBLE_PLAINLY, "a"),
         playbook.RESTORES: (playbook.RESTORES_YES, "d"),
         playbook.VERIFICATION: (playbook.VERIFY_NON_MUTATING, "e")},
        "not plainly reversible",
    ),
    (
        "calling a rollback plain when it needs the full window",
        {playbook.REVERSIBILITY: (playbook.REVERSIBLE_PLAINLY, "a"),
         playbook.WINDOW: (playbook.WINDOW_SAME, "f")},
        "not plainly reversible",
    ),
    (
        "citing one clause as the evidence for two different answers",
        {playbook.WINDOW: (playbook.WINDOW_LIGHTER, "g")},
        "more than one question",
    ),
]


@pytest.mark.parametrize(
    "label,overrides,needle", ROLLBACK_MUTATIONS, ids=[m[0] for m in ROLLBACK_MUTATIONS]
)
def test_rollback_contract_rejects(label, overrides, needle):
    with pytest.raises(ValueError, match=re.escape(needle)):
        playbook.RollbackContract(_valid_facts(**overrides))


def test_an_incomplete_contract_cannot_be_built():
    """Totality is the guard that recovered the end-resumed control. A procedure that simply omits
    a rollback question must not be constructible."""
    partial = tuple(f for f in _valid_facts() if f.question != playbook.ENDING)
    with pytest.raises(ValueError, match="does not answer"):
        playbook.RollbackContract(partial)


def test_an_answer_must_quote_the_playbook_and_silence_must_not():
    with pytest.raises(ValueError, match="must quote the playbook"):
        playbook.RollbackFact(playbook.WINDOW, playbook.WINDOW_SAME, "")
    with pytest.raises(ValueError, match="cannot also quote"):
        playbook.RollbackFact(playbook.VERIFICATION, playbook.VERIFY_NOT_STATED, "something")
    with pytest.raises(ValueError, match="is not one of"):
        playbook.RollbackFact(playbook.WINDOW, "whenever_convenient", "x")
    with pytest.raises(ValueError, match="unknown rollback question"):
        playbook.RollbackFact("vibes", playbook.WINDOW_SAME, "x")


def test_rollback_prose_cannot_be_authored_at_all():
    """The original attack was editing the rollback text. There is no longer a field to edit: both
    published fields are `init=False` and computed from the contract."""
    contract = playbook.RollbackContract(_valid_facts())
    common = dict(
        name="x", when="y", blocks_start=("z",), playbook="docs/DEPLOYMENT.md, PR 6 cutover",
        playbook_digest="0" * 64, rollback_contract=contract,
    )
    with pytest.raises(TypeError):
        Procedure(**common, rollback=("redeploy the previous image",))
    with pytest.raises(TypeError):
        Procedure(**common, irreversible="No, just redeploy.")
    # and what it does publish is exactly the contract's own sentences
    built = Procedure(**common)
    assert built.irreversible == contract.statements()[0]
    assert built.rollback == contract.statements()[1:]


def test_an_answer_quoting_a_sentence_the_playbook_does_not_contain_is_caught():
    """The evidence check is what stops an answer being justified by an invented quote."""
    fabricated = playbook.RollbackFact(
        playbook.WINDOW, playbook.WINDOW_SAME, "rollback may be performed at any time"
    )
    assert fabricated.evidence.lower() not in _playbook_prose("PR 6 cutover")
    # every quote the shipped contracts actually use IS present — same check, opposite direction
    for procedure in OPERATIONS.value("OPS.CUTOVER.PROCEDURES"):
        prose = _playbook_prose(procedure.playbook.partition(", ")[2])
        for fact in procedure.rollback_contract.facts:
            if fact.evidence:
                assert fact.evidence.lower() in prose


def test_downgrade_classifier_separates_unconditional_refusal_from_conditional(tmp_path):
    """The schema answer is only as good as this split, so it is tested on fixtures where the
    right answer is known by construction, not just on the repo's own migrations."""
    cases = {
        "always.py": "def downgrade():\n    raise RuntimeError('no')\n",
        "always_with_docstring.py": "def downgrade():\n    '''doc'''\n    raise RuntimeError('no')\n",
        "conditional.py": "def downgrade():\n    if witnesses():\n        raise RuntimeError('no')\n"
                          "    op.drop_column('t', 'c')\n",
        "noop.py": "def downgrade():\n    pass\n",
        "reverses.py": "def downgrade():\n    op.drop_column('t', 'c')\n",
    }
    for name, source in cases.items():
        (tmp_path / name).write_text(source)
    assert _downgrade_kind(tmp_path / "always.py") == "refuses_always"
    assert _downgrade_kind(tmp_path / "always_with_docstring.py") == "refuses_always"
    assert _downgrade_kind(tmp_path / "conditional.py") == "refuses_conditionally"
    assert _downgrade_kind(tmp_path / "noop.py") == "no_op"
    assert _downgrade_kind(tmp_path / "reverses.py") == "reverses"


def test_the_repo_s_own_migrations_classify_as_the_contract_needs():
    """The derivation feeding PR 7b-core's schema answer, read off the real tree."""
    def kind(revision: str) -> str:
        return _downgrade_kind(next((REPO / "alembic" / "versions").glob(f"{revision}_*.py")))

    for revision in ("018", "019", "020", "021", "022"):
        assert kind(revision) == "refuses_always", f"{revision} no longer refuses outright"
    for revision in ("013", "014", "015", "016", "017"):
        assert kind(revision) == "refuses_conditionally", f"{revision} changed shape"
    assert kind("023") == "no_op"


def test_schema_answer_is_recomputed_from_migrations_not_trusted():
    """A procedure that understates its own boundary is caught by the migrations, not by prose.

    The mutation has to be built as a whole legal contract — SCHEMA_STAYS forces the reversibility
    answer away from the boundary too, which is the point of that invariant — and it still fails,
    because the revisions it names refuse unconditionally.
    """
    pr7b = next(p for p in OPERATIONS.value("OPS.CUTOVER.PROCEDURES")
                if p.name == "Migrations 013-023")
    assert _schema_answer_from_migrations(pr7b.migration_range) == playbook.SCHEMA_CONDITIONAL

    understated = playbook.RollbackContract(_valid_facts())  # schema=STAYS, reversibility=mild
    assert understated.answer(playbook.SCHEMA) == playbook.SCHEMA_STAYS
    assert understated.answer(playbook.SCHEMA) != _schema_answer_from_migrations(
        pr7b.migration_range
    ), "a procedure could understate the 018 boundary and the migrations would not notice"

    # and the derivation is total over the shapes the tree can present
    assert _schema_answer_from_migrations(()) == playbook.SCHEMA_STAYS
    # refuses only on evidence -> still CONDITIONAL, not a clean downgrade: whether it reverses
    # depends on whether a witness exists, which is exactly what "conditional" means here
    assert _schema_answer_from_migrations(("013", "014")) == playbook.SCHEMA_CONDITIONAL
    assert _schema_answer_from_migrations(("018", "019")) == playbook.SCHEMA_FROZEN
    assert _schema_answer_from_migrations(("001", "002")) == playbook.SCHEMA_DOWNGRADES


def test_understating_the_migration_range_cannot_erase_the_boundary():
    """The hole a first pass at this left open, and the reason `migration_range` is not enough on
    its own.

    Deriving the schema answer from a range the AUTHOR supplies only moves the trust one level
    down. Declare `migration_range=()` and the derivation dutifully returns `stays`, which then
    agrees with an understated `schema=stays`, which in turn permits `reversibility=with_conditions`
    — and PR 7b-core's 018 boundary vanishes from the document with every invariant satisfied.

    The fix is that the playbook body has to agree too. It names 018 and 022 as forward-only, and a
    cutover cannot warn about a revision it claims not to install.
    """
    pr7b = next(p for p in OPERATIONS.value("OPS.CUTOVER.PROCEDURES")
                if p.name == "Migrations 013-023")
    body = _deployment_section_body(pr7b.playbook.partition(", ")[2])

    assert {"018", "022"} <= _forward_only_revisions_named_in(body)

    # the erased-boundary contract is internally consistent — every invariant in playbook.py
    # passes, and the derivation agrees with it once the range is empty
    erased = playbook.RollbackContract(_valid_facts())
    assert erased.answer(playbook.SCHEMA) == playbook.SCHEMA_STAYS
    assert erased.answer(playbook.REVERSIBILITY) == playbook.REVERSIBLE_WITH_CONDITIONS
    assert _schema_answer_from_migrations(()) == erased.answer(playbook.SCHEMA)

    # the shipped procedure passes the bind; the understated one does not. Both run the SAME
    # check the release runs, not a restatement of it.
    _assert_range_covers_forward_only(pr7b, body, "PR 7b-core cutover")
    understated = dataclasses.replace(pr7b, migration_range=(), rollback_contract=erased)
    with pytest.raises(AssertionError, match="cannot warn about a revision it claims not to"):
        _assert_range_covers_forward_only(understated, body, "PR 7b-core cutover")

    # and dropping just one revision is caught — the check is per-revision, not a count
    trimmed = dataclasses.replace(
        pr7b, migration_range=tuple(r for r in pr7b.migration_range if r != "022"))
    with pytest.raises(AssertionError, match=r"\['022'\]"):
        _assert_range_covers_forward_only(trimmed, body, "PR 7b-core cutover")


def test_older_revisions_a_playbook_merely_references_are_not_forced_into_the_range():
    """The bind has to distinguish 'this cutover installs it' from 'this section mentions it'.

    PR 6's body references 003, 005, 010, 011 and 012 for context while installing nothing — its
    cutover is the flag flip. Requiring every mentioned revision to be in the range would make the
    correct empty range fail, so only UNCONDITIONAL refusers count.
    """
    pr6 = next(p for p in OPERATIONS.value("OPS.CUTOVER.PROCEDURES")
               if p.name == "Bundle-pinning activation")
    body = _deployment_section_body(pr6.playbook.partition(", ")[2])
    mentioned = set(re.findall(r"\b(0\d{2})\b", body))
    assert {"003", "005", "010", "011", "012"} <= mentioned, sorted(mentioned)
    assert not _forward_only_revisions_named_in(body), (
        "PR 6 references older revisions for context; none of them is forward-only, so its empty "
        "migration_range is consistent with its playbook"
    )
    assert pr6.migration_range == ()


# ── F12: the 024 staleness guard, proven to bite ───────────────────────────────────────────────
#
# Codex's four reproductions, each run through the SAME helper the release verifier calls. The old
# guard globbed a directory that does not exist, so it passed all four.


def _fake_script(tmp_path, revisions: dict) -> ScriptDirectory:
    """A throwaway Alembic tree. `revisions` maps revision id -> down_revision (None for base)."""
    versions = tmp_path / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    for revision, down in revisions.items():
        down_literal = "None" if down is None else f'"{down}"'
        (versions / f"{revision}_probe.py").write_text(
            f'"""probe {revision}"""\nrevision = "{revision}"\n'
            f"down_revision = {down_literal}\nbranch_labels = None\ndepends_on = None\n"
            "def upgrade():\n    pass\n\n\ndef downgrade():\n    pass\n"
        )
    return _alembic_script(script_location=tmp_path)


class _Model:
    """Stand-in for the callback model; only `model_fields` is read."""

    def __init__(self, *names):
        self.model_fields = dict.fromkeys(names)


def test_the_shipped_tree_is_genuinely_pending():
    """Baseline. Without this, every rejection below could be the helper failing on anything."""
    assert_024_unbuilt(_alembic_script(), DecisionCallback)


def test_024_present_while_pending_is_caught(tmp_path):
    script = _fake_script(tmp_path, {"022": None, "023": "022", "024": "023"})
    with pytest.raises(AssertionError, match="is stale"):
        assert_024_unbuilt(script, _Model("case_id"))


def test_024_on_a_multiple_head_branch_is_caught(tmp_path):
    """The interesting shape: 024 lands beside the existing head rather than after it, so a check
    that only followed the single head would walk straight past it."""
    script = _fake_script(tmp_path, {"022": None, "023": "022", "024": "022"})
    with pytest.raises(AssertionError, match="heads"):
        assert_024_unbuilt(script, _Model("case_id"))


def test_024_present_without_the_callback_field_is_caught(tmp_path):
    """Half-built from the schema side: the revision exists, the wire has not caught up."""
    script = _fake_script(tmp_path, {"023": None, "024": "023"})
    with pytest.raises(AssertionError, match="is stale"):
        assert_024_unbuilt(script, _Model("case_id", "event_sequence"))


def test_the_callback_field_present_while_the_revision_is_absent_is_caught(tmp_path):
    """Half-built from the wire side, and the direction the old guard could never have seen: the
    integrator meets `decision_sequence` on the callback while the document says NOT BUILT."""
    script = _fake_script(tmp_path, {"022": None, "023": "022"})
    with pytest.raises(AssertionError, match="decision_sequence"):
        assert_024_unbuilt(script, _Model("case_id", "decision_sequence"))


def test_the_guard_reads_alembic_not_a_directory_path():
    """The defect itself: the old check globbed `REPO/migrations/versions`, which does not exist,
    so its revision set was empty no matter what was on disk. Pin the layout it actually uses."""
    assert not (REPO / "migrations").exists(), (
        "a `migrations/` tree now exists; the guard reads alembic.ini's script_location, so "
        "confirm which one Alembic is configured to use before changing anything"
    )
    assert (REPO / "alembic" / "versions").is_dir()
    assert {r.revision for r in _alembic_script().walk_revisions()}, (
        "the script directory resolved to an empty revision set — the same vacuous-pass shape "
        "this test exists to prevent"
    )


def test_the_guard_uses_the_CONFIGURED_tree_not_the_conventional_one(tmp_path):
    """Re-gate finding 5. The first version built a `ScriptDirectory` directly, so it never routed
    the divergent config through `_alembic_script()` — restoring the defective unconditional
    override left every one of these tests green. It now goes through the production helper, and
    asserts both the resolved path and the discovered revisions come from the CONFIGURED tree.
    """
    configured = tmp_path / "canonical"
    (configured / "versions").mkdir(parents=True)
    for revision, down in (("023", None), ("024", "023")):
        (configured / "versions" / f"{revision}_probe.py").write_text(
            f'"""probe"""\nrevision = "{revision}"\ndown_revision = '
            f'{"None" if down is None else repr(down)}\n'
            "branch_labels = None\ndepends_on = None\n"
            "def upgrade():\n    pass\n\n\ndef downgrade():\n    pass\n")
    ini = tmp_path / "alembic.ini"
    ini.write_text(f"[alembic]\nscript_location = {configured}\n")

    resolved = _alembic_script(config_path=ini)
    assert Path(resolved.dir).resolve() == configured.resolve()
    assert {r.revision for r in resolved.walk_revisions()} == {"023", "024"}
    with pytest.raises(AssertionError, match="is stale"):
        assert_024_unbuilt(resolved, _Model("case_id"))


def test_the_helper_resolves_a_relative_script_location_independently_of_cwd(tmp_path):
    """The repo's own config uses a RELATIVE `script_location`, so a helper that resolved it
    against the process cwd would pass or fail by accident depending on where pytest ran."""
    tree = tmp_path / "alembic"
    (tree / "versions").mkdir(parents=True)
    (tree / "versions" / "001_probe.py").write_text(
        '"""probe"""\nrevision = "001"\ndown_revision = None\n'
        "branch_labels = None\ndepends_on = None\n"
        "def upgrade():\n    pass\n\n\ndef downgrade():\n    pass\n")
    ini = tmp_path / "alembic.ini"
    ini.write_text("[alembic]\nscript_location = alembic\n")
    resolved = _alembic_script(config_path=ini)
    assert Path(resolved.dir).resolve() == tree.resolve()


def test_the_production_helper_resolves_the_repo_configured_tree():
    declared = Config(str(REPO / "alembic.ini")).get_main_option("script_location")
    expected = (REPO / declared).resolve() if not Path(declared).is_absolute() else Path(
        declared).resolve()
    assert Path(_alembic_script().dir).resolve() == expected


def _sequenced_callback_attempt() -> dict:
    """A well-formed callback body that ALSO tries to carry the post-024 ordering key."""
    return {
        "case_id": "c1",
        "run_id": "r1",
        "event_id": "e1",
        "decision": "approve",
        "score": 10,
        "gates": dict.fromkeys(GatesBody.model_fields, True),
        "buy_enablement": "enabled",
        "checks": [],
        "decided_at": "2026-08-12T00:00:00+00:00",
        "decision_sequence": 7,
    }


def test_the_publisher_cannot_emit_an_unmodelled_ordering_key():
    """Gate finding 6, closed structurally rather than by inspection.

    Codex added `decision_sequence` to `Pipeline._callback_body` and every check stayed green,
    because the emitter built a plain dict and the guard read a model nobody routed through. The
    enqueue path now goes through `encode_decision_callback`, so an unmodelled key is REFUSED
    before it can reach the outbox — the emitter cannot publish a field the contract does not
    declare, whatever it puts in the dict.
    """
    with pytest.raises(PydanticValidationError):
        schemas.encode_decision_callback(_sequenced_callback_attempt())
    clean = _sequenced_callback_attempt()
    clean.pop("decision_sequence")
    emitted = schemas.encode_decision_callback(clean)
    assert emitted["case_id"] == "c1" and emitted["decided_at"].startswith("2026-08-12")


def test_the_enqueued_body_is_the_encoder_s_output(monkeypatch):
    """The routing itself: whatever `_callback_body` returns, what reaches the outbox is the
    encoder's validated output. Proven by making the emitter hostile and watching the wire."""
    from kyc_tool.orchestration import pipeline as pipeline_module

    source = (SRC / "orchestration" / "pipeline.py").read_text()
    assert "body = encode_decision_callback(body)" in source, (
        "the enqueue path no longer routes through the authoritative encoder"
    )
    # and the encoder is applied AFTER the optional fields are attached, so validation covers the
    # whole body rather than a prefix of it
    encoded_at = source.index("body = encode_decision_callback(body)")
    held_at = source.index('body["enforcement_held"]')
    assert held_at < encoded_at, "enforcement_held is attached after encoding, so it is unvalidated"
    assert pipeline_module.encode_decision_callback is schemas.encode_decision_callback


def test_the_optional_fields_still_survive_encoding():
    """Guard the guard: dropping unmodelled keys must not drop MODELLED optional ones. Both
    `event_sequence` and `enforcement_held` exist precisely so validation preserves them."""
    payload = _sequenced_callback_attempt()
    payload.pop("decision_sequence")
    payload["event_sequence"] = 4
    payload["enforcement_held"] = {"computed_decision": "approve", "reason": "x"}
    emitted = schemas.encode_decision_callback(payload)
    assert emitted["event_sequence"] == 4
    assert emitted["enforcement_held"]["computed_decision"] == "approve"


def test_a_sequenced_wire_version_without_024_fails():
    """Codex's matrix, third case, now aimed at the PUBLISHER's own value (re-gate finding 4).
    Patching a parallel constant in `schemas` proved nothing about what gets persisted."""
    with (
        mock.patch.object(publisher, "_WIRE_VERSION", "sequenced"),
        pytest.raises(AssertionError, match="advertises"),
    ):
        assert_024_unbuilt(_alembic_script(), DecisionCallback)


def test_an_encoder_that_emits_the_sequence_fails_even_with_the_model_clean():
    """Fourth case: the model stays clean and the ENCODER starts emitting. This is the direction
    the old declaration-reading guard was blind to."""
    original = schemas.encode_decision_callback

    def leaking_encoder(payload):
        """An encoder that ACCEPTS the ordering key instead of refusing it."""
        clean = {k: v for k, v in payload.items() if k != "decision_sequence"}
        return {**original(clean), "decision_sequence": payload.get("decision_sequence", 7)}

    with (
        mock.patch.object(schemas, "encode_decision_callback", leaking_encoder),
        pytest.raises(AssertionError, match="accepted `decision_sequence`"),
    ):
        assert_024_unbuilt(_alembic_script(), DecisionCallback)


# ── re-gate finding 3: the ENQUEUE boundary is the serialization authority ─────────────────────
class _CapturingSession:
    """Captures what would be persisted, without a database."""

    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)


def _clean_callback_body() -> dict:
    body = _sequenced_callback_attempt()
    body.pop("decision_sequence")
    return body


def test_enqueue_refuses_an_ordering_key_whatever_the_caller_assembled():
    """Validating in the pipeline sanitized ONE caller's dict. Adding the key after that call, or
    calling enqueue directly, stored it verbatim because the enqueue accepted a raw dict."""
    from kyc_tool.outbox.publisher import enqueue_decision_callback

    poisoned = {**_clean_callback_body(), "decision_sequence": 7}
    with pytest.raises(PydanticValidationError):
        enqueue_decision_callback(_CapturingSession(), body=poisoned, decision_sequence=7)


def test_enqueue_stores_the_validated_body_and_keeps_modelled_optionals():
    """Guard the guard: refusing everything would pass the test above. A clean body must persist,
    with `event_sequence` and `enforcement_held` intact — they exist to survive validation."""
    from kyc_tool.outbox.publisher import enqueue_decision_callback

    body = _clean_callback_body()
    body["event_sequence"] = 4
    body["enforcement_held"] = {"computed_decision": "approve", "reason": "x"}
    session = _CapturingSession()
    enqueue_decision_callback(session, body=body, decision_sequence=9)

    (row,) = session.added
    assert "decision_sequence" not in row.payload_json
    assert row.payload_json["event_sequence"] == 4
    assert row.payload_json["enforcement_held"]["computed_decision"] == "approve"
    # the ordering column is a COLUMN, not a wire field — it may carry the ordinal
    assert row.decision_sequence == 9


def test_row_identity_is_derived_from_the_validated_body():
    """Re-gate-3 finding 2. `case_id`/`run_id` used to be separate keyword arguments, so the
    outbox could account, order, and complete a row under one identity while the wire body named
    another — `ROW row-case row-run / BODY body-case body-run` was Codex's reproduction, and
    several integration tests were doing it silently. The arguments no longer exist: one
    authority, the validated body, and the row follows it.
    """
    import inspect

    from kyc_tool.outbox.publisher import enqueue_decision_callback

    parameters = set(inspect.signature(enqueue_decision_callback).parameters)
    assert parameters == {"session", "body", "decision_sequence"}, (
        "enqueue grew identity arguments again; the mismatch class returns with them"
    )

    body = _clean_callback_body()
    body["case_id"], body["run_id"] = "case-77", "run-77"
    session = _CapturingSession()
    enqueue_decision_callback(session, body=body, decision_sequence=1)
    (row,) = session.added
    assert (row.case_id, row.run_id) == ("case-77", "run-77")
    assert (row.payload_json["case_id"], row.payload_json["run_id"]) == ("case-77", "run-77")


def test_an_unmodelled_field_added_after_the_pipeline_encodes_is_still_refused():
    """Codex's second bypass: encode in the pipeline, then mutate before enqueue. The enqueue is
    the last authority, so the late addition cannot survive."""
    from kyc_tool.outbox.publisher import enqueue_decision_callback

    encoded = schemas.encode_decision_callback(_clean_callback_body())
    encoded["decision_sequence"] = 7  # added AFTER the pipeline's encode
    with pytest.raises(PydanticValidationError):
        enqueue_decision_callback(_CapturingSession(), body=encoded, decision_sequence=7)


# ── re-gate-3 finding 3: strictness reaches every nested wire object ───────────────────────────
NESTED_UNKNOWNS = [
    ("root", lambda b: b.__setitem__("decision_sequence", 7)),
    ("gates", lambda b: b["gates"].__setitem__("future_gate", True)),
    ("checks item", lambda b: b.__setitem__("checks", [
        {"type": "x", "status": "pass", "points": 1, "source": "s", "future_provenance": "p"}])),
    ("enforcement_held", lambda b: b.__setitem__("enforcement_held", {
        "computed_decision": "approve", "reason": "x", "future": "p"})),
]


@pytest.mark.parametrize("label,mutate", NESTED_UNKNOWNS, ids=[c[0] for c in NESTED_UNKNOWNS])
def test_an_undeclared_field_is_refused_at_every_nesting_depth(label, mutate):
    """Root-only strictness left the nested objects permissive: an unknown gate or check field was
    silently DROPPED at the encoder while leaking through any path that skipped it, and
    `enforcement_held` — a plain `dict` — was not even dropping, it was PUBLISHING arbitrary keys.
    Same accepted-here-invisible-there class, one layer down.
    """
    from kyc_tool.outbox.publisher import enqueue_decision_callback

    body = _clean_callback_body()
    mutate(body)
    with pytest.raises(PydanticValidationError):
        enqueue_decision_callback(_CapturingSession(), body=body, decision_sequence=1)


def test_exact_nested_bodies_still_serialize():
    """The positive half, through the same boundary: every declared nested field survives."""
    from kyc_tool.outbox.publisher import enqueue_decision_callback

    body = _clean_callback_body()
    body["checks"] = [{"type": "org_id", "status": "pass", "points": 10, "source": "gleif",
                       "reason_codes": ["OK"]}]
    body["event_sequence"] = 2
    body["enforcement_held"] = {"computed_decision": "approve", "reason": "hold"}
    session = _CapturingSession()
    enqueue_decision_callback(session, body=body, decision_sequence=3)
    (row,) = session.added
    assert row.payload_json["checks"][0]["reason_codes"] == ["OK"]
    assert row.payload_json["enforcement_held"] == {
        "computed_decision": "approve", "reason": "hold"}


def test_the_recorder_is_handed_the_active_wire_version():
    """Re-gate-3 finding 4, second half: the gate's literal proves what `_WIRE_VERSION` IS; this
    proves it is what `_record_attempt` actually RECEIVES. Both call sites must bind the module
    global by name — an edit that passes anything else fails here even if the global is clean."""
    import ast as ast_module

    tree = ast_module.parse((SRC / "outbox" / "publisher.py").read_text())
    handed = []
    for node in ast_module.walk(tree):
        if (isinstance(node, ast_module.Call)
                and isinstance(node.func, ast_module.Attribute)
                and node.func.attr == "_record_attempt"):
            for keyword in node.keywords:
                if keyword.arg == "wire_version":
                    handed.append(ast_module.unparse(keyword.value))
    assert handed and all(value == "_WIRE_VERSION" for value in handed), (
        f"_record_attempt is handed {handed or 'nothing'} as wire_version; the gate's literal "
        "then proves nothing about what gets persisted"
    )
