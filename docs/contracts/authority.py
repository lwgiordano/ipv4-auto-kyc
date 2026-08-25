"""Executable authority for every registry claim, and the receipt closure over their text.

Re-audit-6 finding 1. These lanes lived in `tests/unit/test_contract_registry_authority.py`, so
`_top_level_verify` could describe them as "everything a release runs" while the actual release
command ran none of them. Replacing `WIRE.CALLBACK.RECEIVER_TXN` with a receiver contract that
returns 2xx BEFORE committing made its verifier fail and the published contract succeed: a false
counterparty obligation, every outline block in the reviewed order, in the governed PDF.

A proof that only the test suite runs is not a control on the artifact. So the machinery lives
here, in production, and both sides call it: `docs.generators.publication` before it promotes
anything, and the authority suite, which keeps its 226 adversarial tests and now attacks the same
code a release depends on.

Two lanes:

- AUTHORITY_VERIFIERS — one executable verifier per claim id, registered by `@verifies`. Each one
  runs the real system (the signing vector, the receiver reference, the route table, Alembic's
  graph, the parsed spec) and refuses a claim the system contradicts.
- the receipt closure — every registry-owned string a projection can render, plus the display
  declarations that decide how it is drawn, each holding exactly one receipt: bound to a verifier,
  or pinned as reviewed prose or reviewed schema.

`problems()` runs both and returns what a release must refuse over.
"""

import ast
import contextlib
import dataclasses
import hashlib
import html
import math
import re
import time
import types
from collections.abc import Mapping
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import ValidationError as PydanticValidationError
from tests import roadmap

from docs.contracts import ClaimState, playbook
from docs.contracts import wire as wire_module
from docs.contracts.operations import OPERATIONS
from docs.contracts.signing_example import sign as example_sign
from docs.contracts.wire import WIRE
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


@contextlib.contextmanager
def raises(expected):
    """The refusal these verifiers assert, without importing a test framework.

    Seven of them prove that the running system REFUSES something — an unknown release source, a
    dev worker in production, a malformed rotation key. That is authority, not test scaffolding,
    so it moved here with them and this is the two lines of `pytest.raises` they used.
    """
    caught = types.SimpleNamespace(value=None)
    try:
        yield caught
    except expected as exc:  # noqa: B902 — the refusal this verifier exists to prove
        caught.value = exc
        return
    raise AssertionError(f"expected {getattr(expected, '__name__', expected)}, nothing was raised")


REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "kyc_tool"
def hardened(**overrides) -> Settings:
    """A production-shaped Settings, which is a DESCRIPTION OF PRODUCTION and therefore
    authority rather than test scaffolding — several verifiers below prove what production
    refuses, and they cannot do that without being able to build one. One definition:
    `tests/unit/test_production_config.py` imports it from here."""
    base = dict(
        environment="production",
        auth_disabled=False,
        platform_hmac_secret="s" * 40,
        platform_callback_url="https://platform.example/kyc",
        object_store="s3",
        s3_bucket="kyc-evidence",
        ocr_engine="tesseract",
        email_provider="ses",
        adapters_profile="real",
        read_auth_required=True,
        ui_enabled=False,
        ui_admin_token="t" * 32,
        # HMAC v2 (PR 5a): split secrets + key_ids, both sunset dates, window.
        hmac_inbound_key_id="kyc-platform-1",
        hmac_inbound_secret="i" * 40,
        hmac_outbound_key_id="kyc-tool-1",
        hmac_outbound_secret="o" * 40,
        hmac_v1_inbound_sunset_at="2026-09-01T00:00:00Z",
        hmac_v1_outbound_sunset_at="2026-10-01T00:00:00Z",
        hmac_v1_observation_window_days=14,
    )
    base.update(overrides)
    return Settings(**base)


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

    # The digest the document prints must be what the DOCUMENTED COMMAND produces: sha256 over
    # the complete file bytes, exactly as `shasum -a 256` reports it. The previous form digested
    # the runnable body only and quoted it inside the file's own header, so a reader running the
    # printed command got a different value and concluded the file was tampered with — a check on
    # the parts was standing in for a check on the documented procedure.
    assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact_digest()
    assert artifact_digest() not in on_disk, (
        "the file quotes its own digest again — that value cannot survive being written into "
        "the text it covers"
    )

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
    # re-audit `1826661..b5c7a83` finding 7: names are not semantics — the typed
    # preconditions/effects are simulated and the whole normative surface is pinned here
    _rotation_closed_surface_ok()
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
    # re-audit `1826661..b5c7a83` finding 6: the extension point is the RUNTIME registry — the
    # verifier resolves the same slot every consumer does; anything but a MissingCapability
    # means a provider REGISTERED, and this claim must move with it
    from kyc_tool import capabilities

    resolved = capabilities.resolve(capabilities.SLOT_RETIREMENT_EVIDENCE)
    assert isinstance(resolved, capabilities.MissingCapability), (
        "a retirement authority is registered; the claim, gates, and ROADMAP unit must move "
        "in the same change"
    )
    assert resolved.roadmap_unit == "PR 5c"
    # finding 7: the procedure the gates hang off is itself simulated and pinned
    _rotation_closed_surface_ok()

    # the reserved, unbuilt ROADMAP unit — reserved WITHOUT a migration or a shipped/pending state
    row = next((r for r in roadmap.records() if r[0] == "PR 5c"), None)
    assert row is not None, "ROADMAP §C reserves no PR 5c unit for the retirement evidence"
    _unit, state, revisions = row.unit, row.state, row.revisions
    assert state == "future" and revisions == (), (
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

    documented = tuple(row.name for row in WIRE.value("WIRE.INGEST.HEADERS"))
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
        with raises(HTTPException) as excinfo:
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
    table = {row.name: row for row in WIRE.value("WIRE.EVENT.TABLE")}
    assert set(table) == set(EventType.__args__) == set(PAYLOAD_MODELS)

    for event_type, model in PAYLOAD_MODELS.items():
        required = tuple(sorted(f for f, i in model.model_fields.items() if i.is_required()))
        optional = tuple(sorted(f for f, i in model.model_fields.items() if not i.is_required()))
        row = table[event_type]
        assert tuple(sorted(row.required)) == required, f"{event_type} required fields"
        assert tuple(sorted(row.optional)) == optional, f"{event_type} optional fields"
        # the derived display cells are what the page prints; hold them to the same fields
        assert row.required_display == (", ".join(row.required) or "(none)")
        assert row.optional_display == (", ".join(row.optional) or "(none)")
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
    _receiver_surface_ok()
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
@verifies("WIRE.CALLBACK.VALIDATION")
def _receiver_validation_rules_are_executable_and_precede_classification():
    """R-audit-3 finding 6: the published contract must order validation BEFORE history/table
    consultation, and each published invalid-input row must be a real refusal — the verifier
    constructs every rule's specimen and the reference receiver must HOLD it, so the contract
    and the executable boundary cannot drift."""
    from docs.contracts import receiver_reference as receiver

    _receiver_surface_ok()  # the rules AND their specimen identities are pinned surface
    claim_ids = [c.id for c in WIRE.claims]
    validation_index = claim_ids.index("WIRE.CALLBACK.VALIDATION")
    assert validation_index < claim_ids.index("WIRE.CALLBACK.RECEIVER_TXN")
    assert validation_index < claim_ids.index("WIRE.CALLBACK.EFFECTIVENESS")
    assert validation_index < claim_ids.index("WIRE.CALLBACK.RELEASE")
    rules = WIRE.value("WIRE.CALLBACK.VALIDATION")
    assert len(rules) >= 6, "the validation table lost rows"
    for rule in rules:
        assert rule.invalid_input.strip() and rule.disposition.strip()
        assert "HOLD" in rule.disposition.upper(), (
            f"{rule.invalid_input[:40]}: the disposition no longer says HOLD"
        )
        state, callback, kwargs = rule.specimen()
        with raises(receiver.ReceiverIntegrityError):
            receiver.decide(state, callback, **kwargs)
    # the transaction steps consult validation before the table, and acknowledgement is
    # qualified to VALID callbacks
    steps = " ".join(WIRE.value("WIRE.CALLBACK.RECEIVER_TXN"))
    validate_at = steps.find("Validate the callback")
    record_at = steps.find("record it in your accepted")
    assert validate_at != -1 and record_at != -1 and validate_at < record_at, (
        "the transaction steps no longer validate before recording"
    )
    assert "VALID" in steps
    published = WIRE.value("WIRE.CALLBACK.ACK_VS_APPLY").text
    assert "VALID callback" in published and "always acknowledge" not in published
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
    assert legend_digest == "44365d0e7c62b6f5", (
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
    _receiver_surface_ok()
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
    with raises(rr.UnknownSourceError):
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
    obligations = _live_obligation_ids(_live_blocker_section())
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
    from kyc_tool import capabilities

    resolved = capabilities.resolve(capabilities.SLOT_ANSWER_ARTIFACT)
    assert isinstance(resolved, capabilities.MissingCapability), (
        "an answer-artifact authority is registered; the claim, the gate, and ROADMAP "
        "PR 7b-inputs must move in the same change"
    )
    assert resolved.roadmap_unit == "PR 7b-inputs"
    published = WIRE.value("WIRE.ORDERING.OBLIGATION_STATE").text
    assert "RESOLVE" in published and "has not shipped" in published, (
        "the published statement no longer tells the reader that no reply can resolve an "
        "obligation"
    )

    # the accounted role inventory mirrors ProcessRole exactly (the plan.PLAN_ROLES bind, again):
    # a new executable role cannot appear without the O4 matrix demanding a stance on it.
    assert set(wire_module.ACCOUNTED_PROCESS_ROLES) == {role.value for role in ProcessRole}
    assert len(wire_module.ACCOUNTED_PROCESS_ROLES) == len(ProcessRole)

    # the reserved, unbuilt ROADMAP unit for the artifact schema
    row = next((r for r in roadmap.records() if r[0] == "PR 7b-inputs"), None)
    assert row is not None, "ROADMAP §C reserves no PR 7b-inputs unit for the answer artifacts"
    _unit, state, revisions = row.unit, row.state, row.revisions
    assert state == "future" and revisions == (), (
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
    with raises(NotImplementedError):
        _make_ocr_engine("tesseract")
    with raises(NotImplementedError):
        make_email_sender("ses")
    with raises(NotImplementedError):
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
    with raises(ProductionConfigError) as excinfo:
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
    for row in OPERATIONS.value("OPS.CONFIG.DEFAULTS"):
        assert int(getattr(settings, row.name)) == row.default, f"{row.name} default moved"
        # the derived cells are what the page prints; hold them to the same source
        assert row.variable == f"KYC_{row.name.upper()}"
        assert row.display == str(row.default)
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
    published = OPERATIONS.value("OPS.CONFIG.HMAC_SET_RULE").text
    assert f"at least {_MIN_HMAC_SECRET_LEN} characters" in published, published
    assert "timezone-aware ISO-8601" in published
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
    with raises(PydanticValidationError):
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
def _parse_atx_heading(line: str):
    """CommonMark ATX recognition, ONE rule for every consumer (R-audit-3 finding 7): up to
    three leading spaces, 1-6 hashes, required space/EOL — returning (level, visible label).
    Returns None for non-headings. Four spaces is an indented code block, never a heading."""
    indent = len(line) - len(line.lstrip(" "))
    candidate = line.lstrip(" ") if indent <= 3 else ""
    if not candidate.startswith("#"):
        return None
    depth = len(candidate) - len(candidate.lstrip("#"))
    if 0 < depth <= 6 and candidate[depth:depth + 1] in (" ", ""):
        return depth, candidate[depth:].strip()
    return None
def _atx_level(line: str) -> int:
    """The ATX heading level of `line`, or 0 — a thin view over _parse_atx_heading."""
    parsed = _parse_atx_heading(line)
    return parsed[0] if parsed else 0
_FENCE_OPEN = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")
def _markdown_blocks(text: str) -> list:
    """ONE CommonMark block model for every section reader (R-audit-3 finding 7): backtick AND
    tilde fences with real opener-length/indent/closer rules, headings nonexistent inside
    fences, and EVERY fence balanced — an unmatched fence is a ValueError, never silently
    swallowed content. Nodes: ("fence", char, info, [lines]) and ("line", text)."""
    nodes, open_fence = [], None  # open_fence = (char, length, info, lines)
    for line in text.split("\n"):
        if open_fence is not None:
            char, length, info, lines = open_fence
            stripped = line.strip()
            if (len(line) - len(line.lstrip(" ")) <= 3 and stripped
                    and set(stripped) == {char} and len(stripped) >= length):
                nodes.append(("fence", char, info, lines))
                open_fence = None
            else:
                lines.append(line)
            continue
        opened = _FENCE_OPEN.match(line)
        if opened:
            char, info = opened.group(2)[0], opened.group(3).strip()
            if char == "`" and "`" in info:
                raise ValueError(f"a backtick fence info string may not contain backticks: "
                                 f"{line!r}")
            open_fence = (char, len(opened.group(2)), info, [])
            continue
        nodes.append(("line", line))
    if open_fence is not None:
        raise ValueError(
            f"an unbalanced {open_fence[0]!r} fence (info {open_fence[2]!r}) runs to the end "
            "of the section — it can swallow everything after it; refuse the document"
        )
    return nodes
def _section_bytes(ref, root=None) -> str:
    """The EXACT text of the referenced section, HEADING INCLUDED, bounded by the ONE shared
    CommonMark block model (R-audit-3 finding 7): the heading must exist OUTSIDE any fence (a
    section wrapped in a ```/~~~ fence renders as code — it has no heading to reference), its
    level comes from _parse_atx_heading (never from byte zero, so a 1-3 space indented heading
    bounds and displays exactly), fences balance, and the section runs to the next real heading
    of the same or higher level. Byte-exact; CRLF→LF is the ONLY normalization."""
    text = ((root or REPO) / ref.path).read_bytes().decode("utf-8").replace("\r\n", "\n")
    _markdown_blocks(text)  # whole-document fence balance, before any bounding
    lines = text.split("\n")
    fence_lines = set()
    index = 0
    for node in _markdown_blocks(text):
        if node[0] == "line":
            while lines[index] != node[1]:
                fence_lines.add(index)
                index += 1
            index += 1
    fence_lines.update(range(index, len(lines)))
    matches = [i for i, line in enumerate(lines)
               if line == ref.heading and i not in fence_lines]
    assert len(matches) == 1, (
        f"heading {ref.heading!r} matches {len(matches)} real (non-fence) lines; the "
        "reference must name exactly one section by its exact heading line"
    )
    parsed = _parse_atx_heading(ref.heading)
    assert parsed is not None, f"{ref.heading!r} is not a CommonMark ATX heading"
    level = parsed[0]
    start = matches[0] + 1
    end = len(lines)
    for i in range(start, len(lines)):
        if i in fence_lines:
            continue
        depth = _atx_level(lines[i])
        if 0 < depth <= level:
            end = i
            break
    return "\n".join([ref.heading, *lines[start:end]])
def _section_body(section_text: str) -> str:
    """The section WITHOUT its heading line — what command parsing and prose quoting run over."""
    return section_text.partition("\n")[2]
def _join_operator_lines(block: list) -> list:
    joined = []
    for raw in block:
        if joined and joined[-1].endswith("\\"):
            joined[-1] = joined[-1][:-1].rstrip() + " " + raw.strip()
        elif raw.strip():
            joined.append(raw.strip())
    return joined
def _section_commands(section_text: str) -> list:
    """Every MARKED operator command in the section, in source order, as exact line strings.

    The marking convention is the document's: operator commands live in operator fences
    (backtick or tilde; one command per line; a trailing backslash joins the next line with one
    space) and in ``double-backtick`` inline spans. Bytes are compared exactly — an NBSP or
    raw-newline lookalike is not the reviewed command — and any root is inventoried. What is
    NOT marked is not thereby trusted: _unmarked_instruction_problems refuses command-shaped
    content everywhere else (R-audit-3 finding 8)."""
    commands = []
    for node in _markdown_blocks(section_text):
        if node[0] == "fence":
            if node[2] == "operator":
                commands.extend(_join_operator_lines(node[3]))
        else:
            commands.extend(
                re.findall(r"(?<!`)``([^`]+?)``(?!`)", node[1]))
    return commands
def _section_fence_infos(section_text: str) -> list:
    """The info string of every fence (backtick AND tilde) in the section, in order."""
    return [node[2] for node in _markdown_blocks(section_text) if node[0] == "fence"]
_COMMANDLIKE_ROOTS = frozenset({
    "python", "python3", "alembic", "rm", "psql", "aws", "curl", "sha256sum", "bash", "sh",
    "docker", "git", "pip", "kubectl", "systemctl", "dropdb", "pg_dump", "pg_restore",
    # R-audit-11 finding 1: the witnessed maintenance roots carry ROOT-OWNED semantics —
    # root + ANY operand is command-shaped, so a relative or hyphenated operand
    # (`pkill kyc-worker`, `mv kyc-data backup-data`) cannot slip the structural floor.
    # This set is CLOSED and grows only by audit witness; the structural rules (paths,
    # flags, snake_case, `=`, metacharacters, imperative leads) remain the
    # vocabulary-independent floor beneath it.
    "chmod", "chown", "pkill", "mv", "tee", "cp", "dd", "truncate"})
_SHELL_METACHAR = re.compile(r"\$\(|&&|\|\||<\(")
_IMPERATIVE_LEAD = re.compile(
    r"(?i)(?:^|[.;:!?]\s+)(?:then\s+|first\s+|now\s+)?(?:run|execute|invoke)\s+"
    r"(?!the\b|a\b|an\b|it\b|this\b|that\b|these\b|those\b|them\b|your\b|its\b|"
    r"each\b|every\b|all\b|any\b|both\b|one\b|only\b|per\b|as\b|with\b|in\b|on\b|"
    r"under\b|from\b|at\b|against\b|after\b|before\b|during\b|once\b|again\b|"
    r"twice\b|\u2039)"
    r"\S+\s+\S")
_STRICT_METACHAR = re.compile(r"\$\(|&&|\|\||<\(|;\s|\s>\s")
_ARGV_TOKEN = re.compile(r"[A-Za-z0-9._/:=@~+-]+")
_ARGV_BARE_WORD = re.compile(r"[a-z][a-z0-9._-]*")
_ARGV_FUNCTION_WORDS = frozenset({
    "the", "a", "an", "it", "this", "that", "these", "those", "them", "they", "your",
    "its", "each", "every", "all", "any", "both", "one", "only", "per", "as", "with",
    "in", "on", "under", "from", "at", "against", "after", "before", "during", "once",
    "again", "twice", "of", "for", "to", "and", "or", "nor", "but", "if", "when",
    "while", "than", "then", "so", "such", "via", "into", "onto", "over", "between",
    "within", "without", "across", "along", "behind", "beside", "beyond", "by", "near",
    "toward", "towards", "upon", "since", "until", "till", "above", "below", "off",
    "out", "up", "down", "not", "no", "is", "are", "was", "were", "be", "been", "being",
    "am", "has", "have", "had", "do", "does", "did", "can", "could", "may", "might",
    "must", "shall", "should", "will", "would", "there", "here"})
def _command_shaped(text: str, strict: bool = False, sentence_start: bool = True) -> bool:
    """Would a reader read `text` as something to RUN? DEFENSE IN DEPTH, not the authority
    (R-audit-13 finding 1): the reviewed playbook sections are byte-identical PROJECTIONS
    of typed body sources, so certification never rests on this classifier — a doc-only
    addition of ANY shape breaks projection identity first. What this heuristic models,
    structurally and vocabulary-free: shell metacharacters, dash-flag tokens, imperative
    leads, known roots beside an operand, the closed argv grammar (paths in runs,
    machine-shaped operand pairs), and the sentence-position rules (lowercase
    function-word-free leads, heading/list roles, colon clauses, single-token list items).
    Honestly OUTSIDE its scope by design: a capitalized imperative with plain-word operands
    reads as an English sentence at this granularity and is left to projection identity. A
    LONE flag or path token is a mention — you cannot run a flag. Unicode whitespace
    lookalikes split like whitespace, so an NBSP variant is still command-shaped."""
    tokens = text.split()
    if not tokens:
        return False
    if (_STRICT_METACHAR if strict else _SHELL_METACHAR).search(text):
        return True
    if _IMPERATIVE_LEAD.search(text):
        return True
    if tokens[0].lower() in _COMMANDLIKE_ROOTS and len(tokens) >= 2:
        return True
    if len(tokens) >= 2:
        for token in tokens:
            bare = token.strip(".,;:()`'\"")
            # a dash-flag token is executable fingerprint whatever the first word is
            # (`/bin/rm -rf`, `find -delete`, `mytool --wipe-everything`); an absolute path
            # ALONE is still a mention, but a path INSIDE a token run refuses below (r1) —
            # API routes like /v1/cases/{id}/events keep their `{id}` placeholders, which
            # break the argv charset; other path mentions belong in backtick spans.
            if re.fullmatch(r"-{1,2}[A-Za-z][A-Za-z0-9-]*(=\S*)?", bare):
                return True
        for i, token in enumerate(tokens[:-1]):
            bare = token.strip(".,;:()`'\"")
            # a known root — bare or by absolute path — followed by ANY operand is an
            # instruction wherever it sits (R-audit-5 finding 2: `dropdb kyc_prod`,
            # `systemctl restart kyc`, `kubectl delete pod` carry no flag and no path, and
            # certified as reviewed prose)
            operand = tokens[i + 1].strip(".,;:()`'\"")
            if (bare.lower() in _COMMANDLIKE_ROOTS
                    or bare.rpartition("/")[2].lower() in _COMMANDLIKE_ROOTS) and operand:
                return True
        # the closed argv grammar (R-audit-7 finding 2), over each maximal run of
        # consecutive argv-charset tokens — a token carrying a comma is LIST syntax, not
        # an argv word, and terminates its run (argv arguments are space-separated):
        # (r1) a run carrying an absolute-path token is runnable — a path ALONE stays a
        # mention (API routes with `{id}` placeholders break the charset and never join
        # runs; other path mentions belong in backtick spans); (r2) a lowercase bare word
        # adjoining a machine-shaped operand (snake_case, an `=`-assignment, or a leading
        # `/` — each longer than the bare punctuation prose uses for "either/or" and
        # "equals") is a root beside its argument whatever its vocabulary —
        # an UNKNOWN root beside a snake_case operand refuses with no root list at all
        # (the closed root set above additionally owns ANY-operand shapes for witnessed
        # roots — R-audit-11 finding 1 — so the two authorities overlap, never compete).
        runs, run = [], []
        for token in tokens:
            bare = token.strip(".;:()`'\"")
            if bare and "," not in token and _ARGV_TOKEN.fullmatch(bare):
                run.append(bare)
            else:
                if len(run) >= 2:
                    runs.append(run)
                run = []
        if len(run) >= 2:
            runs.append(run)
        for run in runs:
            if any(t.startswith("/") and len(t) > 1 for t in run):
                return True
            for word, operand in zip(run, run[1:], strict=False):
                if (_ARGV_BARE_WORD.fullmatch(word)
                        and word not in _ARGV_FUNCTION_WORDS
                        and len(operand) > 1
                        and ("_" in operand or "=" in operand
                             or operand.startswith("/"))):
                    return True
        # the SENTENCE-INITIAL naked-command rule (R-audit-12 finding 1: every root list is
        # specimen-grown — kill, killall, sudo, rsync, scp, ssh, make certified — so this
        # shape carries NO vocabulary at all). Written English capitalizes its sentence
        # starts and reaches a function word almost immediately; a pasted command does
        # neither. At each sentence start — the text's start when the CALLER says this text
        # begins a sentence (the prose scanner tracks paragraph boundaries and the previous
        # line's terminal punctuation; a hard-wrap continuation does NOT begin one), plus
        # after ./!/? within the text, list and quote markers skipped — a leading
        # comma-free, function-word-free argv run of >=2 tokens whose FIRST token is a
        # plain lowercase bare word is a command whatever its vocabulary. A clause that
        # reaches "the"/"of"/"are" inside two tokens — "run the restore CLI", "workers are
        # stopped before the window" — never qualifies, and a capitalized imperative
        # ("Confirm both pools...") is an English sentence, exactly as always.
        # R-audit-13 finding 1 widened this from lowercase multi-token runs to the five
        # boundary dimensions the audit proved unmodeled — executable-token grammar
        # (hyphenated first tokens), rendered block role (heading markers join the skip
        # set, so a heading's VISIBLE text is judged), clause boundary (a colon opens a
        # clause inside a line), case (a capitalized word beside a machine-shaped or
        # hyphenated operand), and arity (a list item that is nothing but one bare word).
        # Still no vocabulary anywhere.
        sentences = re.split(r"(?<=[.!?:])\s+", text)
        if not sentence_start:
            sentences = sentences[1:]
        for sentence in sentences:
            stokens = sentence.split()
            marker_led = False
            while stokens and re.fullmatch(
                    r"[-*+>]|#{1,6}|\d+[.)]|\([a-z0-9]{1,2}\)", stokens[0]):
                marker_led = marker_led or stokens[0].lstrip("#") != ""
                stokens = stokens[1:]
            lead: list = []
            for token in stokens:
                bare = token.strip(".;:()`'\"")
                if (not bare or "," in token or not _ARGV_TOKEN.fullmatch(bare)
                        or bare.lower() in _ARGV_FUNCTION_WORDS):
                    break
                lead.append(bare)
            if len(lead) >= 2 and re.fullmatch(r"[a-z][a-z0-9._-]*", lead[0]):
                return True
            if (marker_led and len(stokens) == 1 and len(lead) == 1
                    and re.fullmatch(r"[a-z][a-z0-9._-]*", lead[0])):
                return True
    return False
def _rendered_inline(text: str) -> str:
    """What a CommonMark reader SEES for inline prose (R-audit-8 finding 2: `pk**ill**`
    renders as `pkill`, `&#47;` as `/`; R-audit-9 finding 1: `pk<span>ill</span>` and
    `pk<!--hide-->ill` render as `pkill` — tags and comments do not display). Order
    mirrors the spec: raw HTML (comments, then quote-aware tags) vanishes first, emphasis
    and links resolve next, and entities decode LAST on the parsed text, so a decoded
    `&#60;` or `&#42;` is a literal character and can never become new markup. Star
    emphasis binds intraword; underscore emphasis does not (CommonMark flanking), so
    `kyc_worker` keeps its underscores. Code spans are the caller's lane and never reach
    here.

    Backslash escapes come FIRST of all (R-audit-10 finding 1: `\\/` renders as `/`,
    `kyc\\_worker` as `kyc_worker`, and the reviewer saw backslash-broken non-command
    tokens): a backslash before ASCII punctuation renders the character LITERALLY and
    suppresses any markup role it had — `\\*` opens no emphasis, `\\<` opens no tag — so
    each escaped character is parked in a private-use placeholder before any parsing and
    restored as the bare character after entity decoding. A backslash before anything
    else is a visible backslash and stays. (A private-use codepoint already in the source
    would be restored as punctuation — that direction only REVEALS more to the scanner,
    never hides.)"""
    text = re.sub(r"\\([!-/:-@\[-`{-~])",
                  lambda m: chr(0xE000 + ord(m.group(1))), text)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"</?[A-Za-z!](?:\"[^\"]*\"|'[^']*'|[^>])*>", "", text)
    previous = None
    while previous != text:
        previous = text
        # links and images render as their visible text: inline `[text](url)`, then the
        # REFERENCE family — full `[text][label]`, collapsed `[text][]`, and shortcut
        # `[text]` — and their `![...]` image forms. A defined reference renders a clean
        # link showing `text`; matching how the inline form is already handled closes the
        # whole link class (self-audit extension of the R9 rendered-review seam).
        text = re.sub(r"!?\[([^\]]*)\]\([^()\s]*(?:\s+\"[^\"]*\")?\)", r"\1", text)
        text = re.sub(r"!?\[([^\]]*)\]\[[^\]]*\]", r"\1", text)
        text = re.sub(r"!?\[([^\]]*)\](?!\[|\()", r"\1", text)
        text = re.sub(r"\*\*(?=\S)([^*]+?)(?<=\S)\*\*", r"\1", text)
        text = re.sub(r"\*(?=[^\s*])([^*]+?)(?<=[^\s*])\*", r"\1", text)
        text = re.sub(r"(?<![A-Za-z0-9_])__(?=\S)([^_]+?)(?<=\S)__(?![A-Za-z0-9_])",
                      r"\1", text)
        text = re.sub(r"(?<![A-Za-z0-9_])_(?=[^\s_])([^_]+?)(?<=[^\s_])_(?![A-Za-z0-9_])",
                      r"\1", text)
    text = html.unescape(text)
    return re.sub(r"[-]", lambda m: chr(ord(m.group(0)) - 0xE000), text)
def _unmarked_instruction_problems(section_text: str) -> list:
    """Command-shaped content OUTSIDE operator nodes (R-audit-3 finding 8): a single-backtick
    span, an example fence, or plain prose carrying something runnable is a located refusal —
    an author cannot self-classify a dangerous command as a mention or an example. Prose is
    judged as RENDERED (R-audit-8 finding 2); fence and span content is literal for a
    reader, so it is scanned exactly as written."""
    problems = []
    # sentence-position tracking for the naked-command rule (R-audit-12 finding 1): a line
    # begins a sentence when it opens a block (section start, after a fence, after a
    # heading, after a blank) or when the previous prose line ended with terminal
    # punctuation. A hard-wrap continuation line does NOT — its first word is the middle
    # of an English sentence, and treating wraps as starts drowned the scan in prose.
    starts_sentence = True
    for node in _markdown_blocks(section_text):
        if node[0] == "fence":
            if node[2] != "operator":
                for line in node[3]:
                    if _command_shaped(line, strict=True):
                        problems.append(
                            f"a {node[2] or 'bare'} fence carries a command-shaped line "
                            f"outside the operator lane: {line.strip()[:70]!r}")
            starts_sentence = True
            continue
        line = re.sub(r"(?<!`)``[^`]+?``(?!`)", "\u2039span\u203a", node[1])  # reviewed
        for span in re.findall(r"(?<!`)`([^`]+?)`(?!`)", line):
            # a span is a mid-prose MENTION — it never occupies sentence position
            if _command_shaped(span, sentence_start=False):
                problems.append(
                    f"a single-backtick span is command-shaped — a mention cannot be "
                    f"runnable: {span[:70]!r}")
        # prose is judged as RENDERED (R-audit-8 finding 2): code spans are the reader's
        # literal lane and were lifted out above; everything else renders before review.
        prose = _rendered_inline(re.sub(r"(?<!`)`[^`]+?`(?!`)", "\u2039span\u203a", line))
        if _command_shaped(prose, sentence_start=starts_sentence):
            problems.append(
                f"prose carries a command-shaped instruction outside any marked node: "
                f"{prose.strip()[:70]!r}")
        stripped = prose.strip()
        starts_sentence = (not stripped or stripped.endswith((".", "!", "?", ":"))
                           or _parse_atx_heading(node[1]) is not None)
    return problems
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
    # R-audit-3 finding 8: review is not opt-in — command-shaped content outside the operator
    # lane (mentions, example fences, plain prose) is refused, not skipped.
    problems.extend(_unmarked_instruction_problems(_section_body(section)))
    return problems
PROCEDURE_DEFINITION_PINS = {
    "PR 5b full maintenance window": "74dc08a643bcc640",
    "Bundle-pinning activation": "1906bcffd7980305",
    "Migrations 013-023": "4447ddc48c2844ae",
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
        # R-audit-14 finding 1: the document section is what RENDERING the typed body
        # produces — ordered Narrative and OperatorInstruction records, the operator lane
        # emitted only from typed Command records, the inline marked lane declared on the
        # Narrative that prints it. R-audit-13's `body_source` was a second hand-authored
        # Markdown mirror whose PATH alone was pinned, so a coherent dual edit published
        # an untyped instruction with no pin change; now there is ONE source, ONE
        # renderer, and every byte of every block sits inside the definition pin below —
        # so any source edit forces the one deliberate re-pin a reviewer signs. The prose
        # classifier stays as DEFENSE IN DEPTH over the same bytes, never the authority.
        assert ref.body, f"{procedure.name}: the ref carries no typed body"
        assert section == ref.rendered_body, (
            f"{procedure.name}: the document section is not the typed body projection; "
            "author the typed body in docs/contracts/playbook_bodies.py, never the "
            "document"
        )
        # R-audit-15 finding 1: construction and verification consume the SAME real
        # CommonMark block tree. Every code block a reader gets from the published
        # section — at ANY container depth, fenced or indented or raw <pre> — must be
        # exactly an OperatorInstruction's fence, in order. A `>`-nested operator fence,
        # an indented block, or a raw <pre> is a code block to the reader and therefore
        # an executable lane; there is no depth at which one hides from this.
        from docs.contracts import body as body_model

        published = body_model.code_blocks(section)
        typed = [("fence", "operator", "\n".join(block.rendered().split("\n")[1:-1]) + "\n")
                 for block in ref.body
                 if type(block) is body_model.OperatorInstruction]
        assert published == typed, (
            f"{procedure.name}: the section's code blocks are not exactly its typed "
            f"operator lane.\n  published: {[(k, i) for k, i, _ in published]}\n"
            f"  typed:     {[(k, i) for k, i, _ in typed]}"
        )
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
ALL_CLAIM_IDS = frozenset(WIRE.ids) | frozenset(OPERATIONS.ids)
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
_BLOCKER_ITEM_HEAD = re.compile(r"- \*\*([^*]+)\*\*")
def _live_obligation_ids(live: str) -> list[str]:
    """The blocker list's obligation identifiers, ORDERED, read STRUCTURALLY (re-audit
    `1826661..b5c7a83` finding 10 — the previous reader searched for its chosen bold syntax,
    so a top-level `- O5 — ...` item was invisible rather than an input). The section is its
    heading, an unindented prose preamble, then top-level list items; EVERY item must begin
    with exactly one bold `O<n>` identifier, and a bullet outside that grammar, a heading
    inside the section, an unindented line after the list starts, an obligation-shaped bold
    token outside an item head, and a duplicate or malformed identifier are each ERRORS —
    never non-input. Multiplicity and completeness stay checked (gate finding 11: O10 read
    complete, duplicates refused)."""
    # R-audit-4 finding 6: `O&#53;` renders as O5 to a reader; an HTML comment can split a
    # token. The obligation-bearing section is PLAIN by contract — entity references and HTML
    # comments are refused outright, before any scan that could be blinded by them.
    if re.search(r"&#\d+;|&#x[0-9A-Fa-f]+;|&[A-Za-z][A-Za-z0-9]*;|<!--", live):
        raise ValueError(
            "entity or comment obfuscation in the live blocker section — the section is "
            "plain text by contract; write the obligation as a top-level bold-id item"
        )
    lines = live.split("\n")
    if not lines or _atx_level(lines[0]) == 0:
        raise ValueError("the live blocker text does not start at its section heading")
    body = lines[1:]
    item_starts = [i for i, line in enumerate(body) if line.startswith("- ")]
    preamble_end = item_starts[0] if item_starts else len(body)
    for line in body[:preamble_end]:
        if _atx_level(line):
            raise ValueError(f"blocker-shaped heading inside the live section: {line!r}")
        if line.strip() and line.startswith((" ", "\t")):
            raise ValueError(f"indented text outside any blocker item: {line!r}")
        # R-audit-3 finding 12: obligation-shaped text hides in the trusted preamble in ANY
        # syntax — plain, numbered, blockquoted, bolded. A bare O<n> token in the preamble is
        # an error outright; obligations exist only as top-level bold-id items.
        if re.search(r"\bO[1-9][0-9]*\b", line):
            raise ValueError(
                f"obligation-shaped text outside the blocker items: {line.strip()[:70]!r}")
    ids = []
    for n, start in enumerate(item_starts):
        end = item_starts[n + 1] if n + 1 < len(item_starts) else len(body)
        for continuation in body[start + 1:end]:
            if _atx_level(continuation):
                raise ValueError(
                    f"blocker-shaped heading inside the live section: {continuation!r}")
            if continuation.strip() and not continuation.startswith((" ", "\t")):
                raise ValueError(
                    f"unstructured top-level line inside the blocker list: {continuation!r}")
        item = "\n".join(body[start:end])
        head = _BLOCKER_ITEM_HEAD.match(item)
        if head is None:
            raise ValueError(
                f"top-level blocker item outside the bold-id grammar: {item[:70]!r}")
        token = re.fullmatch(r"(O[0-9]+)(?:[^0-9A-Za-z].*)?", head.group(1), re.DOTALL)
        if not token or not re.fullmatch(r"O[1-9][0-9]*", token.group(1)):
            raise ValueError(
                f"malformed obligation identifier {head.group(1)!r} in the live blocker list")
        if re.search(r"\*\*\s*O[0-9]", item[head.end():]):
            raise ValueError(
                f"obligation-shaped bold token beyond its item head: {item[:70]!r}")
        ids.append(token.group(1))
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise ValueError(f"duplicate obligation identifiers {sorted(duplicates)}")
    return ids
def _live_blocker_section() -> str:
    """EXACTLY the live `## Open blockers` section, the slice the pending-inputs verifier
    reads — REDs mutate this text and run the same reader."""
    spec = (REPO / ".agents" / "superpowers" / "specs"
            / "2026-07-22-pr7b-activation-platform-ordering-design.md").read_text()
    live = spec[spec.index("## Open blockers"):]
    return live[: live.index("\n## ", 1)] if "\n## " in live[1:] else live
ROTATION_PROFILE_PINS = {
    "INBOUND": ("deploy_rotation_entry", "confirm_both_accepted", "switch_signer",
                "prove_old_id_quiet", "promote_then_remove"),
    "OUTBOUND": ("accept_old_and_new", "hard_stop_attest_zero", "deploy_sole_new_signer",
                 "confirm_new_key_arrivals", "retire_old_key"),
}
ROTATION_SURFACE_PIN = "67139a6b27e53774"
ROTATION_RATIONALES_PIN = "fbe2e74488509e50"
def _rotation_closed_surface_ok() -> None:
    """Asserted by BOTH assembled rotation verifiers: simulation clean, profile exact,
    normative surface and rationales exactly as reviewed."""
    problems = wire_module.rotation_semantics_problems()
    assert problems == [], problems
    for direction, pinned in ROTATION_PROFILE_PINS.items():
        derived = tuple(s.action for s in wire_module.ROTATION_STEPS
                        if s.direction == direction)
        assert derived == pinned, (
            f"{direction}: the step actions diverge from the reviewed profile: {derived}"
        )
    surface = hashlib.sha256(wire_module.rotation_surface_projection().encode()).hexdigest()[:16]
    assert surface == ROTATION_SURFACE_PIN, (
        f"the closed rotation surface changed since review (was {ROTATION_SURFACE_PIN}, "
        f"now {surface}) — read it, then re-pin in the same commit"
    )
    rationales = hashlib.sha256(
        "\n\n".join(wire_module.ROTATION_RATIONALES).encode()).hexdigest()[:16]
    assert rationales == ROTATION_RATIONALES_PIN, (
        f"the rotation rationales changed since review (was {ROTATION_RATIONALES_PIN}, "
        f"now {rationales}) — read them, then re-pin in the same commit"
    )
RECEIVER_SURFACE_PIN = "24ed7efcfa93edfc"
def _receiver_surface_ok() -> None:
    """Asserted by the effectiveness AND release verifiers: both published tables EQUAL their
    registry derivation, and the complete surface digest equals the reviewed pin."""
    assert tuple(WIRE.value("WIRE.CALLBACK.EFFECTIVENESS")) == wire_module.base_transitions()
    assert tuple(WIRE.value("WIRE.CALLBACK.RELEASE")) == wire_module.release_transitions()
    digest = hashlib.sha256(
        wire_module.receiver_surface_projection().encode()).hexdigest()[:16]
    assert digest == RECEIVER_SURFACE_PIN, (
        f"the receiver surface changed since review (was {RECEIVER_SURFACE_PIN}, now "
        f"{digest}) — read the new surface, then re-pin in the same commit"
    )
def governing_string_paths(claim) -> dict:
    """Every (type-level path -> ordered strings) by which this claim GOVERNS the page.

    Two kinds live in here, and the difference is stated rather than blurred (re-audit-2
    finding 1). Most are text the document PRINTS. A few are declarations that decide how that
    text is drawn — today, exactly one: a column's `role`, which chooses between an exact prose
    cell and a token cell whose commas the page comparison must forgive. It prints nothing, and
    that is precisely why the previous closure let it through while it silently stripped
    punctuation from an externally-binding table. Both kinds need a receipt; `REGISTRY_SCHEMA_PINS`
    keeps the second in its own reviewed lane so neither can be filed as the other.
    """
    found: dict = {}

    def walk(value, path):
        if isinstance(value, bool) or value is None:
            return
        if isinstance(value, str):
            found.setdefault(path, []).append(value)
            return
        if isinstance(value, (int, float, bytes)):
            return
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            fields = {f.name: f for f in dataclasses.fields(value)}
            declared = getattr(type(value), "PUBLISHED_FIELDS", None)
            # PUBLISHED_FIELDS is what reaches the page; REVIEWED_FIELDS is prose the type
            # declares as human-reviewed — a Statement's alternatives, INCLUDING the branches it
            # did not select (Wave-2 re-audit finding 1). Both need receipts; neither may hide
            # behind the other.
            names = (*(declared or tuple(fields)),
                     *getattr(type(value), "REVIEWED_FIELDS", ()))
            for name in names:
                # A DERIVED field is skipped only when the type has not declared it published.
                # `Statement.text` is derived AND published — it is the sentence a reader gets —
                # so the closure must see it (Wave-2 audit finding 1); its receipt is the
                # statement's own verifier, which executes the fact that selects it.
                if not fields[name].init and not declared:
                    continue
                walk(getattr(value, name), f"{path}:{type(value).__name__}.{name}")
            return
        if isinstance(value, Mapping):
            for key, item in value.items():
                walk(item, f"{path}{{{key}}}")
            return
        if isinstance(value, (set, frozenset)):
            raise AssertionError(
                f"{claim.id}: unordered collection at {path} — a rendered value must have a "
                "stable order for its digest to mean anything")
        if isinstance(value, (list, tuple)):
            for item in value:
                walk(item, path + "[]")
            return
        found.setdefault(path, []).append(str(value))

    walk(claim.value, "value")
    # EVERY registry-owned string a projection can render, not only the ones under `value`
    # (Wave-2 audit finding 2). A column's header is the matrix's first row — registry text, printed
    # verbatim — and walking only value/note left it outside the closure entirely: replacing
    # WIRE.INGEST.STATUS's headers with ("Code", "Treat this response as optional") printed that
    # instruction on the page while the authority map, the ordered matrix comparison, the total
    # prose stream, and this closure all stayed silent. Outer Claim fields are enumerated from the
    # dataclass itself, so a field added later cannot be forgotten here either.
    #
    # `columns` carries BOTH kinds: each column's `header` is printed verbatim as the matrix's
    # first row, and its `role` decides how the cells beneath it may be drawn. The role used to be
    # a separate `token_columns` field excluded from this closure on the true-but-irrelevant
    # ground that integers print nothing (re-audit-2 finding 1) — so
    # `replace(WIRE["WIRE.INGEST.STATUS"], token_columns=(1,))` marked a PROSE column lossy and
    # the release verifier forgave the comma loss it caused. Walking the typed schema enumerates
    # both, under their own paths, each holding its own receipt.
    _RENDERED_CLAIM_FIELDS = ("columns",)
    _NEVER_RENDERED = ("id", "value", "authority", "state", "exclusive_terms")
    declared = {f.name for f in dataclasses.fields(claim)}
    unaccounted = declared - set(_RENDERED_CLAIM_FIELDS) - set(_NEVER_RENDERED)
    if unaccounted:
        raise AssertionError(
            f"Claim gained field(s) {sorted(unaccounted)}: declare whether a projection can "
            "render them, so the receipt closure covers them or provably need not"
        )
    for name in _RENDERED_CLAIM_FIELDS:
        walk(getattr(claim, name), name)
    return found
VERIFIER_BOUND = frozenset({
    # Derived published cells: text COMPUTED from records their claim's verifier binds — the
    # transition texts from OutcomeKind/REASON_TEXTS, the event payload displays from
    # PAYLOAD_MODELS, the setting displays from Settings, and every `Statement.text` from the
    # fact its verifier executes. None of them is authored beside the claim (Wave-2 finding 1).
    ("WIRE.EVENT.TABLE", "value[]:EventRow.required_display"),
    ("WIRE.EVENT.TABLE", "value[]:EventRow.optional_display"),
    ("WIRE.CALLBACK.EFFECTIVENESS", "value[]:Transition.condition"),
    ("WIRE.CALLBACK.EFFECTIVENESS", "value[]:Transition.record"),
    ("WIRE.CALLBACK.EFFECTIVENESS", "value[]:Transition.effective"),
    ("WIRE.CALLBACK.EFFECTIVENESS", "value[]:Transition.why"),
    ("WIRE.CALLBACK.RELEASE", "value[]:ReleaseTransition.condition"),
    ("WIRE.CALLBACK.RELEASE", "value[]:ReleaseTransition.record"),
    ("WIRE.CALLBACK.RELEASE", "value[]:ReleaseTransition.effective"),
    ("WIRE.CALLBACK.RELEASE", "value[]:ReleaseTransition.why"),
    ("WIRE.CALLBACK.ACK_CONSEQUENCE", "value:Statement.text"),
    ("WIRE.SIGN.DIRECTION_FORM", "value:Statement.text"),
    ("WIRE.SIGN.COMPANION_PROOF", "value:Statement.text"),
    ("WIRE.CALLBACK.OPTIONAL_FIELD_RULE", "value:Statement.text"),
    ("WIRE.CALLBACK.VALIDATION_ORDER", "value:Statement.text"),
    ("WIRE.CALLBACK.ACK_VS_APPLY", "value:Statement.text"),
    ("WIRE.CALLBACK.LEGEND_CLOSURE", "value:Statement.text"),
    ("WIRE.CALLBACK.RELEASE_STATE", "value:Statement.text"),
    ("WIRE.ORDERING.ORDINAL_AUTHORITY", "value:Statement.text"),
    ("WIRE.ORDERING.OBLIGATION_STATE", "value:Statement.text"),
    ("OPS.CONFIG.DEFAULTS", "value[]:SettingDefault.variable"),
    ("OPS.CONFIG.DEFAULTS", "value[]:SettingDefault.display"),
    ("OPS.CONFIG.HMAC_SET_RULE", "value:Statement.text"),
    ("OPS.CUTOVER.EXECUTION_SOURCE", "value:Statement.text"),
    ("OPS.CUTOVER.CEILING_RULE", "value:Statement.text"),
    ("OPS.HMAC.V1_DROP_TIMING", "value:Statement.text"),
    ("WIRE.EVENT.TABLE", "value[]:EventRow.required_display"),
    ("WIRE.INGEST.PATH", "value"),
    ("WIRE.INGEST.HEADERS", "value[]:HeaderSpec.name"),
    ("WIRE.INGEST.EXTRA_FIELDS", "value{envelope}"),
    ("WIRE.INGEST.EXTRA_FIELDS", "value{payload}"),
    ("WIRE.ACTOR.SENSITIVE", "value{events}[]"),
    ("WIRE.ACTOR.SENSITIVE", "value{actor_type}"),
    ("WIRE.EVENT.TABLE", "value[]:EventRow.name"),
    ("WIRE.SIGN.CANONICAL", "value[]"),
    ("WIRE.SIGN.DIRECTIONS", "value{platform_to_tool}"),
    ("WIRE.SIGN.DIRECTIONS", "value{tool_to_platform}"),
    ("WIRE.SIGN.VECTOR", "value{secret}"),
    ("WIRE.SIGN.VECTOR", "value{key_id}"),
    ("WIRE.SIGN.VECTOR", "value{direction}"),
    ("WIRE.SIGN.VECTOR", "value{method}"),
    ("WIRE.SIGN.VECTOR", "value{path_qs}"),
    ("WIRE.SIGN.VECTOR", "value{timestamp}"),
    ("WIRE.SIGN.VECTOR", "value{slot}"),
    ("WIRE.SIGN.VECTOR", "value{body_sha256}"),
    ("WIRE.SIGN.VECTOR", "value{canonical_lines}[]"),
    ("WIRE.SIGN.VECTOR", "value{signature}"),
    ("WIRE.SIGN.ROTATION", "value[]"),
    ("WIRE.CALLBACK.PATH", "value"),
    ("WIRE.CALLBACK.FIELDS", "value[]"),
    ("WIRE.CALLBACK.OPTIONAL_FIELDS", "value[]"),
    ("WIRE.CALLBACK.GATES", "value[]"),
    ("WIRE.CALLBACK.DECISIONS", "value[]"),
    ("WIRE.CALLBACK.EFFECTIVENESS", "value[]:Transition.phase"),
    ("WIRE.CALLBACK.LEGEND", "value[][]"),
    ("WIRE.ORDERING.PENDING_INPUTS", "value[]:PendingInput.obligation"),
    ("OPS.CONFIG.HMAC_SET", "value[]"),
    ("OPS.CUTOVER.PROCEDURES", "value[]:Procedure.name"),
    ("OPS.CUTOVER.PROCEDURES", "value[]:Procedure.plan:ProcedurePlanContract.phase"),
    ("OPS.CUTOVER.PROCEDURES", "value[]:Procedure.plan:ProcedurePlanContract.subject_id"),
    ("OPS.CUTOVER.PROCEDURES",
     "value[]:Procedure.plan:ProcedurePlanContract.prerequisites[]:PinnedImage.evidence"),
    ("OPS.CUTOVER.PROCEDURES",
     "value[]:Procedure.plan:ProcedurePlanContract.prerequisites[]:"
     "PlatformWrittenCommitment.commitments[]"),
    ("OPS.CUTOVER.PROCEDURES",
     "value[]:Procedure.plan:ProcedurePlanContract.prerequisites[]:"
     "PlatformWrittenCommitment.evidence"),
    ("OPS.CUTOVER.PROCEDURES",
     "value[]:Procedure.plan:ProcedurePlanContract.prerequisites[]:TrustedPathProbes.evidence"),
    ("OPS.CUTOVER.PROCEDURES", "value[]:Procedure.playbook_ref:PlaybookRef.path"),
    ("OPS.CUTOVER.PROCEDURES", "value[]:Procedure.playbook_ref:PlaybookRef.heading"),
    ("OPS.CUTOVER.PROCEDURES", "value[]:Procedure.playbook_ref:PlaybookRef.sha256"),
    ("OPS.CUTOVER.PROCEDURES",
     "value[]:Procedure.playbook_ref:PlaybookRef.commands[]:Command.argv[]"),
    ("OPS.CUTOVER.PROCEDURES",
     "value[]:Procedure.playbook_ref:PlaybookRef.commands[]:Command.line"),
    ("OPS.CUTOVER.PROCEDURES", "value[]:Procedure.playbook_ref:PlaybookRef.body[]:Narrative.text"),
    ("OPS.CUTOVER.PROCEDURES",
     "value[]:Procedure.playbook_ref:PlaybookRef.body[]:Narrative.commands[]:Command.argv[]"),
    ("OPS.CUTOVER.PROCEDURES",
     "value[]:Procedure.playbook_ref:PlaybookRef.body[]:Narrative.commands[]:Command.line"),
    ("OPS.CUTOVER.PROCEDURES",
     "value[]:Procedure.rollback_contract:RollbackContract.facts[]:RollbackFact.question"),
    ("OPS.CUTOVER.PROCEDURES",
     "value[]:Procedure.rollback_contract:RollbackContract.facts[]:RollbackFact.answer"),
    ("OPS.CUTOVER.PROCEDURES",
     "value[]:Procedure.rollback_contract:RollbackContract.facts[]:RollbackFact.evidence"),
    ("OPS.CUTOVER.PROCEDURES",
     "value[]:Procedure.plan:ProcedurePlanContract.prerequisites[]:FlagStillOff.flag_env"),
    ("OPS.CUTOVER.PROCEDURES",
     "value[]:Procedure.plan:ProcedurePlanContract.prerequisites[]:FlagStillOff.evidence"),
    ("OPS.CUTOVER.PROCEDURES",
     "value[]:Procedure.plan:ProcedurePlanContract.prerequisites[]:SeedAndReadBack.evidence"),
    ("OPS.CUTOVER.PROCEDURES",
     "value[]:Procedure.plan:ProcedurePlanContract.prerequisites[]:BacklogPreflight.evidence"),
    ("OPS.CUTOVER.PROCEDURES",
     "value[]:Procedure.plan:ProcedurePlanContract.prerequisites[]:StoppedAttestedZero.roles[]"),
    ("OPS.CUTOVER.PROCEDURES",
     "value[]:Procedure.plan:ProcedurePlanContract.prerequisites[]:StoppedAttestedZero.evidence"),
    ("OPS.CUTOVER.PROCEDURES",
     "value[]:Procedure.playbook_ref:PlaybookRef.body[]:"
     "OperatorInstruction.commands[]:Command.argv[]"),
    ("OPS.CUTOVER.PROCEDURES",
     "value[]:Procedure.playbook_ref:PlaybookRef.body[]:"
     "OperatorInstruction.commands[]:Command.line"),
    ("OPS.CUTOVER.PROCEDURES",
     "value[]:Procedure.playbook_ref:PlaybookRef.body[]:OperatorInstruction.wraps[][]"),
    ("OPS.CUTOVER.PROCEDURES",
     "value[]:Procedure.plan:ProcedurePlanContract.prerequisites[]:"
     "RetentionSuspendedAttested.evidence"),
    ("OPS.CUTOVER.PROCEDURES",
     "value[]:Procedure.plan:ProcedurePlanContract.prerequisites[]:PreWindowDiagnostic.evidence"),
    ("OPS.CUTOVER.PROCEDURES",
     "value[]:Procedure.plan:ProcedurePlanContract.prerequisites[]:AuthoritativeBackup.evidence"),
    ("OPS.CUTOVER.PROCEDURES",
     "value[]:Procedure.rollback_contract:RollbackContract.branches[]:RollbackBranch.outcome"),
    ("OPS.CUTOVER.PROCEDURES",
     "value[]:Procedure.rollback_contract:RollbackContract.branches[]:"
     "RollbackBranch.facts[]:RollbackFact.question"),
    ("OPS.CUTOVER.PROCEDURES",
     "value[]:Procedure.rollback_contract:RollbackContract.branches[]:"
     "RollbackBranch.facts[]:RollbackFact.answer"),
    ("OPS.CUTOVER.PROCEDURES",
     "value[]:Procedure.rollback_contract:RollbackContract.branches[]:"
     "RollbackBranch.facts[]:RollbackFact.evidence"),
    ("OPS.CUTOVER.PROCEDURES", "value[]:Procedure.migration_span:MigrationSpan.base_revision"),
    ("OPS.CUTOVER.PROCEDURES", "value[]:Procedure.migration_span:MigrationSpan.target_revision"),
})
REGISTRY_PROSE_PINS = {
    # Every STATEMENT alternative — the selected reading and the one it was chosen over.
    # These are reviewed English, not machine-judged claims (Wave-2 re-audit finding 1): the
    # answer is executed, the sentences are read by a human and pinned here, and an edit to any
    # branch is a deliberate re-pin. The release verifier runs this closure.
    ("OPS.CONFIG.HMAC_SET_RULE", "value:Statement.alternatives{accepts_partial}"):
        ("3a26d7c0abb4389a",
         "the accepts_partial reading: Production accepts a subset of these variables a"),
    ("OPS.CONFIG.HMAC_SET_RULE", "value:Statement.alternatives{refuses_partial}"):
        ("051346b6cdc9b22c",
         "the refuses_partial reading: Production refuses a partial set. Secrets are at"),
    ("OPS.CUTOVER.CEILING_RULE", "value:Statement.alternatives{needs_this_procedure}"):
        ("42e2b21c4e6b8acd",
         "the needs_this_procedure reading: Any change to the ceiling, up or down, follows t"),
    ("OPS.CUTOVER.CEILING_RULE", "value:Statement.alternatives{rolling_ok}"):
        ("a8fb9dc30018b018",
         "the rolling_ok reading: For a ceiling change a rolling restart is fine; "),
    ("OPS.CUTOVER.EXECUTION_SOURCE", "value:Statement.alternatives{playbook_only}"):
        ("38d54c94048aa347",
         "the playbook_only reading: Plan from these entries; EXECUTE from the named "),
    ("OPS.CUTOVER.EXECUTION_SOURCE", "value:Statement.alternatives{summaries_ok}"):
        ("33a043ebc1f75950",
         "the summaries_ok reading: Plan and execute from these entries; the named p"),
    ("OPS.HMAC.V1_DROP_TIMING", "value:Statement.alternatives{after_confirmation}"):
        ("86267412a6b58645",
         "the after_confirmation reading: The publisher keeps signing v1 until your side c"),
    ("OPS.HMAC.V1_DROP_TIMING", "value:Statement.alternatives{at_the_sunset_date}"):
        ("e6faf8b58b1a696f",
         "the at_the_sunset_date reading: The publisher drops v1 the moment the outbound d"),
    ("WIRE.CALLBACK.ACK_CONSEQUENCE", "value:Statement.alternatives{recovered}"):
        ("dac88c8a87765d75",
         "the recovered reading: A 2xx returned before your commit is safe: we re"),
    ("WIRE.CALLBACK.ACK_CONSEQUENCE", "value:Statement.alternatives{unrecoverable}"):
        ("a57d9bb2963025e1",
         "the unrecoverable reading: A 2xx returned before your commit is unrecoverab"),
    ("WIRE.CALLBACK.ACK_VS_APPLY", "value:Statement.alternatives{different_decisions}"):
        ("3aa328b2c8582e0d",
         "the different_decisions reading: ACKNOWLEDGING a callback and APPLYING it are dif"),
    ("WIRE.CALLBACK.ACK_VS_APPLY", "value:Statement.alternatives{same_decision}"):
        ("f31629b336317c87",
         "the same_decision reading: Acknowledging a callback and applying it are the"),
    ("WIRE.CALLBACK.LEGEND_CLOSURE", "value:Statement.alternatives{closed}"):
        ("563f3623d1c9b4b6",
         "the closed reading: Every condition in the two tables is built from "),
    ("WIRE.CALLBACK.LEGEND_CLOSURE", "value:Statement.alternatives{open}"):
        ("18f5ab561e3077ed",
         "the open reading: The conditions read as ordinary English, and oth"),
    ("WIRE.CALLBACK.OPTIONAL_FIELD_RULE", "value:Statement.alternatives{may_drop}"):
        ("fa5fd417bcaa84cd",
         "the may_drop reading: You may drop either field: nothing downstream de"),
    ("WIRE.CALLBACK.OPTIONAL_FIELD_RULE", "value:Statement.alternatives{tolerate_and_preserve}"):
        ("6301b756a7d6e8ce",
         "the tolerate_and_preserve reading: Tolerate and preserve both. enforcement_held car"),
    ("WIRE.CALLBACK.RELEASE_STATE", "value:Statement.alternatives{live}"):
        ("95983e50056df421",
         "the live reading: The release protocol is live today, so this tabl"),
    ("WIRE.CALLBACK.RELEASE_STATE", "value:Statement.alternatives{post_024_only}"):
        ("e14b065121269fdc",
         "the post_024_only reading: POST-024 ONLY: the release protocol arrives with"),
    ("WIRE.CALLBACK.VALIDATION_ORDER", "value:Statement.alternatives{classify_first}"):
        ("b4902f5bc3078cc9",
         "the classify_first reading: You may classify first and validate afterwards: "),
    ("WIRE.CALLBACK.VALIDATION_ORDER", "value:Statement.alternatives{validate_first}"):
        ("d047619c688acca3",
         "the validate_first reading: VALIDATE BEFORE YOU CLASSIFY: after the signatur"),
    ("WIRE.ORDERING.OBLIGATION_STATE", "value:Statement.alternatives{resolvable}"):
        ("ac81e3d4c342138a",
         "the resolvable reading: A complete emailed answer clears the obligation "),
    ("WIRE.ORDERING.OBLIGATION_STATE", "value:Statement.alternatives{unresolvable}"):
        ("c0168092a5044cbc",
         "the unresolvable reading: These are the decisions 024 cannot be built with"),
    ("WIRE.ORDERING.ORDINAL_AUTHORITY", "value:Statement.alternatives{both_order}"):
        ("e35b34b359a5016e",
         "the both_order reading: Two ordinals for one case, and either ordinal or"),
    ("WIRE.ORDERING.ORDINAL_AUTHORITY", "value:Statement.alternatives{only_one_orders}"):
        ("a0323d6cb0b2b2a4",
         "the only_one_orders reading: Two ordinals for one case, and only one of them "),
    ("WIRE.SIGN.COMPANION_PROOF", "value:Statement.alternatives{executed}"):
        ("38c7c25bb3570796",
         "the executed reading: Our test suite executes the file's exact bytes a"),
    ("WIRE.SIGN.COMPANION_PROOF", "value:Statement.alternatives{unread}"):
        ("ee9f169dedc6d87b",
         "the unread reading: The shipped file is not executed by our suite, s"),
    ("WIRE.SIGN.DIRECTION_FORM", "value:Statement.alternatives{literal_only}"):
        ("f8173a97525d6d6a",
         "the literal_only reading: Literal tokens. Prose like 'inbound' will not ve"),
    ("WIRE.SIGN.DIRECTION_FORM", "value:Statement.alternatives{prose_ok}"):
        ("5b0e9c369d22ea8d",
         "the prose_ok reading: Either spelling works: prose also verifies along"),
    ("WIRE.INGEST.HEADERS", "value[]:HeaderSpec.value_note"):
        ("d1688c6441401460", "header value shapes: unique per logical event / unix seconds ..."),
    ("WIRE.INGEST.HEADERS", "value[]:HeaderSpec.why"):
        ("bc30558c830a6df1", "header why column: a replay returns the stored response verbatim ..."),
    ("WIRE.INGEST.STATUS", "value[][]"):
        ("5b6e84d6fa388b1c", "status meanings: accepted and queued — the normal result ..."),
    ("WIRE.INGEST.EXTRA_FIELDS", "value{prose}"):
        ("ebb5fb1852ddc911", "Unknown fields at the TOP LEVEL of the envelope are rejected ..."),
    ("WIRE.INGEST.ORDERING", "value"):
        ("4082744023a50603", "Events for one case are serialized by the order we admit them ..."),
    ("WIRE.ACTOR.SENSITIVE", "value{rule}"):
        ("38189f7f98c705ba", "actor.type must equal 'reviewer', and actor.id must equal ..."),
    ("WIRE.ACTOR.SENSITIVE", "value{trap}"):
        ("4a802291233aa6a5", "The generic envelope example shows actor.type 'user' ..."),
    ("WIRE.EVENT.TABLE", "value[]:EventRow.note"):
        ("a544984f5e28e9ef", "event notes: First event for a case creates it ..."),
    ("WIRE.SIGN.COMPANION", "value"):
        ("d8a840df49e2d780", "The runnable signer ships as a FILE alongside this document ..."),
    ("WIRE.SIGN.V1_SUNSET", "value"):
        ("bd9a9136304b9957", "Both v1 sunset dates are set with you at cutover and are unset ..."),
    ("WIRE.SIGN.ROTATION_RETIREMENT", "value[]:RetirementGate.direction"):
        ("ddf39acb3f68d0fa", "gate directions: INBOUND / OUTBOUND"),
    ("WIRE.SIGN.ROTATION_RETIREMENT", "value[]:RetirementGate.transition"):
        ("0a0a486a14dfa220", "blocked steps: We prove no request has arrived under the old id ..."),
    ("WIRE.SIGN.ROTATION_RETIREMENT", "value[]:RetirementGate.why_blocked"):
        ("4b71db50038610f7", "why blocked: Nothing durable records WHICH key id a request ..."),
    ("WIRE.SIGN.ROTATION_RETIREMENT", "value[]:RetirementGate.unblocked_by"):
        ("51752bf615e15d4f", "unblocked by: A durable fleet-wide per-key acceptance witness ..."),
    ("WIRE.CALLBACK.DELIVERY", "value[]"):
        ("9d0c6522fadae281", "delivery bullets: Each automated decision enqueues one callback ..."),
    ("WIRE.CALLBACK.VALIDATION", "value[]:ValidationRule.invalid_input"):
        ("c08382fbbb08bb13", "invalid inputs: a PARTIAL release binding ..."),
    ("WIRE.CALLBACK.VALIDATION", "value[]:ValidationRule.disposition"):
        ("a138892f4ba2ec82", "dispositions: integrity_mismatch — HOLD; do not record ..."),
    ("WIRE.CALLBACK.RECEIVER_TXN", "value[]"):
        ("2349b4021d8f161e", "the six receiver steps: Verify the signature. / ... / COMMIT ..."),
    ("WIRE.CALLBACK.WAIT_BOUND", "value"):
        ("3644c198b16adb76", "Respond within 10 seconds. Each attempt carries a 40-second ..."),
    ("WIRE.CALLBACK.COMPLETION", "value"):
        ("58004ff4528ff759", "For an automated decision that produces an eligible callback ..."),
    ("WIRE.ORDERING.NO_DECIDED_AT", "value"):
        ("c4e6d4ca523c3e2e", "decided_at is a display timestamp and is not an ordering key ..."),
    ("WIRE.ORDERING.INTERIM", "value"):
        ("b6244cbcf0e8b1ca", "Until ordered delivery is activated: dedupe exact repeats ..."),
    ("WIRE.ORDERING.SEQUENCE_DOMAINS", "value[]"):
        ("157c8ec72343f56b", "event_sequence is INGEST PROVENANCE / decision_sequence orders ..."),
    ("WIRE.ORDERING.PENDING_INPUTS", "value[]:PendingInput.owner"):
        ("49b8b60e5696f071", "obligation owners: TechCraft / platform"),
    ("WIRE.ORDERING.PENDING_INPUTS", "value[]:PendingInput.question"):
        ("476f3e9b300db914", "O1-O4 questions: Which platform principal is authorised ..."),
    ("WIRE.ORDERING.PENDING_INPUTS", "value[]:PendingInput.answer_type"):
        ("dd4ae27b7ff99ee6", "answer shapes: principal identifier + key id + which HMAC ..."),
    ("WIRE.ORDERING.PENDING_INPUTS", "value[]:PendingInput.blocked_deliverable"):
        ("5efc3281a00f6419", "blocked deliverables: the manual.release_requested request model ..."),
    ("WIRE.ORDERING.BOOTSTRAP_024", "value"):
        ("e0e3dbdc4a5609ff", "Ordered delivery needs a signed bootstrap of per-case high-water ..."),
    ("WIRE.ORDERING.INTEGRITY_MISMATCH", "value"):
        ("07d15b3e8ed5c95e", "integrity_mismatch is a POST-ACTIVATION terminal state ..."),
    ("WIRE.RETENTION.BY_KIND", "value[][]"):
        ("3b1e60e21247c182", "retention rows: decision callbacks / POC verification emails ..."),
    ("OPS.BLOCKER.PRODUCTION_PROVIDERS", "value[]"):
        ("acb99d446c3b05a6", "This tool cannot run in production yet, and no configuration ..."),
    ("OPS.PROCESS.COMMANDS", "value[][]"):
        ("7a59c32b1e5070b9", "process table rows: API / Migrations / workers, commands, notes"),
    ("OPS.PROCESS.DEV_WORKER_BANNED", "value"):
        ("df4438bbe893ea56", "dev_worker is a development role that wires fixture adapters ..."),
    ("OPS.INFRA.COMPONENTS", "value[][]"):
        ("0acf577a49110427", "infrastructure rows: PostgreSQL / object store / compute ..."),
    ("OPS.CONFIG.PRODUCTION_FLOORS", "value[]"):
        ("cf18faefd52e1d74", "job_lease_seconds is floored at 30 in production ..."),
    ("OPS.CONFIG.ROTATION_KEYS", "value"):
        ("fa0307861feb0a52", "KYC_HMAC_INBOUND_EXTRA_KEYS is a JSON object of key_id to secret ..."),
    ("OPS.CONFIG.M2_GATE", "value"):
        ("4e2a1218d6d6a959", "KYC_ENFORCE_POSITIVE_DECISIONS stays false in production ..."),
    ("OPS.HEALTH.PROBES", "value[][]"):
        ("337d8c489ebf9c5d", "health rows: /healthz, /readyz, metrics — contracts and actions"),
    ("OPS.RELEASE.CLASSIFICATION", "value"):
        ("877130f0966ab63a", "Every release declares itself rolling or full-maintenance ..."),
    ("OPS.CUTOVER.OUTBOX_CEILING", "value[]"):
        ("0220501e7a96543b", "ceiling steps: disable autoscaling and rolling restart ..."),
    ("OPS.ROLLBACK.MIGRATION_BOUNDARY", "value[]"):
        ("49880b15d3422e44", "A release with no migration rolls back by redeploying ..."),
    ("OPS.HMAC.ROLLOUT_ORDER", "value[]"):
        ("98e2f53212a9f721", "Deploy with both v1 sunset dates in the future ..."),
    ("OPS.RECOVERY.REQUEUE", "value"):
        ("737b6d8d346c6cb8", "The always-mounted ops endpoints POST /v1/ops/requeue ..."),
    # Column titles: registry-owned text printed verbatim as the matrix's first row. Reviewed as
    # LABELS — they name a column, they do not instruct — so a rewrite that turns one into an
    # instruction ("Treat this response as optional") is a re-pin a reviewer reads.
    ("WIRE.INGEST.HEADERS", "columns[]:Column.header"):
        ("850fe8bf33e67110", "columns: Header | Value | Why"),
    ("WIRE.INGEST.STATUS", "columns[]:Column.header"):
        ("396356ba1aec5749", "columns: Code | Meaning"),
    ("WIRE.EVENT.TABLE", "columns[]:Column.header"):
        ("2c6b7085596c4258", "columns: event_type | Required payload | Optional payload | Notes"),
    ("WIRE.SIGN.ROTATION_RETIREMENT", "columns[]:Column.header"):
        ("02127eca44c1607b", "columns: Direction | Blocked step | Why blocked | What unblocks it"),
    ("WIRE.CALLBACK.VALIDATION", "columns[]:Column.header"):
        ("54e80acd7032ce61", "columns: Invalid input | Disposition"),
    ("WIRE.CALLBACK.EFFECTIVENESS", "columns[]:Column.header"):
        ("d0efce82c130ff22", "columns: Phase | When this row applies | Record | Effective? | Why"),
    ("WIRE.CALLBACK.LEGEND", "columns[]:Column.header"):
        ("a2bf7a42c69a4737", "columns: Token | Meaning"),
    ("WIRE.CALLBACK.RELEASE", "columns[]:Column.header"):
        ("cd84a75e6429e5eb", "columns: When this row applies | Record | Effective? | Why"),
    ("WIRE.ORDERING.PENDING_INPUTS", "columns[]:Column.header"):
        ("43eab4f9e30f89aa", "columns: # | Owner | question | answer shape | blocked deliverable"),
    ("WIRE.RETENTION.BY_KIND", "columns[]:Column.header"):
        ("c54b7c11309c5fc9", "columns: Kind | What happens"),
    ("OPS.PROCESS.COMMANDS", "columns[]:Column.header"):
        ("cb90fa299ee91ed9", "columns: Process | Command | Notes"),
    ("OPS.INFRA.COMPONENTS", "columns[]:Column.header"):
        ("d9ac3561a1db5d11", "columns: Component | Requirement | Why"),
    ("OPS.CONFIG.DEFAULTS", "columns[]:Column.header"):
        ("aa580cc604c89097", "columns: Setting | Default"),
    ("OPS.HEALTH.PROBES", "columns[]:Column.header"):
        ("62bf0bb1e296336c", "columns: Surface | Contract | Action"),
}
REGISTRY_SCHEMA_PINS = {
    ("WIRE.INGEST.HEADERS", "columns[]:Column.role"):
        ("5ee473ddb44944e5", "roles: Header=token | Value=prose | Why=prose"),
    ("WIRE.INGEST.STATUS", "columns[]:Column.role"):
        ("2d1a5acccc943a88", "roles: Code=prose | Meaning=prose"),
    ("WIRE.EVENT.TABLE", "columns[]:Column.role"):
        ("2c4a19568d89e2ed",
         "roles: event_type=token | Required payload=token | Optional payload=token | Notes=prose"),
    ("WIRE.SIGN.ROTATION_RETIREMENT", "columns[]:Column.role"):
        ("cc03a2136b605cf3",
         "roles: Direction=prose | Blocked step=prose | Why blocked=prose | What unblocks=prose"),
    ("WIRE.CALLBACK.VALIDATION", "columns[]:Column.role"):
        ("2d1a5acccc943a88", "roles: Invalid input=prose | Disposition=prose"),
    ("WIRE.CALLBACK.EFFECTIVENESS", "columns[]:Column.role"):
        ("2722b015173e80ea",
         "roles: Phase=prose | When=prose | Record=prose | Effective?=prose | Why=prose"),
    ("WIRE.CALLBACK.LEGEND", "columns[]:Column.role"):
        ("f55a2cc14b3f7d0e", "roles: Token=token | Meaning=prose"),
    ("WIRE.CALLBACK.RELEASE", "columns[]:Column.role"):
        ("cc03a2136b605cf3", "roles: When=prose | Record=prose | Effective?=prose | Why=prose"),
    ("WIRE.ORDERING.PENDING_INPUTS", "columns[]:Column.role"):
        ("2722b015173e80ea",
         "roles: #=prose | Owner=prose | question=prose | answer shape=prose | blocked=prose"),
    ("WIRE.RETENTION.BY_KIND", "columns[]:Column.role"):
        ("2d1a5acccc943a88", "roles: Kind=prose | What happens=prose"),
    ("OPS.PROCESS.COMMANDS", "columns[]:Column.role"):
        ("ec9931734d9d9611", "roles: Process=prose | Command=prose | Notes=prose"),
    ("OPS.INFRA.COMPONENTS", "columns[]:Column.role"):
        ("ec9931734d9d9611", "roles: Component=prose | Requirement=prose | Why=prose"),
    ("OPS.CONFIG.DEFAULTS", "columns[]:Column.role"):
        ("f55a2cc14b3f7d0e", "roles: Setting=token | Default=prose"),
    ("OPS.HEALTH.PROBES", "columns[]:Column.role"):
        ("ec9931734d9d9611", "roles: Surface=prose | Contract=prose | Action=prose"),

    # …and which ROW ATTRIBUTE lands under each header (re-audit-3 finding 1). This was
    # `claim_table(row_fields=...)`, chosen at the generator call and recorded on the Block the
    # checks then read, so re-aiming it published every value truly under every header truly with
    # the pairs wrong — the missing-authority explanation under `Blocked step`, the acceptance
    # witness under `Why it cannot be exercised today`, `_top_level_verify` green. The label shows
    # the pairing itself, because a digest over field names is not something anyone can read.
    ("WIRE.INGEST.HEADERS", "columns[]:Column.field"):
        ("42ae47fd4f9e1d17", "binds: Header<-name | Value<-value_note | Why<-why"),
    ("WIRE.INGEST.STATUS", "columns[]:Column.field"):
        ("01ba4719c80b6fe9", "binds: positional tuple rows, no column names an attribute"),
    ("WIRE.EVENT.TABLE", "columns[]:Column.field"):
        ("0ffca65db090f3aa",
         "binds: event_type<-name | Required<-required_display | Optional<-optional_display | "
         "Notes<-note"),
    ("WIRE.SIGN.ROTATION_RETIREMENT", "columns[]:Column.field"):
        ("0536d0037b84cb98",
         "binds: Direction<-direction | Blocked step<-transition | Why blocked<-why_blocked | "
         "What unblocks it<-unblocked_by"),
    ("WIRE.CALLBACK.VALIDATION", "columns[]:Column.field"):
        ("367dab3505422001", "binds: Invalid input<-invalid_input | Disposition<-disposition"),
    ("WIRE.CALLBACK.EFFECTIVENESS", "columns[]:Column.field"):
        ("45235df6e16be0d7",
         "binds: Phase<-phase | When<-condition | Record<-record | Effective?<-effective | "
         "Why<-why"),
    ("WIRE.CALLBACK.LEGEND", "columns[]:Column.field"):
        ("01ba4719c80b6fe9", "binds: positional tuple rows, no column names an attribute"),
    ("WIRE.CALLBACK.RELEASE", "columns[]:Column.field"):
        ("e46896959678b9e0",
         "binds: When<-condition | Record<-record | Effective?<-effective | Why<-why"),
    ("WIRE.ORDERING.PENDING_INPUTS", "columns[]:Column.field"):
        ("6e35378a435b3e42",
         "binds: #<-obligation | Owner<-owner | question<-question | shape<-answer_type | "
         "blocked<-blocked_deliverable"),
    ("WIRE.RETENTION.BY_KIND", "columns[]:Column.field"):
        ("01ba4719c80b6fe9", "binds: positional tuple rows, no column names an attribute"),
    ("OPS.PROCESS.COMMANDS", "columns[]:Column.field"):
        ("75a11da44c802486", "binds: positional tuple rows, no column names an attribute"),
    ("OPS.INFRA.COMPONENTS", "columns[]:Column.field"):
        ("75a11da44c802486", "binds: positional tuple rows, no column names an attribute"),
    ("OPS.CONFIG.DEFAULTS", "columns[]:Column.field"):
        ("279f581798f13762", "binds: Setting<-variable | Default<-display"),
    ("OPS.HEALTH.PROBES", "columns[]:Column.field"):
        ("75a11da44c802486", "binds: positional tuple rows, no column names an attribute"),
}
DISPLAY_SCHEMA_PATHS = frozenset({"columns[]:Column.role", "columns[]:Column.field"})
def _prose_digest(texts) -> str:
    return hashlib.sha256("\n".join(texts).encode()).hexdigest()[:16]
def _receipt_problems(registries) -> list[str]:
    """Every governing string path holds exactly one receipt; every receipt names a live path."""
    problems: list[str] = []
    seen: set = set()
    for registry in registries:
        for claim in registry.claims:
            for path, texts in governing_string_paths(claim).items():
                key = (claim.id, path)
                seen.add(key)
                receipts = [name for name, table in
                            (("VERIFIER_BOUND", None),
                             ("REGISTRY_PROSE_PINS", REGISTRY_PROSE_PINS),
                             ("REGISTRY_SCHEMA_PINS", REGISTRY_SCHEMA_PINS))
                            if (key in VERIFIER_BOUND if table is None else key in table)]
                if len(receipts) > 1:
                    problems.append(
                        f"{key}: carries {len(receipts)} receipts ({', '.join(receipts)}); "
                        "one authority of record")
                    continue
                if receipts == ["VERIFIER_BOUND"]:
                    continue
                if not receipts:
                    problems.append(
                        f"{key}: registry text or display authority with NO receipt — bind it in "
                        "a verifier or pin it, which is the act of reviewing it: "
                        f"{texts[0][:60]!r}")
                    continue
                table = REGISTRY_PROSE_PINS if receipts == ["REGISTRY_PROSE_PINS"] \
                    else REGISTRY_SCHEMA_PINS
                pinned = table[key]
                if pinned[0] != _prose_digest(texts):
                    problems.append(
                        f"{key} ({pinned[1]}) changed since it was reviewed: "
                        f"reviewed {pinned[0]}, now {_prose_digest(texts)}. Read the new "
                        "value, then re-pin it in the SAME commit.")
    for key in VERIFIER_BOUND - seen:
        problems.append(f"{key}: VERIFIER_BOUND names a path no claim renders (stale)")
    for key in set(REGISTRY_PROSE_PINS) - seen:
        problems.append(f"{key}: REGISTRY_PROSE_PINS pins a path no claim renders (stale)")
    for key in set(REGISTRY_SCHEMA_PINS) - seen:
        problems.append(f"{key}: REGISTRY_SCHEMA_PINS pins a path no claim declares (stale)")
    return problems
def _statement(claim_id: str):
    """The claim's Statement, with the shape checks every one of these verifiers relies on."""
    from docs.contracts.statements import Statement

    registry = WIRE if claim_id.startswith("WIRE.") else OPERATIONS
    value = registry.value(claim_id)
    assert type(value) is Statement, f"{claim_id} must publish a Statement, not {type(value)}"
    assert value.text == value.alternatives[value.answer], (
        f"{claim_id}: published text is not the alternative its answer selects")
    return value
def _publisher_source() -> str:
    return (SRC / "outbox" / "publisher.py").read_text()
@verifies("WIRE.CALLBACK.ACK_CONSEQUENCE")
def _a_2xx_terminalizes_the_row_so_the_warning_is_the_true_one():
    """The early-2xx witness, bound to the publisher's own delivery branch.

    `_deliver` returning normally (the 2xx path) is followed by `_record_delivered`, and the
    delivered terminal is what stops further attempts — so an acknowledgement the platform sends
    before its own commit cannot be retried into existence. Declaring `recovered` would publish
    "we retry the row until your side confirms it", which this refuses.
    """
    statement = _statement("WIRE.CALLBACK.ACK_CONSEQUENCE")
    source = _publisher_source()
    tree = ast.parse(source)
    process = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "process_once")
    body = ast.get_source_segment(source, process) or ""
    # the send is followed by the delivered terminal — no confirmation step in between
    assert "_deliver(" in body and "_record_delivered(" in body
    assert body.index("_deliver(") < body.index("_record_delivered(")
    delivered = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_record_delivered")
    delivered_body = ast.get_source_segment(source, delivered) or ""
    assert "published_at" in delivered_body, "a delivered row is stamped, i.e. terminal"
    assert statement.answer == "unrecoverable", (
        "the publisher terminalizes on the acknowledgement, so the published sentence must be "
        f"the unrecoverable one, not {statement.answer!r}")
@verifies("WIRE.SIGN.COMPANION_PROOF")
def _the_companion_statement_matches_what_the_suite_actually_does():
    """`executed` claims our suite runs the shipped file's own bytes. Run them here, the same way
    the companion verifier does, and require the published signature back."""
    statement = _statement("WIRE.SIGN.COMPANION_PROOF")
    vector = WIRE.value("WIRE.SIGN.VECTOR")
    from docs.contracts.companion import artifact_path, artifact_text

    namespace: dict = {}
    exec(compile(artifact_text(), str(artifact_path()), "exec"), namespace)  # noqa: S102
    produced = namespace["sign"](
        secret=vector["secret"], key_id=vector["key_id"], direction=vector["direction"],
        method=vector["method"], path_qs=vector["path_qs"], timestamp=vector["timestamp"],
        slot=vector["slot"], body=vector["body"],
    )
    assert produced == vector["signature"], "the shipped file no longer reproduces the vector"
    assert statement.answer == "executed"
@verifies("WIRE.CALLBACK.OPTIONAL_FIELD_RULE")
def _optional_fields_are_tolerated_and_preserved_by_the_model():
    """`tolerate_and_preserve` is a property of the callback model: both fields are declared and
    optional, so a receiver that drops them loses data the wire carries."""
    statement = _statement("WIRE.CALLBACK.OPTIONAL_FIELD_RULE")
    declared = WIRE.value("WIRE.CALLBACK.OPTIONAL_FIELDS")
    fields = DecisionCallback.model_fields
    for name in declared:
        assert name in fields and not fields[name].is_required(), name
    assert statement.answer == "tolerate_and_preserve"
@verifies("WIRE.CALLBACK.VALIDATION_ORDER")
def _validation_precedes_classification_in_the_reference_receiver():
    """`validate_first` is executed: the reference receiver's own order decides it."""
    statement = _statement("WIRE.CALLBACK.VALIDATION_ORDER")
    source = (REPO / "docs" / "contracts" / "receiver_reference.py").read_text()
    observe_at = source.index("def observe(")
    validate_at = source.index("_validate(", observe_at)
    classify_at = source.index("predicates.DUP if", observe_at)
    assert validate_at < classify_at, "the reference receiver classifies before it validates"
    assert statement.answer == "validate_first"
@verifies("WIRE.CALLBACK.ACK_VS_APPLY")
def _acknowledging_and_applying_are_separate_in_the_executed_table():
    """`different_decisions` is observable: rows exist that RECORD without becoming EFFECTIVE."""
    statement = _statement("WIRE.CALLBACK.ACK_VS_APPLY")
    rows = WIRE.value("WIRE.CALLBACK.EFFECTIVENESS")
    recorded_not_effective = [r for r in rows if r.records and not r.becomes_effective]
    assert recorded_not_effective, (
        "if no row recorded without becoming effective, the two decisions would be one")
    assert statement.answer == "different_decisions"
@verifies("WIRE.CALLBACK.LEGEND_CLOSURE")
def _every_published_condition_token_comes_from_the_legend():
    """`closed` is checked by tokenizing the published conditions against the legend itself."""
    statement = _statement("WIRE.CALLBACK.LEGEND_CLOSURE")
    legend = dict(WIRE.value("WIRE.CALLBACK.LEGEND"))
    # Every condition is `facet = token` clauses joined by commas; both halves must be legend
    # entries, so a condition cannot introduce a word the legend does not define.
    for row in (*WIRE.value("WIRE.CALLBACK.EFFECTIVENESS"), *WIRE.value("WIRE.CALLBACK.RELEASE")):
        for clause in row.condition.split(" AND "):
            facet, sep, rhs = clause.strip().partition(" = ")
            if not sep:  # a set clause: `sequence in {absent, not_above}`
                facet, _, rhs = clause.strip().partition(" in ")
                tokens = [w.strip() for w in rhs.strip("{} ").split(",")]
            else:
                tokens = [rhs.strip()]
            for token in tokens:
                assert token in legend, (
                    f"condition token {token!r} is not defined in the legend")
    assert statement.answer == "closed"
@verifies("WIRE.CALLBACK.RELEASE_STATE")
def _the_release_protocol_is_not_exercisable_today():
    """`post_024_only` is bound to the same absence the release claim is: the table is PENDING and
    no shipped code path emits its states."""
    statement = _statement("WIRE.CALLBACK.RELEASE_STATE")
    assert WIRE["WIRE.CALLBACK.RELEASE"].state is ClaimState.PENDING
    for module in ("api/schemas.py", "outbox/publisher.py", "events/ingest.py"):
        source = (SRC / module).read_text()
        assert "manual_release_pending" not in source, f"{module} already implements the protocol"
    assert statement.answer == "post_024_only"
@verifies("WIRE.ORDERING.ORDINAL_AUTHORITY")
def _only_the_decision_ordinal_orders_decisions():
    """`only_one_orders` is the D1 split, executed against the published domains."""
    statement = _statement("WIRE.ORDERING.ORDINAL_AUTHORITY")
    domains = " ".join(WIRE.value("WIRE.ORDERING.SEQUENCE_DOMAINS"))
    assert "event_sequence is INGEST PROVENANCE" in domains
    assert "decision_sequence" in domains
    assert statement.answer == "only_one_orders"
@verifies("WIRE.ORDERING.OBLIGATION_STATE")
def _no_artifact_can_resolve_an_obligation_yet():
    """`unresolvable` is executed: `resolution_problems` must refuse even a perfect artifact."""
    statement = _statement("WIRE.ORDERING.OBLIGATION_STATE")
    for item in WIRE.value("WIRE.ORDERING.PENDING_INPUTS"):
        problems = wire_module.resolution_problems(item, {"answer": "complete", "signed": True})
        assert problems, f"{item.obligation} accepted an artifact while the schema is unbuilt"
    assert statement.answer == "unresolvable"
@verifies("OPS.CONFIG.HMAC_SET_RULE")
def _production_refuses_a_partial_hmac_set():
    """`refuses_partial` is executed against the real production boundary."""
    statement = _statement("OPS.CONFIG.HMAC_SET_RULE")
    partial = hardened(hmac_inbound_secret="")
    assert any("hmac_inbound_secret" in v for v in production_config_violations(partial))
    assert statement.answer == "refuses_partial"
@verifies("OPS.CUTOVER.EXECUTION_SOURCE")
def _every_procedure_sends_the_operator_to_a_playbook():
    """`playbook_only` is structural: each procedure carries a playbook ref, and the guide
    publishes no step summary an operator could execute from instead. The Codex witness — "the
    named playbook is optional" — is the other alternative, which this refuses."""
    statement = _statement("OPS.CUTOVER.EXECUTION_SOURCE")
    for procedure in OPERATIONS.value("OPS.CUTOVER.PROCEDURES"):
        assert procedure.playbook_ref.path and procedure.playbook_ref.heading
        assert procedure.playbook, f"{procedure.name} publishes no playbook pointer"
    generator = (REPO / "docs" / "generators" / "techcraft_deployment_guide.py").read_text()
    assert "claim_steps(\"OPS.CUTOVER.PROCEDURES\"" not in generator
    assert statement.answer == "playbook_only"
@verifies("OPS.CUTOVER.CEILING_RULE")
def _a_ceiling_change_runs_the_published_procedure():
    """`needs_this_procedure` is bound to the shipped cutover record the claim renders from."""
    statement = _statement("OPS.CUTOVER.CEILING_RULE")
    steps = OPERATIONS.value("OPS.CUTOVER.OUTBOX_CEILING")
    assert steps and any("autoscaling" in step for step in steps)
    assert statement.answer == "needs_this_procedure"
@verifies("OPS.HMAC.V1_DROP_TIMING")
def _the_publisher_drops_v1_on_the_date_not_on_confirmation():
    """`at_the_sunset_date` is a property of the publisher: the sunset is a timestamp comparison
    with no readiness input, so nothing waits for the receiver."""
    statement = _statement("OPS.HMAC.V1_DROP_TIMING")
    source = _publisher_source()
    assert "v1_outbound_sunset_at" in source
    assert "receiver_ready" not in source and "confirmed_ready" not in source
    assert statement.answer == "at_the_sunset_date"
@verifies("WIRE.SIGN.DIRECTION_FORM")
def _only_the_literal_direction_token_verifies():
    """`literal_only` is EXECUTED: sign the same request with the prose word 'inbound' in place
    of the literal direction token and require the signature not to match."""
    statement = _statement("WIRE.SIGN.DIRECTION_FORM")
    directions = WIRE.value("WIRE.SIGN.DIRECTIONS")
    vector = WIRE.value("WIRE.SIGN.VECTOR")
    literal = directions["platform_to_tool"]
    assert vector["direction"] == literal
    # the canonical string refuses an unknown direction outright, so prose cannot even produce
    # a signature to compare — a stronger form of "will not verify" than a mismatch
    with raises(ValueError):
        sign_v2(
            vector["secret"], key_id=vector["key_id"], direction="inbound",
            method=vector["method"], path_qs=vector["path_qs"], timestamp=vector["timestamp"],
            slot=vector["slot"], body=vector["body"],
        )
    assert statement.answer == "literal_only"


# The complete registry surface. The receipt closure is defined over ALL of it — a receipt naming
# a path no claim renders is stale WHEREVER that claim lives — so it cannot be evaluated one
# registry at a time, and a release verifies the whole surface rather than the half it publishes.
REGISTRIES = (WIRE, OPERATIONS)


def problems(registries=REGISTRIES) -> list[str]:
    """Every way these registries fail their own authority. Empty means publishable.

    Both lanes, in the order a reader cares about: a claim the running system contradicts is a
    false document, and a rendered string with no receipt is an unreviewed one.
    """
    found: list[str] = []
    for registry in registries:
        for claim in registry.claims:
            verifier = AUTHORITY_VERIFIERS.get(claim.id)
            if verifier is None:
                found.append(f"{claim.id}: no verifier — an unverified claim is not publishable")
                continue
            try:
                verifier()
            except Exception as exc:  # noqa: BLE001 — any refusal is a refusal
                found.append(f"{claim.id}: {type(exc).__name__}: {str(exc).strip()[:200]}")
    found.extend(_receipt_problems(registries))
    return found
