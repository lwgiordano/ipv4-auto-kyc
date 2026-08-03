"""The dead-letter requeue TRANSACTIONS, shared by the always-mounted ops router and the optional
/ui console (re-audit `f2929f8..6a4cd87` F3). The RUNBOOK's recovery path must exist in the secure
production configuration — previously it lived only under KYC_UI_ENABLED=true, so a dead job/outbox
in production 404'd until an operator enabled the debug/PII console.

Recovery is a single CAS (re-audit `7d1c435..827bc0f` F2/F4): the authority is ONE conditional
UPDATE whose predicate carries the full contract — still dead, and the fixed bounded grant still
fits int4 — with RETURNING deciding the winner. The pre-read exists only for friendly 404/409
messages; it authorizes nothing, so a recovery racing another recovery (or a worker claim) can
never clear a live nonce, double-grant budget, or double-write audit evidence."""

from fastapi import HTTPException
from sqlalchemy import text

from kyc_tool.config import PG_INT4_MAX, require_numeric_domain
from kyc_tool.db.audit import audit
from kyc_tool.db.session import uow


def requeue_dead_job(session_factory, job_id: int, *, attempt_grant: int) -> dict:
    """Atomically requeue a DEAD job with a FIXED bounded retry grant, and reset its FAILED run to
    QUEUED (the run reset is load-bearing: a requeued job whose run stays FAILED completes without
    doing anything). `max_attempts = attempts + grant` leaves exactly `grant` remaining attempts —
    the monotonic attempts counter is never rewound (nonce-ABA, audit honesty), and repeated
    exhaust/recover cycles grant exactly N each time instead of doubling (re-audit F4)."""
    require_numeric_domain("job_recovery_attempt_grant", attempt_grant)
    with uow(session_factory) as session:
        row = session.execute(
            text("SELECT status, attempts, case_id FROM jobs WHERE id=:id"), {"id": job_id}
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="job not found")
        if row.status != "dead":
            raise HTTPException(status_code=409, detail=f"job is {row.status}, not dead")
        if row.attempts > PG_INT4_MAX - attempt_grant:
            raise HTTPException(
                status_code=409,
                detail=f"attempts counter {row.attempts} cannot accept another grant of "
                f"{attempt_grant} within int4; nothing changed",
            )
        # THE authority: one CAS. Only the winner mutates the run and writes audit evidence; a
        # racing recovery/claim makes this match zero rows and refuses with a stable 409.
        won = session.execute(
            text(
                "UPDATE jobs SET status='queued', max_attempts = attempts + :grant, "
                "run_after=now(), locked_by=NULL, "
                "lease_expires_at=NULL, last_error=NULL, updated_at=now() "
                "WHERE id=:id AND status='dead' AND attempts <= :cap - :grant "
                "RETURNING payload_json, case_id"
            ),
            {"id": job_id, "grant": attempt_grant, "cap": PG_INT4_MAX},
        ).first()
        if won is None:
            raise HTTPException(
                status_code=409,
                detail="job changed while requeueing (recovered or claimed concurrently); "
                "nothing changed — re-inspect and retry if still dead",
            )
        run_id = (won.payload_json or {}).get("run_id")
        if run_id:
            session.execute(
                text(
                    "UPDATE runs SET state='QUEUED', error=NULL, finished_at=NULL "
                    "WHERE id=:run_id AND state='FAILED'"
                ),
                {"run_id": run_id},
            )
        audit(session, "job.requeued", case_id=won.case_id, run_id=run_id, job_id=job_id)
    return {"requeued": job_id, "run_reset": run_id, "attempts_granted": attempt_grant}


def requeue_dead_outbox(session_factory, outbox_id: int) -> dict:
    """Requeue a DEAD, unredacted outbox row. Redaction rides IN the UPDATE predicate so losing a
    race against retention is the honest 409, not a 500 from the migration-020 database guard."""
    with uow(session_factory) as session:
        row = session.execute(
            text("SELECT kind, status, case_id, run_id, payload_json FROM outbox WHERE id=:id"),
            {"id": outbox_id},
        ).first()
        if row is None or row.status != "dead":
            raise HTTPException(status_code=409, detail="outbox row not found or not dead")
        if (row.payload_json or {}).get("redacted"):
            remedy = ("re-submit the POC instead" if row.kind == "poc_email"
                      else "re-emit the decision (recalculate.requested) instead")
            raise HTTPException(
                status_code=409,
                detail=f"dead {row.kind} is redacted (body scrubbed); {remedy}",
            )
        applied = session.execute(
            text(
                "UPDATE outbox SET status='pending', attempts=0, next_attempt_at=now(), "
                "last_error=NULL WHERE id=:id AND status='dead' "
                "AND payload_json <> '{\"redacted\": true}'::jsonb"
            ),
            {"id": outbox_id},
        ).rowcount
        if not applied:
            raise HTTPException(
                status_code=409,
                detail="outbox row changed while requeueing (redacted or no longer dead); retry",
            )
        audit(session, "outbox.requeued", case_id=row.case_id, run_id=row.run_id, outbox_id=outbox_id)
    return {"requeued": outbox_id}
