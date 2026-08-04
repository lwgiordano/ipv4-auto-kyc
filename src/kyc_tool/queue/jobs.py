"""Hand-rolled durable Postgres job queue.

Design (validated in the architecture review):
- Claim = short transaction: FOR UPDATE SKIP LOCKED + lease columns; work runs
  OUTSIDE any transaction; completion happens inside the handler's commit txn.
- Per-case FIFO: a job with a case_id is claimable only when it is the OLDEST
  not-done job for that case — this both serializes runs per case (supersession
  chains can't race) and guarantees per-case event ordering.
- Crash safety: an expired lease is requeued by the reaper; a job that exhausts
  max_attempts dead-letters (and its run is failed by the caller).
"""

import threading
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.orm import Session

from kyc_tool.config import require_numeric_domain
from kyc_tool.db.tables import Job
from kyc_tool.queue.backoff import saturating_backoff_seconds

_CLAIM_SQL = text(
    """
    UPDATE jobs
    SET status = 'running',
        locked_by = :claim_nonce,
        lease_expires_at = clock_timestamp() + make_interval(secs => :lease_seconds),
        attempts = attempts + 1,
        updated_at = now()
    WHERE id = (
        SELECT j.id
        FROM jobs j
        WHERE j.status = 'queued'
          AND j.run_after <= now()
          AND j.kind = ANY(:kinds)
          AND (
                j.case_id IS NULL
                OR j.id = (
                    SELECT min(j2.id) FROM jobs j2
                    WHERE j2.case_id = j.case_id
                      AND j2.status IN ('queued', 'running')
                )
              )
        ORDER BY j.id
        FOR UPDATE OF j SKIP LOCKED
        LIMIT 1
    )
    RETURNING id, kind, case_id, payload_json, attempts, max_attempts, locked_by
    """
)


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    id: int
    kind: str
    case_id: str | None
    payload: dict
    attempts: int
    max_attempts: int
    # The claim NONCE (re-audit `3db5f13..a7df17b` F1): `locked_by = '<worker>:<uuid4>'`, unique per
    # claim. `attempts` alone had an ABA hole — the supported ops requeue reset it, recycling the
    # generation — so ownership is the nonce; attempts stays as the monotonic retry counter.
    claim_nonce: str = ""


def enqueue(
    session: Session,
    kind: str,
    payload: dict,
    *,
    case_id: str | None = None,
    max_attempts: int = 5,
) -> Job:
    # Consumer-layer domain guard (re-audit `03dbfab..bc325e7` R5-F7): jobs.max_attempts is int4 with
    # no DB CHECK yet, so a negative/zero/bool max_attempts would otherwise commit an invalid budget.
    # Refuse before add/flush; the row is never created.
    require_numeric_domain("job_max_attempts", max_attempts)
    job = Job(kind=kind, case_id=case_id, payload_json=payload, max_attempts=max_attempts)
    session.add(job)
    session.flush()
    return job


def claim(session: Session, kinds: list[str], worker_id: str, lease_seconds: int) -> ClaimedJob | None:
    """Claim one runnable job. Caller owns the (short) transaction."""
    # Consumer-layer domain re-check (re-audit R4-F3): a nonpositive lease mints an ALREADY-EXPIRED
    # claim the reaper immediately requeues — a second worker then claims the same case job,
    # defeating per-case serialization; an overflowing lease raises DatetimeFieldOverflow inside
    # make_interval, outside the handler boundary. Refuse before the UPDATE, leaving the row untouched.
    require_numeric_domain("job_lease_seconds", lease_seconds)
    claim_nonce = f"{worker_id}:{uuid.uuid4().hex}"  # unique per CLAIM, not per worker (F1)
    row = session.execute(
        _CLAIM_SQL, {"claim_nonce": claim_nonce, "lease_seconds": lease_seconds, "kinds": kinds}
    ).first()
    if row is None:
        return None
    return ClaimedJob(
        id=row.id,
        kind=row.kind,
        case_id=row.case_id,
        payload=row.payload_json,
        attempts=row.attempts,
        max_attempts=row.max_attempts,
        claim_nonce=row.locked_by,
    )


@dataclass
class ClaimContext:
    """The live claim capability (re-audit `3db5f13..a7df17b` F2): the worker publishes it for the
    duration of the handler; heartbeat loss/error sets `lost`, and every transition/adapter boundary
    plus every committing transaction proves liveness through it. A stale claimant is REVOKED at the
    next boundary instead of running to completion on side effects."""

    job: ClaimedJob
    lost: threading.Event = field(default_factory=threading.Event)


_CURRENT_CLAIM: ContextVar[ClaimContext | None] = ContextVar("queue_claim_context", default=None)


@contextmanager
def claim_scope(ctx: ClaimContext):
    token = _CURRENT_CLAIM.set(ctx)
    try:
        yield ctx
    finally:
        _CURRENT_CLAIM.reset(token)


def current_claim() -> ClaimContext | None:
    return _CURRENT_CLAIM.get()


def check_claim_live() -> None:
    """Cheap in-process boundary check (no DB): raises when the heartbeat marked the claim lost."""
    ctx = _CURRENT_CLAIM.get()
    if ctx is not None and ctx.lost.is_set():
        raise StaleJobClaim(f"job {ctx.job.id} claim lost (heartbeat) — revoking before next step")


def assert_live(session: Session) -> None:
    """In-TRANSACTION liveness AUTHORITY (F2; hardened per re-audit `7d1c435..827bc0f` F1): a HELD
    fence, not a peek. `FOR UPDATE` takes the job ROW LOCK under the exact nonce + running status +
    an UNEXPIRED lease, and PostgreSQL holds that lock until this transaction ends — the reaper, a
    rival claim, and manual recovery all mutate this same row, so they serialize BEHIND the commit
    this proof authorizes; "proved live" cannot go stale between the check and the commit. A miss
    (reaped/reclaimed/recovered/EXPIRED — an expired-but-unreaped claim has no authority either)
    raises StaleJobClaim so the whole transaction rolls back. No ambient claim = no-op (direct/
    manual callers own their own fencing).

    LOCK ORDER: call this FIRST in the transaction, before any Case/Run/Task lock — the one order
    everywhere is job → case → run/task. Never hold this lock over network or object-store I/O."""
    ctx = _CURRENT_CLAIM.get()
    if ctx is None:
        return
    if ctx.lost.is_set():
        raise StaleJobClaim(f"job {ctx.job.id} claim lost (heartbeat) — rolling back")
    live = session.execute(
        text(
            "SELECT 1 FROM jobs WHERE id=:id AND status='running' AND locked_by=:nonce "
            "AND lease_expires_at > clock_timestamp() FOR UPDATE"
        ),
        {"id": ctx.job.id, "nonce": ctx.job.claim_nonce},
    ).first()
    if live is None:
        ctx.lost.set()
        raise StaleJobClaim(
            f"job {ctx.job.id} nonce is no longer live (or its lease expired) — "
            "rolling back this transaction"
        )


def prove_live_for_send(session_factory) -> None:
    """SEND authorization (re-audit `7d1c435..827bc0f` F5): a cheap, lock-free DB re-proof of the
    ambient claim invoked immediately before every physical upstream call (after the rate permit,
    which may have waited a long time). Unlike assert_live this holds NOTHING — it authorizes an
    external side effect, not a commit — so a stale worker stops calling upstreams at the next
    send even though revocation of its writes still rests on the held in-txn fence. A miss sets
    `lost` and raises StaleJobClaim; no ambient claim = no-op (direct unit calls)."""
    check_claim_live()
    ctx = _CURRENT_CLAIM.get()
    if ctx is None:
        return
    try:
        with session_factory() as session:
            live = session.execute(
                text(
                    "SELECT 1 FROM jobs WHERE id=:id AND status='running' AND locked_by=:nonce "
                    "AND lease_expires_at > clock_timestamp()"
                ),
                {"id": ctx.job.id, "nonce": ctx.job.claim_nonce},
            ).first()
    except StaleJobClaim:
        raise
    except Exception as exc:
        # FAIL CLOSED (re-audit `750630c..ca85355` F4): "cannot prove the claim is live" is an
        # AUTHORITY failure, not evidence about the upstream — it must never be converted into
        # AdapterStatus.UPSTREAM_ERROR / a partial run by the pipeline's generic handler.
        ctx.lost.set()
        raise StaleJobClaim(
            f"job {ctx.job.id}: claim authority UNAVAILABLE "
            f"({exc.__class__.__name__}: {exc}) — failing closed, no upstream call"
        ) from exc
    if live is None:
        ctx.lost.set()
        raise StaleJobClaim(f"job {ctx.job.id} claim is no longer live — refusing to call upstream")


class StaleJobClaim(RuntimeError):
    """This worker's claim generation is no longer the live one (the reaper requeued the job and
    another worker claimed it). The caller MUST let its transaction roll back — a stale worker's
    decision/write must never commit (PR 7a fencing, migration-free slice)."""


def complete(session: Session, job: ClaimedJob) -> bool:
    """Fenced completion — call inside the handler's commit transaction so job completion is atomic
    with the work it performed. The fence is the CLAIM GENERATION: `attempts` increments on every
    claim, so a worker that lost its lease (reaped + reclaimed) matches zero rows here and returns
    False — the decide transaction must then abort via StaleJobClaim rather than commit a duplicate
    decision (PR 7a, migration-free slice; the dedicated lease_token column lands with migration 026)."""
    applied = session.execute(
        text(
            "UPDATE jobs SET status='done', updated_at=now() "
            "WHERE id=:id AND status='running' AND locked_by=:nonce"
        ),
        {"id": job.id, "nonce": job.claim_nonce},
    ).rowcount
    return applied > 0


def fail(session: Session, job: ClaimedJob, error: str, backoff_base_seconds: int) -> str:
    """Record a failure under the claim-nonce fence. Returns a CLOSED result derived from the actual
    UPDATE (re-audit `3db5f13..a7df17b` F3 — never from the caller's stale snapshot):
    'dead' (this call dead-lettered it), 'requeued' (retry scheduled), or 'stale' (fence miss — the
    claim was reaped/recovered/reclaimed; NOTHING was written and the caller must NOT dead-letter
    the run or emit any terminal side effect)."""
    # Consumer-layer domain re-check (re-audit R4-F3): a negative base makes run_after the PAST so the
    # job requeues immediately with no throttle; a large base × high attempts overflowed
    # make_interval. Refuse before any write so the job's state is unchanged on a bad base.
    require_numeric_domain("job_backoff_base_seconds", backoff_base_seconds)
    # Both failure writebacks carry the SAME claim-generation fence as complete() (PR 7a slice): an
    # unfenced fail() from a stale worker would requeue/dead-letter a job another worker now owns,
    # clobbering the live claim. Zero rows updated = stale; leave the live claim untouched.
    if job.attempts >= job.max_attempts:
        applied = session.execute(
            text(
                "UPDATE jobs SET status='dead', locked_by=NULL, lease_expires_at=NULL, "
                "last_error=:err, updated_at=now() "
                "WHERE id=:id AND status='running' AND locked_by=:nonce RETURNING id"
            ),
            {"id": job.id, "err": error[:2000], "nonce": job.claim_nonce},
        ).rowcount
        return "dead" if applied else "stale"
    # Saturating schedule (shared with the outbox): the shift is bounded before 2**shift is built and
    # the post-jitter delay is capped, so no attempt count overflows timestamp arithmetic.
    delay = saturating_backoff_seconds(backoff_base_seconds, job.attempts, jitter_fraction=0.25)
    applied = session.execute(
        text(
            """
            UPDATE jobs
            SET status='queued', locked_by=NULL, lease_expires_at=NULL,
                last_error=:err, run_after = now() + make_interval(secs => :delay),
                updated_at=now()
            WHERE id=:id AND status='running' AND locked_by=:nonce
            """
        ),
        {"id": job.id, "err": error[:2000], "delay": delay, "nonce": job.claim_nonce},
    ).rowcount
    return "requeued" if applied else "stale"


def heartbeat(session: Session, job: ClaimedJob, lease_seconds: int) -> bool:
    """Extend the live claim's lease under the SAME claim-nonce fence (PR 7a slice). Returns
    False when the claim was lost (reaped/reclaimed) — the caller stops heartbeating; the fenced
    complete()/fail() then guarantee the stale worker commits nothing.

    LOCK-THEN-EXTEND (re-audit `750630c..ca85355` F5): `now()` is transaction-start time, so a
    heartbeat that waited on the row lock longer than the lease "succeeded" while writing an
    expiry already in the past — and even `clock_timestamp()` in a single UPDATE's SET list is
    projected BEFORE a lock wait (EvalPlanQual re-checks quals, not volatile SET expressions;
    measured on real Postgres). The fenced SELECT FOR UPDATE absorbs the wait first; the UPDATE
    then starts fresh, so its `clock_timestamp()` is genuinely post-wait and the extension is
    real no matter how long the beat blocked."""
    require_numeric_domain("job_lease_seconds", lease_seconds)
    held = session.execute(
        text(
            "SELECT 1 FROM jobs WHERE id=:id AND status='running' AND locked_by=:nonce FOR UPDATE"
        ),
        {"id": job.id, "nonce": job.claim_nonce},
    ).first()
    if held is None:
        return False
    applied = session.execute(
        text(
            "UPDATE jobs SET lease_expires_at = clock_timestamp() + make_interval(secs => :lease), "
            "updated_at=now() WHERE id=:id AND status='running' AND locked_by=:nonce"
        ),
        {"id": job.id, "lease": lease_seconds, "nonce": job.claim_nonce},
    ).rowcount
    return applied > 0


def reap_expired(session: Session) -> list[ClaimedJob]:
    """Requeue running jobs whose lease expired (crashed worker); dead-letter
    the ones already out of attempts. Returns the dead-lettered jobs so the
    caller can fail their runs — the crash-safety invariant that a dead job's
    run is FAILED."""
    # clock_timestamp() (R10-F5): expiry is judged against the WALL clock, matching how leases are
    # minted/extended — a reaper transaction that waited on locks must not judge with stale now().
    session.execute(
        text(
            """
            UPDATE jobs
            SET status='queued', locked_by=NULL, lease_expires_at=NULL, updated_at=now()
            WHERE status='running' AND lease_expires_at < clock_timestamp()
              AND attempts < max_attempts
            """
        )
    )
    dead_rows = session.execute(
        text(
            """
            UPDATE jobs
            SET status='dead', locked_by=NULL, lease_expires_at=NULL,
                last_error = coalesce(last_error, 'lease expired; attempts exhausted'),
                updated_at=now()
            WHERE status='running' AND lease_expires_at < clock_timestamp()
              AND attempts >= max_attempts
            RETURNING id, kind, case_id, payload_json, attempts, max_attempts
            """
        )
    ).fetchall()
    return [
        ClaimedJob(
            id=r.id,
            kind=r.kind,
            case_id=r.case_id,
            payload=r.payload_json,
            attempts=r.attempts,
            max_attempts=r.max_attempts,
        )
        for r in dead_rows
    ]
