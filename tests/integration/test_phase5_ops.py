"""Phase 5 acceptance: chaos degrades to partial runs without wrong decisions,
crashes resume or dead-letter without partial-write corruption, dead-letters
alert (surfaced in metrics), and sustained concurrent load drains cleanly."""

import threading
import time

import pytest
from sqlalchemy import text

from kyc_tool.adapters.base import AdapterOutput, hash_inputs
from kyc_tool.config import ProcessRole
from kyc_tool.domain.models import AdapterStatus
from kyc_tool.orchestration.pipeline import Pipeline
from kyc_tool.queue import jobs
from kyc_tool.queue.worker import Worker
from kyc_tool.storage.object_store import FsStore
from kyc_tool.workers.retention import prune
from tests.conftest import bound_process
from tests.integration.shared import ACME_KYB

pytestmark = pytest.mark.postgres


def _worker_for(session_factory, pipeline, **kwargs) -> Worker:
    return Worker(
        session_factory,
        {"run_transition": pipeline.handle_job},
        backoff_base_seconds=0,
        on_dead_letter=pipeline.on_dead_letter,
        **kwargs,
    **bound_process(ProcessRole.PIPELINE_WORKER))


def test_crash_mid_decide_resumes_without_partial_writes(
    client, engine, post_event, session_factory, policy, settings, tmp_path
):
    """Kill the worker mid-run (decide txn raises once): the retry completes
    the run and exactly one decision exists — no partial writes survive."""
    crashes = {"left": 1}

    def crashing_builder(policy_bundle, ctx):
        if crashes["left"] > 0:
            crashes["left"] -= 1
            raise RuntimeError("simulated crash inside the decide transaction")
        return []

    pipeline = Pipeline(
        session_factory,
        policy,
        FsStore(tmp_path / "ev"),
        settings,
        adapters={},
        intent_builder=crashing_builder,
    )
    worker = _worker_for(session_factory, pipeline)

    response, _ = post_event("case-crash", "recalculate.requested", {})
    run_id = response.json()["run_id"]
    worker.run_until_idle()  # first attempt crashes, retry succeeds

    assert client.get(f"/v1/runs/{run_id}").json()["state"] == "PUBLISH_DECISION"
    with engine.connect() as conn:
        decisions = conn.execute(
            text("SELECT count(*) FROM decisions WHERE run_id=:r"), {"r": run_id}
        ).scalar_one()
        checks = conn.execute(
            text("SELECT count(*) FROM checks WHERE case_id='case-crash'")
        ).scalar_one()
    assert decisions == 1  # the crashed attempt left nothing behind
    assert checks == 0


def test_lease_expiry_requeues_and_another_worker_finishes(
    client, engine, post_event, session_factory, policy, settings, tmp_path
):
    response, _ = post_event("case-lease", "recalculate.requested", {})
    run_id = response.json()["run_id"]

    # worker A claims with a valid lease and then "dies". A zero lease is no longer a legal domain
    # value (re-audit R4-F3 — it minted an already-expired claim), so we force the claim's expiry
    # directly to simulate the crash rather than minting an out-of-domain lease.
    from kyc_tool.db.session import uow

    with uow(session_factory) as session:
        claimed = jobs.claim(session, ["run_transition"], "worker-a", lease_seconds=1)
    assert claimed is not None
    with uow(session_factory) as session:
        session.execute(text("UPDATE jobs SET lease_expires_at = now() - make_interval(secs => 1)"))

    with uow(session_factory) as session:
        jobs.reap_expired(session)  # crash recovery
    pipeline = Pipeline(
        session_factory, policy, FsStore(tmp_path / "ev"), settings, adapters={}
    )
    worker_b = _worker_for(session_factory, pipeline)
    assert worker_b.run_until_idle() >= 1
    assert client.get(f"/v1/runs/{run_id}").json()["state"] == "PUBLISH_DECISION"


def test_dead_letter_fails_run_and_surfaces_in_metrics(
    client, post_event, session_factory, policy, settings, tmp_path
):
    def always_crash(policy_bundle, ctx):
        raise RuntimeError("permanently broken")

    pipeline = Pipeline(
        session_factory,
        policy,
        FsStore(tmp_path / "ev"),
        settings,
        adapters={},
        intent_builder=always_crash,
    )
    worker = _worker_for(session_factory, pipeline)

    response, _ = post_event("case-dl", "recalculate.requested", {})
    run_id = response.json()["run_id"]
    with_session = session_factory()
    with_session.execute(text("UPDATE jobs SET max_attempts=2"))
    with_session.commit()
    with_session.close()

    worker.run_until_idle()
    run = client.get(f"/v1/runs/{run_id}").json()
    assert run["state"] == "FAILED"
    assert "permanently broken" in run["error"]

    metrics = client.get("/v1/metrics").json()
    assert metrics["jobs_by_status"].get("dead", 0) == 1  # the alerting signal
    assert metrics["runs_by_state"].get("FAILED", 0) == 1


class _Flaky500Adapter:
    adapter_id = "floqer_company_enrichment"

    def input_hash(self, case_snapshot, event):
        return hash_inputs(self.adapter_id, event)

    def run(self, case_snapshot, event) -> AdapterOutput:
        raise RuntimeError("Floqer 500")


class _OkEmailAdapter:
    adapter_id = "email_verification"

    def input_hash(self, case_snapshot, event):
        return hash_inputs(self.adapter_id, case_snapshot.get("email"))

    def run(self, case_snapshot, event) -> AdapterOutput:
        email = case_snapshot.get("email")
        if not email:
            return AdapterOutput(self.adapter_id, AdapterStatus.NOT_APPLICABLE)
        return AdapterOutput(
            self.adapter_id,
            AdapterStatus.OK,
            raw=b"{}",
            normalized={"verified": True, "email": email["email"], "domain": email["domain"]},
        )


def test_floqer_500_degrades_to_partial_not_wrong(
    client, post_event, session_factory, policy, settings, tmp_path
):
    """Chaos case: one upstream hard-down. The run goes partial, the healthy
    adapter's evidence still scores, and no wrong decision appears."""
    pipeline = Pipeline(
        session_factory,
        policy,
        FsStore(tmp_path / "ev"),
        settings,
        adapters={
            "floqer_company_enrichment": _Flaky500Adapter(),
            "email_verification": _OkEmailAdapter(),
        },
    )
    worker = _worker_for(session_factory, pipeline)
    post_event(
        "case-floqer500",
        "kyb.run_requested",
        {**ACME_KYB, "email": {"email": "ops@acme.example", "domain": "acme.example"}},
    )
    worker.run_until_idle()

    case = client.get("/v1/cases/case-floqer500").json()
    checks = {c["type"]: c["status"] for c in case["live_checks"]}
    assert checks.get("verified_email") == "pass"  # healthy evidence intact
    assert "linkedin_company_match" not in checks  # nothing invented
    assert case["latest_decision"] == "manual_review_insufficient"

    runs = client.get("/v1/metrics").json()["runs_by_state"]
    assert runs.get("PUBLISH_DECISION", 0) + runs.get("COMPLETE", 0) >= 1


def test_sustained_concurrent_load_drains_cleanly(
    client, engine, post_event, session_factory, policy, settings, tmp_path
):
    """Load: 24 cases through 4 concurrent workers — every run completes to a
    publishable state, nothing dead-letters, one decision per run."""
    case_count = 24
    for i in range(case_count):
        post_event(f"case-load-{i}", "kyb.run_requested", ACME_KYB)

    def make_worker():
        pipeline = Pipeline(
            session_factory, policy, FsStore(tmp_path / "ev"), settings, adapters={}
        )
        return _worker_for(session_factory, pipeline)

    started = time.monotonic()
    threads = [
        threading.Thread(target=lambda: make_worker().run_until_idle(max_jobs=200))
        for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - started

    with engine.connect() as conn:
        states = {
            row[0]: row[1]
            for row in conn.execute(text("SELECT state, count(*) FROM runs GROUP BY state"))
        }
        dead = conn.execute(text("SELECT count(*) FROM jobs WHERE status='dead'")).scalar_one()
        decisions = conn.execute(text("SELECT count(*) FROM decisions")).scalar_one()
    assert states.get("PUBLISH_DECISION", 0) == case_count  # all publishable
    assert dead == 0
    assert decisions == case_count  # exactly one decision per run — no races
    assert elapsed < 120, f"load run took {elapsed:.1f}s"


def test_retention_prunes_old_rows(engine, session_factory, clean_db):
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO audit_log (case_id, at, actor, action, detail_json) VALUES "
                "(NULL, now() - interval '8 years', 'system', 'ancient', '{}'), "
                "(NULL, now(), 'system', 'recent', '{}')"
            )
        )
    counts = prune(session_factory, retention_days=7 * 365)
    assert counts["audit_log"] == 1
    with engine.connect() as conn:
        remaining = [r.action for r in conn.execute(text("SELECT action FROM audit_log"))]
    assert "recent" in remaining and "ancient" not in remaining


def test_rate_limiter_spaces_calls():
    from kyc_tool.orchestration.rate_limit import RateLimiter

    limiter = RateLimiter({"rir_rdap": 50})  # 20ms spacing
    started = time.monotonic()
    for _ in range(4):
        limiter.acquire("rir_rdap")
    assert time.monotonic() - started >= 0.055  # 3 gaps × 20ms, minus jitter
    # uncapped adapters are never delayed
    started = time.monotonic()
    for _ in range(100):
        limiter.acquire("email_verification")
    assert time.monotonic() - started < 0.05
