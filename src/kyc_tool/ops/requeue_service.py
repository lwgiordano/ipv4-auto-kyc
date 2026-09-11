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
from kyc_tool.configuration import repo as configuration_repo
from kyc_tool.configuration.models import ConfigurationUnavailable
from kyc_tool.db.audit import audit
from kyc_tool.db.session import uow


def requeue_dead_job(session_factory, job_id: int, *, attempt_grant: int) -> dict:
    """Atomically requeue a DEAD job with a FIXED bounded retry grant, and reset its FAILED run to
    QUEUED (the run reset is load-bearing: a requeued job whose run stays FAILED completes without
    doing anything). `max_attempts = attempts + grant` leaves exactly `grant` remaining attempts —
    the monotonic attempts counter is never rewound (nonce-ABA, audit honesty), and repeated
    exhaust/recover cycles grant exactly N each time instead of doubling (re-audit F4).

    CASE-ORDERING AUTHORITY (re-audit `750630c..ca85355` F1; HELD, not observed, per
    `ddbff39..c3884bd` F1): recovery may only resurrect the LATEST job of its case, and only while
    nothing for the case is running — and it proves both UNDER the same case FOR UPDATE lock
    ingest admits new events through, so a newer event either landed before the lock (refused) or
    waits behind this recovery's commit. A recovered OLDER job passes the claim gate's
    min(queued,running) check itself, so recovering it beside newer case state replayed frozen
    old evidence. Old evidence is re-processed by submitting a fresh `recalculate.requested`
    event, never by replaying its dead job. (DB backstop — partial unique index on jobs(case_id)
    WHERE status='running' — is reserved into migration 026 with the lease_token column.)

    RUN BINDING (F6): the run reset is verified, not fire-and-forget — the run must EXIST, belong
    to THIS job's case, and be FAILED; anything else rolls the whole recovery back with a governed
    409 (a dead job pointing at a COMPLETE run or another case's run recovers nothing)."""
    require_numeric_domain("job_recovery_attempt_grant", attempt_grant)
    with uow(session_factory) as session:
        try:
            configuration = configuration_repo.get_active(session, lock=True)
        except ConfigurationUnavailable as exc:
            raise HTTPException(503, "Configuration authority unavailable; recovery refused.") from exc
        row = session.execute(
            text("SELECT status, attempts, case_id, payload_json FROM jobs WHERE id=:id"),
            {"id": job_id},
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
        run_id = (row.payload_json or {}).get("run_id")
        if not run_id:
            raise HTTPException(
                status_code=409,
                detail="job payload carries no run_id (unsupported shape); refusing to requeue "
                "a job whose run cannot be verified",
            )
        if configuration is not None:
            pin = session.execute(
                text("SELECT configuration_revision FROM runs WHERE id=:r"), {"r": run_id}
            ).scalar_one_or_none()
            if pin is None:
                raise HTTPException(409, "Active configuration refuses unversioned legacy job recovery.")
            try:
                configuration_repo.get_revision(session, pin)
            except ConfigurationUnavailable as exc:
                raise HTTPException(503, "Pinned configuration unavailable; recovery refused.") from exc
        if row.case_id is not None:
            # HOLD the case-order authority, don't observe it (re-audit `ddbff39..c3884bd` F1):
            # ingest locks this same case row FOR UPDATE before admitting a new event/job, so
            # taking it here makes recovery and ingest strictly serial — a newer same-case
            # event/job either committed BEFORE the lock (the re-checks below see it and refuse)
            # or waits until this recovery commits. Plain SELECTs allowed a new event to commit
            # mid-recovery and the resurrected old job to run ahead of it.
            # Lock order: recovery is case → (dead) job; workers are (running) job → case. No
            # cycle is possible because the two sides can never contend for the same job row —
            # recovery's CAS requires status='dead' while a worker only ever holds
            # status='running' rows, and their run rows belong to different runs.
            session.execute(
                text("SELECT 1 FROM cases WHERE id=:c FOR UPDATE"), {"c": row.case_id}
            )
            # AUTHORITATIVE re-checks, now under the held lock (the pre-lock reads above are
            # only message-quality fast paths):
            newer = session.execute(
                text("SELECT id, status FROM jobs WHERE case_id=:c AND id > :id "
                     "ORDER BY id DESC LIMIT 1"),
                {"c": row.case_id, "id": job_id},
            ).first()
            if newer is not None:
                raise HTTPException(
                    status_code=409,
                    detail=f"a newer job ({newer.id}, {newer.status}) exists for case "
                    f"{row.case_id}; replaying old frozen evidence is retrograde — submit a "
                    "fresh recalculate.requested event instead",
                )
            running = session.execute(
                text("SELECT id FROM jobs WHERE case_id=:c AND status='running' LIMIT 1"),
                {"c": row.case_id},
            ).first()
            if running is not None:
                raise HTTPException(
                    status_code=409,
                    detail=f"job {running.id} for case {row.case_id} is currently running; "
                    "per-case serialization forbids resurrecting a sibling beside it",
                )
        # THE row authority: one CAS. Only the winner mutates the run and writes audit evidence;
        # a racing recovery/claim makes this match zero rows and refuses with a stable 409.
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
        reset = session.execute(
            text(
                "UPDATE runs SET state='QUEUED', error=NULL, finished_at=NULL "
                "WHERE id=:run_id AND case_id=:case_id AND state='FAILED' RETURNING id"
            ),
            {"run_id": run_id, "case_id": won.case_id},
        ).first()
        if reset is None:
            # raising rolls the WHOLE recovery back (job stays dead): a run that is missing,
            # COMPLETE, or under another case must not be lied about or clobbered.
            raise HTTPException(
                status_code=409,
                detail=f"run {run_id} is not a FAILED run of case {won.case_id}; recovery "
                "refused — nothing changed (investigate the job/run pairing first)",
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
