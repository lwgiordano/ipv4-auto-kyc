"""Generic worker loop over the Postgres job queue.

Handlers own their transactions (and call jobs.complete() inside their final
commit). The loop only wraps claim/fail/reap in small transactions of its own.
`run_until_idle()` lets tests drive the pipeline synchronously.
"""

import threading
import time
import uuid

import structlog
from sqlalchemy.orm import Session, sessionmaker

from kyc_tool.config import (
    CAP_DECISION_WRITE,
    ProcessContext,
    ProcessRole,  # noqa: F401 — re-exported for handler-role tooling
    ProcessRoleCapabilityError,
    require_numeric_domain,
    require_role_capability,
)
from kyc_tool.db.session import uow
from kyc_tool.queue import jobs

log = structlog.get_logger(__name__)

Handler = "callable[[Session | None, jobs.ClaimedJob], None]"

# Registering a handler for a kind is acquiring that kind's write capability (re-audit
# `1826661..b5c7a83` finding 5): `run_transition` jobs decide cases, so the role must carry
# CAP_DECISION_WRITE. The map is CLOSED — a kind it does not classify cannot be registered
# under any role, so a new job kind is a reviewed classification here, never a silent write
# path beside the accounted one.
HANDLER_KIND_CAPABILITIES: dict[str, str] = {"run_transition": CAP_DECISION_WRITE}


def heartbeat_cadence_seconds(lease_seconds: float) -> float:
    """STRICTLY below the ROADMAP's lease/3 ceiling at EVERY accepted lease, never clamped upward
    (re-audit `3db5f13..a7df17b` F4: max(lease/3, 1s) scheduled the first beat AT expiry for a 1-2s
    lease). lease/4 leaves at least two further beats of room before expiry after any single one."""
    return lease_seconds / 4.0


class Worker:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        handlers: dict[str, object],
        *,
        process_role: "ProcessContext",
        settings: object,
        lease_seconds: int = 120,
        backoff_base_seconds: int = 5,
        poll_seconds: float = 0.5,
        worker_id: str | None = None,
        on_dead_letter: object | None = None,
    ) -> None:
        # Capability boundary FIRST (re-audit `1826661..b5c7a83` finding 5): every construction
        # states the ProcessRole it runs under, and registering each handler kind demands that
        # kind's capability from the canonical map — inside the constructor, so aliases,
        # factories, and disposable entry points cannot become unaccounted writers.
        for kind in handlers:
            capability = HANDLER_KIND_CAPABILITIES.get(kind)
            if capability is None:
                raise ProcessRoleCapabilityError(
                    f"handler kind {kind!r} is not classified in HANDLER_KIND_CAPABILITIES; "
                    f"classify its write capability before any role may register it"
                )
            require_role_capability(process_role, capability, f"a {kind!r} handler",
                                    settings=settings)
        self.process_role = process_role.role
        # Process boundary (re-audit `5b0f0b8..b75a320` R4-F3): validate the timing knobs at
        # construction so a nonpositive/non-finite poll cannot kill the idle loop at the first
        # `time.sleep`, and a bad lease/backoff refuses BEFORE the worker starts claiming — not mid-run.
        for name, value in (
            ("worker_poll_seconds", poll_seconds),
            ("job_lease_seconds", lease_seconds),
            ("job_backoff_base_seconds", backoff_base_seconds),
        ):
            require_numeric_domain(name, value)
        self.session_factory = session_factory
        self.handlers = handlers
        self.lease_seconds = lease_seconds
        self.backoff_base_seconds = backoff_base_seconds
        self.poll_seconds = poll_seconds
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self.on_dead_letter = on_dead_letter

    def run_once(self) -> bool:
        """Claim and process one job. Returns False when nothing was runnable."""
        with uow(self.session_factory) as session:
            claimed = jobs.claim(
                session, list(self.handlers), self.worker_id, self.lease_seconds
            )
        if claimed is None:
            # Lease-expiry dead-letters fail their runs IN THE SAME TRANSACTION as the reap
            # (re-audit `3db5f13..a7df17b` F3): a crash between the two must not leave a dead job
            # with a live run, and a recovered run must never be re-failed by a later separate txn.
            with uow(self.session_factory) as session:
                dead = jobs.reap_expired(session)
                for job in dead:
                    log.error("job_dead_letter", job_id=job.id, kind=job.kind, reason="lease_expired")
                    if self.on_dead_letter is not None:
                        self.on_dead_letter(job, "lease expired; attempts exhausted", session=session)
            return False

        handler = self.handlers[claimed.kind]
        # PR 7a slice: heartbeat the claim while the handler runs, so a job that legitimately
        # outlives its lease is NOT reaped and double-executed. Cadence STRICTLY below lease/3 —
        # lease/4, never clamped upward (re-audit `3db5f13..a7df17b` F4: max(lease/3, 1s) scheduled
        # the first beat AT expiry for small accepted leases). A fence miss OR a beat error sets
        # `ctx.lost` (F2): we can no longer PROVE the lease extends, so the claim is revoked at the
        # handler's next boundary/transaction instead of running to completion on side effects.
        ctx = jobs.ClaimContext(job=claimed)
        stop_beat = threading.Event()

        def _beat() -> None:
            cadence = heartbeat_cadence_seconds(self.lease_seconds)
            while not stop_beat.wait(cadence):
                try:
                    with uow(self.session_factory) as s:
                        if not jobs.heartbeat(s, claimed, self.lease_seconds):
                            log.warning("job_heartbeat_lost", job_id=claimed.id)
                            ctx.lost.set()
                            return
                except Exception as exc:  # noqa: BLE001 — a beat failure must not kill the worker
                    log.warning("job_heartbeat_error", job_id=claimed.id, error=str(exc))
                    ctx.lost.set()  # unprovable extension = lost (F4): revoke, never keep working
                    return

        beat_thread = threading.Thread(target=_beat, daemon=True)
        beat_thread.start()
        try:
            with jobs.claim_scope(ctx):
                handler(claimed)
        except Exception as exc:  # noqa: BLE001 — worker must survive any handler error
            log.error("job_failed", job_id=claimed.id, kind=claimed.kind, error=str(exc))
            with uow(self.session_factory) as session:
                outcome = jobs.fail(session, claimed, str(exc), self.backoff_base_seconds)
                if outcome == "dead":
                    # run-FAILED rides in the SAME txn as the job terminal (F3)
                    log.error("job_dead_letter", job_id=claimed.id, kind=claimed.kind)
                    if self.on_dead_letter is not None:
                        self.on_dead_letter(claimed, str(exc), session=session)
            if outcome == "stale":
                log.warning("job_fail_stale_fence", job_id=claimed.id)  # recovered/reclaimed: no-op
            return True

        finally:
            stop_beat.set()
            beat_thread.join(timeout=5)
        # Defensive: a handler that returned without completing its job. Fenced — a stale claim
        # generation completes nothing.
        with uow(self.session_factory) as session:
            jobs.complete(session, claimed)
        return True

    def run_until_idle(self, max_jobs: int = 1000) -> int:
        """Process jobs until the queue drains (test/synchronous driver)."""
        processed = 0
        while processed < max_jobs and self.run_once():
            processed += 1
        return processed

    def run_forever(self) -> None:
        log.info("worker_started", worker_id=self.worker_id, kinds=list(self.handlers))
        while True:
            if not self.run_once():
                time.sleep(self.poll_seconds)
