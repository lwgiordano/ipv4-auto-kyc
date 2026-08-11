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
import hashlib
import math
import re
import time
from pathlib import Path

import pytest
from docs.contracts import ClaimState
from docs.contracts.operations import OPERATIONS
from docs.contracts.signing_example import sign as example_sign
from docs.contracts.wire import WIRE
from pydantic import ValidationError as PydanticValidationError

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
def _rotation_claim_matches_the_enforced_floor():
    """The prose promises a 32-char floor, non-blank ids, and no collision. Prove each is actually
    refused rather than merely described."""
    assert hmac_extra_key_violations({"old": "s" * 32}, "active") == []
    assert any("weak" in v for v in hmac_extra_key_violations({"old": "s" * 31}, "active"))
    assert any("blank" in v for v in hmac_extra_key_violations({"": "s" * 32}, "active"))
    assert any("collides" in v for v in hmac_extra_key_violations({"active": "s" * 32}, "active"))


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
        Callback("c", "r", event_sequence=6), phase=POST_024)
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
    revisions = {p.stem for p in (REPO / "migrations" / "versions").glob("*.py")}
    assert not any(r.startswith("024") for r in revisions), "024 exists; the claim is stale"


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


@verifies("OPS.CUTOVER.PROCEDURES")
def _every_procedure_points_at_a_playbook_that_exists():
    """These claims deliberately do NOT restate steps (see the note in operations.py), which makes
    the pointer load-bearing: a reference to a section that does not exist is worse than the
    summary it replaced, because the operator has nothing to fall back to."""
    deployment = (REPO / "docs" / "DEPLOYMENT.md").read_text()
    headings = [line for line in deployment.splitlines() if line.startswith("#")]
    procedures = OPERATIONS.value("OPS.CUTOVER.PROCEDURES")
    assert {p.name for p in procedures} == {
        "Full maintenance window",
        "Bundle-pinning activation",
        "Migrations 013-023",
    }
    for procedure in procedures:
        document, _, section = procedure.playbook.partition(", ")
        assert (REPO / document).exists(), f"{procedure.name} points at a missing document"
        assert any(section in heading for heading in headings), (
            f"{procedure.name} points at '{section}', which is not a heading in {document}"
        )
        assert procedure.blocks_start, f"{procedure.name} lists nothing that blocks starting"
        assert procedure.when.strip() and procedure.irreversible.strip()
        # no step numbering: a numbered list here IS the summary this claim exists to avoid
        for prerequisite in procedure.blocks_start:
            assert not re.match(r"^\s*(?:step\s*)?\d+[.)]", prerequisite.lower()), (
                f"{procedure.name} is restating numbered steps again"
            )

    by_name = {p.name: p for p in procedures}
    # order inside the prerequisites still matters: the pre-window diagnostic exists to run UNDER
    # the retention suspension, and reading it the other way round makes the diagnostic worthless
    pr7b = [p.lower() for p in by_name["Migrations 013-023"].blocks_start]
    suspended = next(i for i, p in enumerate(pr7b) if "retention is suspended" in p)
    diagnostic = next(i for i, p in enumerate(pr7b) if "pre-window diagnostic" in p)
    assert suspended < diagnostic
    assert any("backup" in p for p in pr7b)
    assert "unconditionally" in by_name["Migrations 013-023"].irreversible.lower()
    # the two controls a step summary kept dropping
    assert any(
        "load balancer" in p.lower() or "trusted path" in p.lower()
        for p in by_name["Full maintenance window"].blocks_start
    )
    assert "pr6 image" in by_name["Bundle-pinning activation"].irreversible.lower().replace(
        "pr6 ", "pr6 "
    ).replace("the pr6 image", "pr6 image")


@verifies("OPS.CUTOVER.OUTBOX_CEILING")
def _outbox_ceiling_cutover_matches_the_shipped_canonical_record():
    """Rendered from the same record DEPLOYMENT, RUNBOOK, and .env.example embed, in order. This
    is the one cutover the registry does restate, because it is GENERATED from the authority
    rather than summarized from prose."""
    documented = OPERATIONS.value("OPS.CUTOVER.OUTBOX_CEILING")
    canonical = cutover.render_cutover(cutover.OUTBOX_MAX_ATTEMPTS_CUTOVER)[1:]  # drop the title
    assert list(documented) == [line.split(". ", 1)[1] for line in canonical]


@verifies("OPS.ROLLBACK.MIGRATION_BOUNDARY")
def _rollback_claim_forbids_the_staging_downgrade_myth():
    text = " ".join(OPERATIONS.value("OPS.ROLLBACK.MIGRATION_BOUNDARY")).lower()
    assert "unconditionally" in text and "including staging" in text
    assert "downgrade-freely" not in text.replace("there is no downgrade-freely rule anywhere", "")


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
