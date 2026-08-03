"""Re-audit `3db5f13..a7df17b` unit-layer REDs: F4 heartbeat cadence strictly below lease/3 with a
production lease floor; F5 the plan budget strictly below the lease at EVERY accepted value and a
deadline-aware rate permit that refuses (without consuming the slot) what cannot fit; F2 the
in-process revocation boundary."""

import time

import pytest

from kyc_tool.adapters.retry import BudgetExhausted
from kyc_tool.config import Settings, production_config_violations
from kyc_tool.orchestration.pipeline import plan_budget_seconds
from kyc_tool.orchestration.rate_limit import RateLimiter
from kyc_tool.queue import jobs
from kyc_tool.queue.worker import heartbeat_cadence_seconds


# ── F4: cadence strictly below lease/3, never clamped upward ──────────────────────────────────────
@pytest.mark.parametrize("lease", [1, 2, 3, 4, 10, 30, 120, 3600])
def test_heartbeat_cadence_is_strictly_below_a_third_of_every_accepted_lease(lease):
    cadence = heartbeat_cadence_seconds(lease)
    assert cadence < lease / 3.0  # ROADMAP PR 7a ceiling, strict
    assert cadence > 0


def test_heartbeat_cadence_has_no_upward_clamp():
    """The audit's F4 witness: max(lease/3, 1s) scheduled the FIRST beat at a 1s lease's expiry and
    at lease/2 for a 2s lease. The cadence must scale down with the lease, floor-free."""
    assert heartbeat_cadence_seconds(1) == 0.25
    assert heartbeat_cadence_seconds(2) == 0.5


def test_production_floors_the_job_lease():
    """A 1-2s production lease leaves no room for heartbeat cadence + DB round-trip + the adapter
    plan's DB margin — the registry floors it at 30s in production."""
    violations = production_config_violations(_hardened(job_lease_seconds=5))
    assert any("job_lease_seconds" in v for v in violations)
    assert production_config_violations(_hardened(job_lease_seconds=30)) == []
    # development keeps the full domain (fast tests drive real expiry)
    Settings(environment="development", job_lease_seconds=2)


# ── F5: the plan budget is strictly below the lease at EVERY value ────────────────────────────────
@pytest.mark.parametrize("lease", [1, 2, 10, 11, 30, 120])
def test_plan_budget_is_strictly_below_every_lease(lease):
    budget = plan_budget_seconds(lease)
    assert 0 < budget < lease  # the old max(lease-10, 1) returned >= lease for lease <= 1
    assert budget >= lease * 0.5  # and never collapses to nothing for small leases


# ── F5: deadline-aware rate permits ───────────────────────────────────────────────────────────────
def test_rate_permit_that_cannot_fit_refuses_fast_without_consuming_the_slot():
    limiter = RateLimiter({"gleif": 5.0})  # 0.2s spacing (a registered adapter id — closed set)
    limiter.acquire("gleif")  # slot 1: immediate
    started = time.monotonic()
    with pytest.raises(BudgetExhausted):
        # slot 2 would land ~0.2s out; a deadline 0.01s out cannot fit — refuse WITHOUT sleeping
        limiter.acquire("gleif", deadline_monotonic=time.monotonic() + 0.01)
    assert time.monotonic() - started < 0.15  # refused immediately, did not wait out the interval
    # the refused permit did NOT consume the slot: a caller that fits still gets the ~0.2s one
    limiter.acquire("gleif", deadline_monotonic=time.monotonic() + 5.0)


def test_unlimited_adapters_ignore_the_deadline():
    RateLimiter({}).acquire("anything", deadline_monotonic=time.monotonic() - 100)  # no-op, no raise


# ── F2: the in-process revocation boundary ────────────────────────────────────────────────────────
def test_check_claim_live_raises_once_lost_is_set():
    job = jobs.ClaimedJob(id=1, kind="k", case_id=None, payload={}, attempts=1, max_attempts=5,
                          claim_nonce="w:abc")
    ctx = jobs.ClaimContext(job=job)
    with jobs.claim_scope(ctx):
        jobs.check_claim_live()  # live: no-op
        ctx.lost.set()
        with pytest.raises(jobs.StaleJobClaim):
            jobs.check_claim_live()
    jobs.check_claim_live()  # outside any claim scope: no-op (direct/manual callers)
    assert jobs.current_claim() is None


def _hardened(**overrides) -> Settings:
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
