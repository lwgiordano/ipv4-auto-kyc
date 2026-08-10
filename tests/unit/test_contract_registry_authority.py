"""Every registry claim, held against an INDEPENDENT executable authority.

This file exists because a shared registry is not evidence (re-audit `82636da..9ac574f`, systemic
prevention requirement). If the generator and the test both read the same wrong value they agree
with each other and stay wrong. So nothing here compares the registry to itself: each literal
written in `docs/contracts/` is checked against the thing that actually governs it — a Pydantic
model, a Settings default, a runtime function, a shipped canonical record, or, for the blocker,
by executing the factory and observing that it refuses.

`test_contract_rendering.py` is the other half: it proves the rendered PDF displays these values.
"""

import hashlib
import math
import re
from pathlib import Path

import pytest
from docs.contracts import ClaimState
from docs.contracts.operations import OPERATIONS
from docs.contracts.signing_example import sign as example_sign
from docs.contracts.wire import WIRE

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
    Settings,
    hmac_extra_key_violations,
    production_config_violations,
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

REPO = Path(__file__).resolve().parents[2]


# ── registry hygiene ──────────────────────────────────────────────────────────────────────────────
def test_every_claim_names_an_authority_and_a_known_state():
    for registry in (WIRE, OPERATIONS):
        for claim in registry.claims:
            assert claim.authority.strip(), f"{claim.id} names no authority"
            assert isinstance(claim.state, ClaimState)
            assert claim.id == claim.id.upper(), f"{claim.id} should be an upper-case id"


# ── signing: the vector is ONE bound record ───────────────────────────────────────────────────────
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


def test_signature_vector_is_bound_end_to_end():
    """Body, byte count, body hash, ordered canonical block, and signature all recomputed from
    kyc_tool.security. Change any one part of the published vector and this fails, which is what
    'bound' means: the previous version checked the digest and the hash independently, so an
    altered body with a stale hash passed."""
    v = WIRE.value("WIRE.SIGN.VECTOR")
    fields = _vector_fields(v)

    assert len(v["body"]) == v["body_bytes"]
    assert hashlib.sha256(v["body"]).hexdigest() == v["body_sha256"]

    canonical = canonical_v2(**fields)
    assert tuple(canonical.split("\n")) == v["canonical_lines"], "canonical block drifted or reordered"
    assert canonical.split("\n")[-1] == v["body_sha256"], "the canonical tail is not this body's hash"

    assert sign_v2(v["secret"], **fields) == v["signature"]


def test_published_example_reproduces_the_shipped_signer():
    """The snippet TechCraft copies is executed here against the real signer, so the code in the
    PDF is code that was proven, not prose that looked right."""
    v = WIRE.value("WIRE.SIGN.VECTOR")
    fields = _vector_fields(v)
    assert example_sign(v["secret"], **fields) == sign_v2(v["secret"], **fields) == v["signature"]


def test_direction_tokens_match_the_signer_literals():
    directions = WIRE.value("WIRE.SIGN.DIRECTIONS")
    assert directions["platform_to_tool"] == DIRECTION_INBOUND
    assert directions["tool_to_platform"] == DIRECTION_OUTBOUND
    assert WIRE.value("WIRE.SIGN.VECTOR")["direction"] == DIRECTION_INBOUND


def test_canonical_field_order_matches_the_signer():
    """The documented field names, in order, against a canonical string built from placeholders."""
    documented = WIRE.value("WIRE.SIGN.CANONICAL")
    probe = canonical_v2(
        key_id="K", direction=DIRECTION_INBOUND, method="M", path_qs="P", timestamp="T",
        slot="S", body=b"",
    ).split("\n")
    assert len(documented) == len(probe) == 8
    assert documented[0] == probe[0] == "v2"
    for position, placeholder in ((1, "K"), (2, DIRECTION_INBOUND), (3, "M"), (4, "P"),
                                  (5, "T"), (6, "S")):
        assert probe[position] == placeholder, f"canonical position {position} moved"
    assert probe[7] == hashlib.sha256(b"").hexdigest()


def test_skew_window_matches_the_verifier():
    assert WIRE.value("WIRE.SIGN.SKEW_SECONDS") == MAX_HMAC_SKEW_SECONDS


def test_rotation_claim_matches_the_enforced_floor():
    """The prose promises a 32-char floor, non-blank ids, and no collision. Prove each is actually
    refused rather than merely described."""
    assert hmac_extra_key_violations({"old": "s" * 32}, "active") == []
    assert any("weak" in v for v in hmac_extra_key_violations({"old": "s" * 31}, "active"))
    assert any("blank" in v for v in hmac_extra_key_violations({"": "s" * 32}, "active"))
    assert any("collides" in v for v in hmac_extra_key_violations({"active": "s" * 32}, "active"))


# ── events and ingestion ──────────────────────────────────────────────────────────────────────────
def test_event_table_matches_the_payload_models_exactly():
    """Names AND field lists, both directions. The previous guard only checked that accepted names
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


def test_extra_field_policy_matches_the_models():
    claim = WIRE.value("WIRE.INGEST.EXTRA_FIELDS")
    assert EventEnvelope.model_config.get("extra") == claim["envelope"] == "forbid"
    for model in PAYLOAD_MODELS.values():
        assert model.model_config.get("extra") == claim["payload"] == "allow"


def test_documented_status_codes_are_the_ones_ingest_returns():
    """202 for a queued event and 200 for replay-or-inline are the pair most likely to be
    misbuilt, so they are asserted against the source of the outcomes themselves."""
    documented = dict(WIRE.value("WIRE.INGEST.STATUS"))
    source = (REPO / "src" / "kyc_tool" / "events" / "ingest.py").read_text()
    assert 202 in documented and 200 in documented
    assert "IngestOutcome(202" in source, "ingest no longer returns 202 for a queued event"
    assert "IngestOutcome(200" in source
    assert "queued" in documented[202]
    assert "replay" in documented[200].lower() and "inline" in documented[200].lower()
    for code in (400, 401, 409, 422, 404):
        assert code in documented


def test_sensitive_actor_rule_matches_the_guard():
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
def test_callback_fields_match_the_wire_model():
    required = {f for f, i in DecisionCallback.model_fields.items() if i.is_required()}
    optional = {f for f, i in DecisionCallback.model_fields.items() if not i.is_required()}
    assert set(WIRE.value("WIRE.CALLBACK.FIELDS")) == required
    assert set(WIRE.value("WIRE.CALLBACK.OPTIONAL_FIELDS")) == optional


def test_callback_gates_match_the_gates_model():
    assert set(WIRE.value("WIRE.CALLBACK.GATES")) == set(GatesBody.model_fields)


def test_documented_decisions_are_the_wire_literal():
    documented = set(WIRE.value("WIRE.CALLBACK.DECISIONS"))
    annotation = DecisionCallback.model_fields["decision"].annotation
    assert documented == set(annotation.__args__)


def test_retry_schedule_recomputes_from_the_shipped_backoff():
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


def test_delivery_claim_carries_all_three_outcomes():
    """A6 permits zero sends for a suppressed row, so an at-least-once-only description is an
    overclaim regardless of how it is worded."""
    text = " ".join(WIRE.value("WIRE.CALLBACK.DELIVERY")).lower()
    assert "at least once" in text and "zero times" in text and "dead-letter" in text
    assert "exactly once" not in text and "exactly one" not in text


def test_receiver_transaction_requires_commit_before_2xx():
    steps = [s.lower() for s in WIRE.value("WIRE.CALLBACK.RECEIVER_TXN")]
    commit = next(i for i, s in enumerate(steps) if s.startswith("commit"))
    respond = next(i for i, s in enumerate(steps) if "return 2xx" in s)
    assert commit < respond, "the receiver contract must commit BEFORE returning 2xx"


def test_wait_bound_says_detach_not_cancel():
    """The publisher cannot retract an in-flight request; calling the wall a cancellation would
    let the receiver assume a timed-out attempt never landed."""
    text = WIRE.value("WIRE.CALLBACK.WAIT_BOUND").lower()
    assert "stop waiting" in text and "detach" in text
    assert "may still reach you" in text
    assert "cancel" not in text


# ── ordering ──────────────────────────────────────────────────────────────────────────────────────
def test_decided_at_prohibition_still_has_repo_authority():
    """If the recorded prohibition is ever removed from the repo, this claim's authority is gone
    and the document must not keep asserting it."""
    runbook = (REPO / "docs" / "RUNBOOK.md").read_text()
    findings = (REPO / "AUDIT_FINDINGS.md").read_text()
    assert "fall back to" in runbook and "decided_at" in runbook
    assert "decided_at` is transaction-start time" in findings
    claim = WIRE.value("WIRE.ORDERING.NO_DECIDED_AT").lower()
    assert "not an ordering key" in claim and "transaction-start" in claim


def test_pending_claims_are_marked_pending_and_publish_no_schema():
    """024 is designed but unbuilt. The document must publish requirements, never a draft schema
    an integrator could implement."""
    for claim_id in ("WIRE.ORDERING.BOOTSTRAP_024", "WIRE.ORDERING.INTEGRITY_MISMATCH"):
        claim = WIRE[claim_id]
        assert claim.state is ClaimState.PENDING
    bootstrap = WIRE.value("WIRE.ORDERING.BOOTSTRAP_024")
    assert "NOT BUILT" in bootstrap
    for schema_ish in ("{", "}", "schema_version", "latest_run_id", "high_water_run_id"):
        assert schema_ish not in bootstrap, "the pending claim is publishing a schema again"


# ── retention ─────────────────────────────────────────────────────────────────────────────────────
def test_retention_claim_matches_the_per_kind_behaviour():
    retention = (REPO / "src" / "kyc_tool" / "workers" / "retention.py").read_text()
    documented = dict((kind, text) for kind, text in WIRE.value("WIRE.RETENTION.BY_KIND"))
    assert "redacted in place" in documented["decision callbacks"]
    poc = documented["POC verification emails"]
    assert "DELETED" in poc and "delivered or dead" in poc
    assert "delete" in retention.lower() and "redact" in retention.lower()
    assert WIRE.value("WIRE.RETENTION.WINDOW_DAYS") == Settings().retention_days


# ── operations ────────────────────────────────────────────────────────────────────────────────────
def test_the_production_blocker_is_demonstrably_true():
    """The strongest claim in either document, so it is proven by execution rather than by text:
    production refuses stubs, and every non-stub provider factory raises."""
    from kyc_tool.outbox.emails import make_email_sender
    from kyc_tool.workers.pipeline_worker import _make_ocr_engine, build_adapters

    assert OPERATIONS["OPS.BLOCKER.PRODUCTION_PROVIDERS"].state is ClaimState.BLOCKED

    # (a) production refuses the stub values
    from .test_production_config import hardened

    for stub in ({"ocr_engine": STUB_OCR_ENGINE}, {"email_provider": STUB_EMAIL_PROVIDER},
                 {"adapters_profile": STUB_ADAPTERS_PROFILE}):
        assert production_config_violations(hardened(**stub)), f"{stub} should be refused"

    # (b) and every production-safe value is unimplemented
    with pytest.raises(NotImplementedError):
        _make_ocr_engine("tesseract")
    with pytest.raises(NotImplementedError):
        make_email_sender("ses")
    with pytest.raises(NotImplementedError):
        build_adapters(Settings(adapters_profile="real"), store=None)


def test_process_commands_reference_real_modules():
    for _label, command, _note in OPERATIONS.value("OPS.PROCESS.COMMANDS"):
        if command.startswith("python -m "):
            module = command.split()[2]
            path = REPO / "src" / (module.replace(".", "/") + ".py")
            assert path.exists(), f"{command} names a module that does not exist"


def test_config_defaults_match_settings():
    settings = Settings()
    for field, documented in OPERATIONS.value("OPS.CONFIG.DEFAULTS").items():
        assert int(getattr(settings, field)) == documented, f"{field} default moved"


def test_hmac_variable_set_is_complete_and_enforced():
    documented = OPERATIONS.value("OPS.CONFIG.HMAC_SET")
    assert len(documented) == len(set(documented)) == 8
    from .test_production_config import hardened

    # dropping any one of the v2 values must produce a violation
    for override in ({"hmac_inbound_secret": ""}, {"hmac_outbound_secret": ""},
                     {"hmac_inbound_key_id": ""}, {"hmac_outbound_key_id": ""},
                     {"hmac_v1_inbound_sunset_at": ""}, {"hmac_v1_outbound_sunset_at": ""}):
        assert production_config_violations(hardened(**override)), override


def test_production_floors_are_actually_enforced():
    from .test_production_config import hardened

    assert any("job_lease_seconds" in v for v in production_config_violations(
        hardened(job_lease_seconds=29)))
    assert any("ui_admin_token" in v for v in production_config_violations(
        hardened(ui_admin_token="")))


def test_outbox_ceiling_cutover_matches_the_shipped_canonical_record():
    """Rendered from the same record DEPLOYMENT, RUNBOOK, and .env.example embed, in order."""
    documented = OPERATIONS.value("OPS.CUTOVER.OUTBOX_CEILING")
    canonical = cutover.render_cutover(cutover.OUTBOX_MAX_ATTEMPTS_CUTOVER)[1:]  # drop the title
    assert list(documented) == [line.split(". ", 1)[1] for line in canonical]


@pytest.mark.parametrize(
    ("claim_id", "first_words", "last_words"),
    [
        ("OPS.CUTOVER.FULL_WINDOW_STEPS", "publish the reviewed image", "resume platform"),
        ("OPS.CUTOVER.BUNDLE_PINNING_STEPS", "deploy the rolling part", "cas the durable epoch"),
        ("OPS.CUTOVER.PR7B_PREWINDOW_STEPS", "suspend the retention", "proceed with the window"),
    ],
)
def test_cutover_sequences_start_and_end_where_the_procedure_does(claim_id, first_words, last_words):
    steps = OPERATIONS.value(claim_id)
    assert first_words in steps[0].lower(), f"{claim_id} does not start at the documented step"
    assert last_words in steps[-1].lower(), f"{claim_id} does not end at the documented step"


def test_full_window_recovery_precedes_starting_any_worker():
    """Order, not membership: recovering interrupted jobs after a worker starts is the bug the
    ordered record exists to prevent."""
    steps = [s.lower() for s in OPERATIONS.value("OPS.CUTOVER.FULL_WINDOW_STEPS")]
    recover = next(i for i, s in enumerate(steps) if "recover interrupted" in s)
    start_workers = next(i for i, s in enumerate(steps) if s.startswith("start the new workers"))
    stop = next(i for i, s in enumerate(steps) if s.startswith("stop the api pool"))
    assert stop < recover < start_workers


def test_bundle_pinning_attestation_precedes_resume():
    steps = [s.lower() for s in OPERATIONS.value("OPS.CUTOVER.BUNDLE_PINNING_STEPS")]
    attest = next(i for i, s in enumerate(steps) if "bundle_pinning_ready" in s)
    resume = next(i for i, s in enumerate(steps) if s.startswith("resume"))
    assert attest < resume


def test_rollback_claim_forbids_the_staging_downgrade_myth():
    text = " ".join(OPERATIONS.value("OPS.ROLLBACK.MIGRATION_BOUNDARY")).lower()
    assert "unconditionally" in text and "including staging" in text
    assert "downgrade-freely" not in text.replace("there is no downgrade-freely rule anywhere", "")


def test_hmac_rollout_puts_the_v2_smoke_before_the_observation_clock():
    steps = [s.lower() for s in OPERATIONS.value("OPS.HMAC.ROLLOUT_ORDER")]
    smoke = next(i for i, s in enumerate(steps) if "v2-only callback" in s)
    clock = next(i for i, s in enumerate(steps) if "observation clock" in s)
    assert smoke < clock, "activating the observation clock before the v2 callback smoke"
    assert any("sign-off" in s for s in steps)


def test_m2_gate_matches_the_roadmap():
    # whitespace-normalized: the authoritative phrase wraps across source lines, and a raw
    # substring check would silently pass on its absence rather than its presence
    roadmap = re.sub(r"\s+", " ", (REPO / ".agents" / "ROADMAP.md").read_text())
    claim = OPERATIONS.value("OPS.CONFIG.M2_GATE").lower()
    assert "M4 complete" in roadmap and "staging E2E on real adapters" in roadmap
    for required in ("m4", "staging end-to-end", "real adapters", "platform cutover", "permanent"):
        assert required in claim
    assert "validator hardening" not in claim
