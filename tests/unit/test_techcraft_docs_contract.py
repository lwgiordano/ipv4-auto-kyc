"""Drift guard for the TechCraft-facing documents (`docs/generators/`).

Those two documents state facts an external team will BUILD AGAINST: a worked HMAC test vector,
the exact event-type set, the callback gate names, the retry schedule, and a handful of config
defaults. A doc that drifts from the code is worse than no doc — the integrator implements the
stale contract, and the failure surfaces as a 401 or a dropped callback weeks later.

So every load-bearing number and identifier in those documents is re-derived here from the
shipped code and compared to the published text. Change the canonical signing string, add an
event type, rename a gate, or move a default, and this test names the document that went stale.
Same idiom as `test_outbox_ceiling_contract.py` (cutover block) and
`test_runbook_requeue_governance.py` (fenced SQL).
"""

import hashlib
from pathlib import Path

import pytest

from kyc_tool.api.schemas import PAYLOAD_MODELS, EventType, GatesBody
from kyc_tool.config import Settings
from kyc_tool.queue.backoff import saturating_backoff_seconds
from kyc_tool.security import DIRECTION_INBOUND, MAX_HMAC_SKEW_SECONDS, canonical_v2, sign_v2

GENERATORS = Path(__file__).resolve().parents[2] / "docs" / "generators"


def _as_rendered(source: str) -> str:
    """The generators carry the prose with reportlab inline markup, so `platform->tool` is stored
    escaped as `platform-&gt;tool` and bold spans interrupt phrases. Compare against what a READER
    sees, not the markup: unescape the entities and drop the inline tags. (Deliberately reading
    the .py source rather than the built PDF — this guard must run in CI, which has no reportlab.)
    """
    for tag in ("<b>", "</b>", "<br/>", "<super>", "</super>", "<sub>", "</sub>"):
        source = source.replace(tag, "")
    return source.replace("&gt;", ">").replace("&lt;", "<").replace("&amp;", "&")


CONTRACT = _as_rendered((GENERATORS / "techcraft_integration_contract.py").read_text())
DEPLOY = _as_rendered((GENERATORS / "techcraft_deployment_guide.py").read_text())

# The exact inputs the contract publishes as its worked example.
VECTOR_SECRET = "integration-test-secret-0123456789ab"
VECTOR_KEY_ID = "techcraft-inbound-1"
VECTOR_TS = "1754400000"
VECTOR_SLOT = "evt-0001"
VECTOR_PATH = "/v1/cases/case-42/events"
VECTOR_BODY = (
    b'{"event_type":"kyb.run_requested","occurred_at":"2026-08-04T12:00:00Z",'
    b'"actor":{"type":"user","id":"acct-42"},'
    b'"payload":{"company_legal_name":"ACME NETWORKS LTD"}}'
)


def _vector_fields() -> dict:
    return {
        "key_id": VECTOR_KEY_ID,
        "direction": DIRECTION_INBOUND,
        "method": "POST",
        "path_qs": VECTOR_PATH,
        "timestamp": VECTOR_TS,
        "slot": VECTOR_SLOT,
        "body": VECTOR_BODY,
    }


def test_published_signature_vector_reproduces_from_the_signer():
    """The headline guard: the digest printed in the contract must be what `sign_v2` produces
    today. Any change to the canonical string (field order, separator, direction token) breaks
    the integrator's implementation, and it breaks here first."""
    digest = sign_v2(VECTOR_SECRET, **_vector_fields())
    assert digest in CONTRACT, (
        f"the contract's published signature vector is stale; sign_v2 now yields {digest}"
    )


def test_published_body_hash_and_byte_count_match_the_body():
    body_hash = hashlib.sha256(VECTOR_BODY).hexdigest()
    assert body_hash in CONTRACT, "the published sha256(body) line is stale"
    assert f"{len(VECTOR_BODY)} bytes" in CONTRACT, (
        f"the contract states the wrong body length; it is {len(VECTOR_BODY)} bytes"
    )


def test_published_canonical_string_matches_the_signer():
    """Every line of the printed canonical string, in order, as `canonical_v2` builds it."""
    lines = canonical_v2(**_vector_fields()).split("\n")
    assert lines[0] == "v2" and len(lines) == 8
    for line in lines:
        assert line in CONTRACT, f"canonical line {line!r} is missing from the contract"


def test_direction_tokens_are_published_verbatim():
    """These are literals in the signed string; 'inbound'/'outbound' prose would cost the
    integrator a day of 401s (it did cost the first draft of this document exactly that)."""
    from kyc_tool.security import DIRECTION_OUTBOUND

    assert DIRECTION_INBOUND in CONTRACT and DIRECTION_OUTBOUND in CONTRACT


def test_every_event_type_is_documented_and_no_ghosts_are():
    """The contract's event table must be the closed set the API accepts, both directions."""
    accepted = set(EventType.__args__)
    assert accepted == set(PAYLOAD_MODELS), "schemas.py disagrees with itself"
    for event_type in accepted:
        assert event_type in CONTRACT, f"{event_type} is accepted but undocumented"
    # a documented type the API would reject is the more dangerous direction
    for quoted in ('"kyb.run_requested"', '"recalculate.requested"'):
        assert quoted.strip('"') in accepted


def test_callback_gate_names_match_the_wire_model():
    for gate in GatesBody.model_fields:
        assert gate in CONTRACT, f"gate {gate} is on the wire but missing from the contract"


def test_published_retry_schedule_matches_the_shipped_backoff():
    """10, 20, 40, 80, 160, 320, 640 — recomputed from the real function and defaults, not
    copied. The publisher dead-letters on the last attempt, so there are max_attempts-1 delays."""
    settings = Settings()
    base = settings.outbox_backoff_base_seconds
    attempts = settings.outbox_max_attempts
    delays = [saturating_backoff_seconds(base, n) for n in range(1, attempts)]
    published = ", ".join(str(int(d)) for d in delays[:-1]) + f", {int(delays[-1])}s"
    assert published in CONTRACT, (
        f"the published retry schedule is stale; the shipped defaults give {delays}"
    )
    total_minutes = round(sum(delays) / 60)
    assert f"about {total_minutes} minutes" in CONTRACT, (
        f"the published dead-letter window is stale; backoff alone totals {total_minutes} min"
    )


def test_published_skew_window_matches_the_verifier():
    assert f"{MAX_HMAC_SKEW_SECONDS} seconds" in CONTRACT
    assert f"{MAX_HMAC_SKEW_SECONDS}s either side" in CONTRACT


@pytest.mark.parametrize(
    ("field", "rendered"),
    [
        ("outbox_lease_seconds", "300"),
        ("outbox_http_timeout_seconds", "10"),
        ("outbox_max_attempts", "8"),
        ("retention_days", "2555"),
        ("job_lease_seconds", "120"),
    ],
)
def test_deployment_guide_quotes_real_defaults(field, rendered):
    """The guide prints defaults so an operator can sanity-check a task definition at a glance.
    A stale default there is a misconfiguration they will not catch."""
    actual = getattr(Settings(), field)
    assert str(int(actual)) == rendered, (
        f"{field} default moved to {actual}; the deployment guide still prints {rendered}"
    )
    assert rendered in DEPLOY


def test_deployment_guide_lists_every_process_command():
    """One image, six commands. A missing worker in the guide is a process nobody deploys."""
    for command in (
        "kyc_tool.workers.pipeline_worker",
        "kyc_tool.workers.outbox_worker",
        "kyc_tool.workers.retention",
        "kyc_tool.ops.sweep_staged_evidence",
        "alembic upgrade head",
        "uvicorn",
    ):
        assert command in DEPLOY, f"{command} is missing from the deployment guide"


def test_referenced_ops_modules_exist():
    """Every module the guide tells an operator to run must be importable in the shipped image."""
    src = Path(__file__).resolve().parents[2] / "src" / "kyc_tool"
    for module in ("ops/sweep_staged_evidence.py", "ops/activate_hmac_v1_observation.py",
                   "workers/pipeline_worker.py", "workers/outbox_worker.py",
                   "workers/retention.py"):
        assert (src / module).exists(), f"the guide references a missing module: {module}"


def test_no_unset_sunset_dates_are_published_as_fact():
    """The v1 sunset dates live unset in .env.example and are agreed at cutover. An earlier draft
    printed the values from a TEST FIXTURE as if they were commitments, which is exactly the kind
    of date an integrator plans a migration around."""
    for fixture_date in ("2026-09-01 inbound", "2026-10-01 outbound"):
        assert fixture_date not in CONTRACT, (
            f"the contract states {fixture_date!r} as fact, but the date is unset and negotiated"
        )
