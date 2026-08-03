"""PR 7a migration-free slice, re-worked in the `3db5f13..a7df17b` fold: ownership is a per-claim
NONCE (`locked_by='<worker>:<uuid4>'`), never the attempts counter — the supported ops requeue
rewinds nothing, so recovery can no longer recycle a stale worker's identity (the ABA the audit
witnessed). Loss of liveness REVOKES the handler: heartbeat miss/error sets `ClaimContext.lost`,
every pipeline boundary checks it, and every committing transaction proves the live nonce inside
itself. fail() returns a CLOSED result ('dead'/'requeued'/'stale') derived from the actual UPDATE."""

import threading
import time

import pytest
from sqlalchemy import text

from kyc_tool.db.session import uow
from kyc_tool.ops.requeue_service import requeue_dead_job
from kyc_tool.queue import jobs
from kyc_tool.queue.worker import Worker

pytestmark = pytest.mark.postgres


def _enqueue(session_factory, *, max_attempts=5):
    with uow(session_factory) as s:
        return jobs.enqueue(s, "run_transition", {}, case_id="dur", max_attempts=max_attempts).id


def _expire_and_reap(session_factory):
    with uow(session_factory) as s:
        s.execute(text("UPDATE jobs SET lease_expires_at = now() - interval '1 second'"))
    with uow(session_factory) as s:
        jobs.reap_expired(s)


def test_stale_worker_completes_nothing_and_live_claim_wins(session_factory, clean_db):
    _enqueue(session_factory)
    with uow(session_factory) as s:
        stale = jobs.claim(s, ["run_transition"], "worker-a", 120)
    _expire_and_reap(session_factory)
    with uow(session_factory) as s:
        live = jobs.claim(s, ["run_transition"], "worker-b", 120)
    assert live.claim_nonce != stale.claim_nonce  # a new claim identity
    assert live.claim_nonce.startswith("worker-b:")

    with uow(session_factory) as s:
        assert jobs.complete(s, stale) is False  # stale nonce commits nothing
    with session_factory() as s:
        assert s.execute(text("SELECT status FROM jobs")).scalar_one() == "running"  # live untouched
    with uow(session_factory) as s:
        assert jobs.complete(s, live) is True
    with session_factory() as s:
        assert s.execute(text("SELECT status FROM jobs")).scalar_one() == "done"


def test_stale_fail_cannot_clobber_the_live_claim(session_factory, clean_db):
    _enqueue(session_factory)
    with uow(session_factory) as s:
        stale = jobs.claim(s, ["run_transition"], "worker-a", 120)
    _expire_and_reap(session_factory)
    with uow(session_factory) as s:
        jobs.claim(s, ["run_transition"], "worker-b", 120)

    with uow(session_factory) as s:
        assert jobs.fail(s, stale, "stale boom", 5) == "stale"  # fenced: closed result, no write
    with session_factory() as s:
        st = s.execute(text("SELECT status, last_error FROM jobs")).one()
    assert st.status == "running" and st.last_error is None  # live claim intact


def test_stale_heartbeat_reports_lost(session_factory, clean_db):
    _enqueue(session_factory)
    with uow(session_factory) as s:
        stale = jobs.claim(s, ["run_transition"], "worker-a", 120)
    _expire_and_reap(session_factory)
    with uow(session_factory) as s:
        jobs.claim(s, ["run_transition"], "worker-b", 120)
    with uow(session_factory) as s:
        assert jobs.heartbeat(s, stale, 120) is False


# ── R8-F1 RED: the audit's exact ABA witness, now impossible ──────────────────────────────────────
def test_manual_requeue_cannot_recycle_a_dead_claims_ownership(session_factory, clean_db):
    """claim A (attempts=1, max_attempts=1) → expire/reap dead → ops requeue → claim B: A's
    complete/heartbeat/fail/assert_live must ALL affect zero rows while B completes exactly once,
    and the monotonic attempts counter is never rewound (the requeue grants budget instead)."""
    jid = _enqueue(session_factory, max_attempts=1)
    with uow(session_factory) as s:
        a = jobs.claim(s, ["run_transition"], "worker-a", 120)
    assert (a.attempts, a.max_attempts) == (1, 1)
    _expire_and_reap(session_factory)  # attempts >= max_attempts ⇒ DEAD
    with session_factory() as s:
        assert s.execute(text("SELECT status FROM jobs")).scalar_one() == "dead"

    requeue_dead_job(session_factory, jid, attempt_grant=1)  # the SUPPORTED recovery path
    with session_factory() as s:
        row = s.execute(text("SELECT status, attempts, max_attempts FROM jobs")).one()
    # fresh budget, monotonic counter NOT rewound — the audit's ABA precondition is gone
    assert (row.status, row.attempts, row.max_attempts) == ("queued", 1, 2)

    with uow(session_factory) as s:
        b = jobs.claim(s, ["run_transition"], "worker-b", 120)
    assert b is not None and b.claim_nonce != a.claim_nonce

    # every stale-A write is a fenced no-op…
    with uow(session_factory) as s:
        assert jobs.complete(s, a) is False
    with uow(session_factory) as s:
        assert jobs.heartbeat(s, a, 120) is False
    with uow(session_factory) as s:
        assert jobs.fail(s, a, "late boom", 5) == "stale"
    # …including the in-transaction liveness proof the decide/adapter txns use (F2)
    ctx = jobs.ClaimContext(job=a)
    with jobs.claim_scope(ctx), uow(session_factory) as s, pytest.raises(jobs.StaleJobClaim):
        jobs.assert_live(s)
    assert ctx.lost.is_set()

    with session_factory() as s:
        st = s.execute(text("SELECT status, locked_by, attempts FROM jobs")).one()
    assert (st.status, st.locked_by, st.attempts) == ("running", b.claim_nonce, 2)  # B untouched
    with uow(session_factory) as s:
        assert jobs.complete(s, b) is True  # B completes exactly once
    with session_factory() as s:
        assert s.execute(text("SELECT status FROM jobs")).scalar_one() == "done"


# ── R8-F3 RED: a fenced fail() miss after recovery is honest and undoes nothing ───────────────────
def test_stale_fail_after_recovery_emits_no_dead_letter_and_leaves_the_job_queued(
    session_factory, clean_db
):
    """Worker A's claim dead-letters and an operator requeues it while A is still executing. A's
    late failure must return 'stale', fire NO dead-letter callback (which would re-fail the
    recovered run), and leave the job queued exactly as recovery left it."""
    dead_letters = []
    jid = _enqueue(session_factory, max_attempts=1)

    def handler(job):
        # while A runs: its lease dies, the reaper dead-letters, the operator recovers
        _expire_and_reap(session_factory)
        requeue_dead_job(session_factory, jid, attempt_grant=1)
        raise RuntimeError("A finally fails, long after recovery")

    worker = Worker(
        session_factory, {"run_transition": handler},
        lease_seconds=120, backoff_base_seconds=0, poll_seconds=0.05,
        on_dead_letter=lambda job, err, session=None: dead_letters.append(job.id),
    )
    assert worker.run_once() is True
    assert dead_letters == []  # the fence miss must NOT report as a successful dead-letter
    with session_factory() as s:
        st = s.execute(text("SELECT status, locked_by, last_error FROM jobs")).one()
    assert st.status == "queued" and st.locked_by is None  # recovery preserved, nothing clobbered
    assert st.last_error is None  # A's late error was never written over the recovered row


# ── R8-F2/F4 RED: a heartbeat ERROR revokes the handler at its next boundary ──────────────────────
def test_heartbeat_error_sets_lost_and_revokes_the_handler(session_factory, clean_db):
    """A beat that cannot PROVE the lease extended (DB error) must set ClaimContext.lost; a handler
    honoring the boundary check then raises StaleJobClaim. The claim itself is still live in the DB
    (the outage was ours), so the fenced fail() honestly requeues the job for retry."""
    _enqueue(session_factory)
    main_thread = threading.current_thread()
    beats_broken = threading.Event()

    def factory():
        if beats_broken.is_set() and threading.current_thread() is not main_thread:
            raise RuntimeError("beat DB down")  # only the heartbeat thread sees the outage
        return session_factory()

    def handler(job):
        beats_broken.set()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            jobs.check_claim_live()  # the pipeline's per-boundary check (raises once lost is set)
            time.sleep(0.05)
        raise AssertionError("heartbeat error never revoked the handler")

    worker = Worker(
        session_factory, {"run_transition": handler},
        lease_seconds=2, backoff_base_seconds=0, poll_seconds=0.05,
    )
    worker.session_factory = factory  # claims/fails on the main thread still work
    assert worker.run_once() is True
    with session_factory() as s:
        st = s.execute(text("SELECT status, last_error, attempts FROM jobs")).one()
    assert st.status == "queued"  # honest retry: the claim was live, so the fenced fail applied
    assert "claim lost" in st.last_error
    assert st.attempts == 1


# ── R9-F1 RED: the in-txn fence is HELD, not peeked ───────────────────────────────────────────────
def test_held_fence_serializes_takeover_behind_the_commit(session_factory, clean_db):
    """A's assert_live LOCKS the job row and PostgreSQL holds it to commit: the whole rival
    takeover path (expire → reap → reclaim) BLOCKS until A's transaction ends — 'B owns while A
    commits' is impossible. Downgrading the fence to a plain SELECT lets the rival finish while A
    holds, which fails the ordering assertions below (the audit's prescribed mutation witness)."""
    _enqueue(session_factory)
    with uow(session_factory) as s:
        a = jobs.claim(s, ["run_transition"], "worker-a", 120)
    rival_done = threading.Event()
    order = []

    def rival_takeover():
        with uow(session_factory) as s:  # blocks on A's held row lock
            s.execute(text("UPDATE jobs SET lease_expires_at = now() - interval '1 second'"))
        with uow(session_factory) as s:
            jobs.reap_expired(s)
        with uow(session_factory) as s:
            assert jobs.claim(s, ["run_transition"], "worker-b", 120) is not None
        order.append("rival-owns")
        rival_done.set()

    rival = threading.Thread(target=rival_takeover)
    ctx = jobs.ClaimContext(job=a)
    with jobs.claim_scope(ctx), uow(session_factory) as s:
        jobs.assert_live(s)  # the HELD fence
        rival.start()
        assert not rival_done.wait(1.0), "rival took ownership while A held the fence"
        s.execute(
            text("UPDATE jobs SET last_error='A-COMMITTED-UNDER-FENCE' WHERE id=:i"),
            {"i": a.id},
        )
        order.append("A-committed")
    rival.join(timeout=15)
    assert order == ["A-committed", "rival-owns"]  # strict serialization, never interleaved
    with session_factory() as s:
        st = s.execute(text("SELECT status, locked_by, last_error FROM jobs")).one()
    assert st.status == "running" and st.locked_by.startswith("worker-b:")
    assert st.last_error == "A-COMMITTED-UNDER-FENCE"  # A's write landed BEFORE the takeover


def test_expired_lease_refuses_to_commit_even_before_the_reaper_runs(session_factory, clean_db):
    """R9-F1: the fence includes lease_expires_at > clock_timestamp() — authority is time-bounded,
    so an expired-but-unreaped claim rolls back instead of committing on a dead lease."""
    _enqueue(session_factory)
    with uow(session_factory) as s:
        a = jobs.claim(s, ["run_transition"], "worker-a", 120)
    with uow(session_factory) as s:
        s.execute(text("UPDATE jobs SET lease_expires_at = now() - interval '1 second'"))
    ctx = jobs.ClaimContext(job=a)
    with jobs.claim_scope(ctx), uow(session_factory) as s, pytest.raises(jobs.StaleJobClaim):
        jobs.assert_live(s)
    assert ctx.lost.is_set()


# ── R8-F2 RED: a reclaimed run commits nothing from the stale claimant ────────────────────────────
def test_lost_claim_stops_the_adapter_plan_and_commits_nothing(
    session_factory, pipeline, monkeypatch, clean_db
):
    """A (stale, reclaimed by B mid-fetch) must make NO LATER adapter call and commit no adapter
    row, object bytes, or state hop — the recording transaction's in-txn nonce proof aborts the
    plan the moment the first fetch lands."""
    from kyc_tool.adapters.base import AdapterOutput
    from kyc_tool.domain.models import AdapterStatus
    from kyc_tool.orchestration.triggers import RunPlan

    with session_factory() as s:
        s.execute(text("INSERT INTO cases (id) VALUES ('cf2')"))
        s.execute(
            text(
                "INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
                "actor_json, payload_json, event_sequence) "
                "VALUES ('e-f2', 'cf2', 'k-f2', 'h', 'kyb.run_requested', '{}'::jsonb, "
                "'{}'::jsonb, 1)"
            )
        )
        s.execute(
            text(
                "INSERT INTO runs (id, case_id, triggering_event_id, state, input_snapshot_json) "
                "VALUES ('r-f2', 'cf2', 'e-f2', 'RUN_ADAPTERS', '{}'::jsonb)"
            )
        )
        s.commit()
    with uow(session_factory) as s:
        jobs.enqueue(s, "run_transition", {"run_id": "r-f2"}, case_id="cf2")
    with uow(session_factory) as s:
        stale = jobs.claim(s, ["run_transition"], "worker-a", 120)

    calls = []

    class Probe:
        def __init__(self, aid):
            self.adapter_id = aid

        def input_hash(self, snapshot, event):
            return f"h-{self.adapter_id}"

        def run(self, snapshot, event):
            calls.append(self.adapter_id)
            if self.adapter_id == "p1":  # B reclaims WHILE A's first fetch is in flight
                _expire_and_reap(session_factory)
                with uow(session_factory) as s:
                    assert jobs.claim(s, ["run_transition"], "worker-b", 120) is not None
            return AdapterOutput(adapter_id=self.adapter_id, status=AdapterStatus.OK, raw=b"bytes")

    monkeypatch.setattr(pipeline, "adapters", {"p1": Probe("p1"), "p2": Probe("p2")})
    monkeypatch.setattr(
        "kyc_tool.orchestration.pipeline.plan_for",
        lambda et: RunPlan(adapters=("p1", "p2"), run_broker_gate=False, full=False),
    )
    puts, deletes = [], []
    monkeypatch.setattr(
        pipeline.object_store, "put", lambda key, data: puts.append(key) or f"ref-{key}"
    )
    monkeypatch.setattr(pipeline.object_store, "delete", deletes.append)

    ctx = jobs.ClaimContext(job=stale)
    with jobs.claim_scope(ctx), pytest.raises(jobs.StaleJobClaim):
        pipeline._run_adapters("r-f2")

    assert calls == ["p1"]  # the in-flight fetch finished; the LATER adapter never ran
    # R9-F1 contract: bytes are STAGED outside the fenced txn (the job lock is never held over
    # object-store I/O); the stale fence then orphan-cleans the staged object — net zero survive.
    assert len(puts) == 1 and deletes == [f"ref-{puts[0]}"]
    assert ctx.lost.is_set()
    with session_factory() as s:
        n = s.execute(text("SELECT count(*) FROM adapter_results WHERE run_id='r-f2'")).scalar_one()
        state = s.execute(text("SELECT state FROM runs WHERE id='r-f2'")).scalar_one()
    assert (n, state) == (0, "RUN_ADAPTERS")  # no adapter row, no state hop


# ── R9-F7: the budget probe runs through the REAL pipeline plumbing ───────────────────────────────
def test_pipeline_wires_the_governed_budget_through_real_plumbing(
    session_factory, pipeline, monkeypatch, clean_db
):
    """Replaces the vacuous unit probe the audit flagged: a probe adapter inside a REAL
    `_run_adapters` invocation asserts the ambient budget exists, its deadline sits strictly below
    the lease, and the send-authority hooks (deadline-aware permit, DB liveness proof, byte cap)
    are wired. Deleting `budget_scope()` from the pipeline makes `current_budget()` None here and
    this test fail — the mutation the old test survived."""
    import time as _time

    from kyc_tool.adapters import retry as retry_mod
    from kyc_tool.adapters.base import AdapterOutput
    from kyc_tool.domain.models import AdapterStatus
    from kyc_tool.orchestration.pipeline import plan_budget_seconds
    from kyc_tool.orchestration.triggers import RunPlan

    with session_factory() as s:
        s.execute(text("INSERT INTO cases (id) VALUES ('cf7')"))
        s.execute(
            text(
                "INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
                "actor_json, payload_json, event_sequence) "
                "VALUES ('e-f7', 'cf7', 'k-f7', 'h', 'kyb.run_requested', '{}'::jsonb, "
                "'{}'::jsonb, 1)"
            )
        )
        s.execute(
            text(
                "INSERT INTO runs (id, case_id, triggering_event_id, state, input_snapshot_json) "
                "VALUES ('r-f7', 'cf7', 'e-f7', 'RUN_ADAPTERS', '{}'::jsonb)"
            )
        )
        s.commit()
    with uow(session_factory) as s:
        jobs.enqueue(s, "run_transition", {"run_id": "r-f7"}, case_id="cf7")
    with uow(session_factory) as s:
        claimed = jobs.claim(s, ["run_transition"], "worker-a", 120)

    seen = {}

    class Probe:
        adapter_id = "pb"

        def input_hash(self, snapshot, event):
            return "h-pb"

        def run(self, snapshot, event):
            budget = retry_mod.current_budget()
            seen["budget"] = budget
            if budget is not None:
                seen["remaining"] = budget.deadline_monotonic - _time.monotonic()
                budget.prove_live()  # must pass for the LIVE claim driving this run
            return AdapterOutput(adapter_id="pb", status=AdapterStatus.OK)

    monkeypatch.setattr(pipeline, "adapters", {"pb": Probe()})
    monkeypatch.setattr(
        "kyc_tool.orchestration.pipeline.plan_for",
        lambda et: RunPlan(adapters=("pb",), run_broker_gate=False, full=False),
    )
    ctx = jobs.ClaimContext(job=claimed)
    with jobs.claim_scope(ctx):
        pipeline._run_adapters("r-f7")

    budget = seen["budget"]
    assert budget is not None  # deleting budget_scope() in the pipeline fails HERE
    assert 0 < seen["remaining"] <= plan_budget_seconds(pipeline.settings.job_lease_seconds)
    assert budget.acquire is not None and budget.prove_live is not None
    assert budget.max_response_bytes == pipeline.settings.adapter_max_response_bytes
    with session_factory() as s:
        n = s.execute(text("SELECT count(*) FROM adapter_results WHERE run_id='r-f7'")).scalar_one()
        state = s.execute(text("SELECT state FROM runs WHERE id='r-f7'")).scalar_one()
    assert (n, state) == (1, "VALIDATE")  # recorded under the held fence, hopped honestly


def test_heartbeat_keeps_a_slow_handler_alive_past_its_lease(session_factory, clean_db):
    """The residual itself: lease 2s, handler sleeps 5s. Without the heartbeat the reaper requeues
    mid-execution and a second claim double-executes; with it, the mid-run reap is a no-op and the
    job completes exactly once."""
    _enqueue(session_factory)
    reap_results = []

    def slow_handler(job):
        for _ in range(2):
            time.sleep(2.5)
            with uow(session_factory) as s:  # the reaper runs WHILE the handler is executing
                jobs.reap_expired(s)
                reap_results.append(
                    s.execute(text("SELECT status FROM jobs")).scalar_one()
                )

    worker = Worker(
        session_factory, {"run_transition": slow_handler},
        lease_seconds=2, backoff_base_seconds=0, poll_seconds=0.05,
    )
    assert worker.run_once() is True
    assert reap_results == ["running", "running"]  # never reaped mid-run — heartbeat held the lease
    with session_factory() as s:
        st = s.execute(text("SELECT status, attempts FROM jobs")).one()
    assert (st.status, st.attempts) == ("done", 1)  # exactly one execution
