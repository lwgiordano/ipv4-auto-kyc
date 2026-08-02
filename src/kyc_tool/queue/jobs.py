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

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from kyc_tool.config import require_numeric_domain
from kyc_tool.db.tables import Job
from kyc_tool.queue.backoff import saturating_backoff_seconds

_CLAIM_SQL = text(
    """
    UPDATE jobs
    SET status = 'running',
        locked_by = :worker_id,
        lease_expires_at = now() + make_interval(secs => :lease_seconds),
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
    RETURNING id, kind, case_id, payload_json, attempts, max_attempts
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


def enqueue(
    session: Session,
    kind: str,
    payload: dict,
    *,
    case_id: str | None = None,
    max_attempts: int = 5,
) -> Job:
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
    row = session.execute(
        _CLAIM_SQL, {"worker_id": worker_id, "lease_seconds": lease_seconds, "kinds": kinds}
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
    )


def complete(session: Session, job_id: int) -> None:
    """Mark done — call inside the handler's commit transaction so job
    completion is atomic with the work it performed."""
    session.execute(
        text("UPDATE jobs SET status='done', updated_at=now() WHERE id=:id"), {"id": job_id}
    )


def fail(session: Session, job: ClaimedJob, error: str, backoff_base_seconds: int) -> bool:
    """Record a failure. Returns True when the job dead-lettered."""
    # Consumer-layer domain re-check (re-audit R4-F3): a negative base makes run_after the PAST so the
    # job requeues immediately with no throttle; a large base × high attempts overflowed
    # make_interval. Refuse before any write so the job's state is unchanged on a bad base.
    require_numeric_domain("job_backoff_base_seconds", backoff_base_seconds)
    if job.attempts >= job.max_attempts:
        session.execute(
            text(
                "UPDATE jobs SET status='dead', last_error=:err, updated_at=now() WHERE id=:id"
            ),
            {"id": job.id, "err": error[:2000]},
        )
        return True
    # Saturating schedule (shared with the outbox): the shift is bounded before 2**shift is built and
    # the post-jitter delay is capped, so no attempt count overflows timestamp arithmetic.
    delay = saturating_backoff_seconds(backoff_base_seconds, job.attempts, jitter_fraction=0.25)
    session.execute(
        text(
            """
            UPDATE jobs
            SET status='queued', locked_by=NULL, lease_expires_at=NULL,
                last_error=:err, run_after = now() + make_interval(secs => :delay),
                updated_at=now()
            WHERE id=:id
            """
        ),
        {"id": job.id, "err": error[:2000], "delay": delay},
    )
    return False


def reap_expired(session: Session) -> list[ClaimedJob]:
    """Requeue running jobs whose lease expired (crashed worker); dead-letter
    the ones already out of attempts. Returns the dead-lettered jobs so the
    caller can fail their runs — the crash-safety invariant that a dead job's
    run is FAILED."""
    session.execute(
        text(
            """
            UPDATE jobs
            SET status='queued', locked_by=NULL, lease_expires_at=NULL, updated_at=now()
            WHERE status='running' AND lease_expires_at < now() AND attempts < max_attempts
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
            WHERE status='running' AND lease_expires_at < now() AND attempts >= max_attempts
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
