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

from kyc_tool.config import require_numeric_domain
from kyc_tool.db.session import uow
from kyc_tool.queue import jobs

log = structlog.get_logger(__name__)

Handler = "callable[[Session | None, jobs.ClaimedJob], None]"


class Worker:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        handlers: dict[str, object],
        *,
        lease_seconds: int = 120,
        backoff_base_seconds: int = 5,
        poll_seconds: float = 0.5,
        worker_id: str | None = None,
        on_dead_letter: object | None = None,
    ) -> None:
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
            with uow(self.session_factory) as session:
                dead = jobs.reap_expired(session)
            # Lease-expiry dead-letters must fail their runs too, not just the
            # handler-exception path below — else a crashed run stays a zombie.
            for job in dead:
                log.error("job_dead_letter", job_id=job.id, kind=job.kind, reason="lease_expired")
                if self.on_dead_letter is not None:
                    self.on_dead_letter(job, "lease expired; attempts exhausted")
            return False

        handler = self.handlers[claimed.kind]
        # PR 7a slice: heartbeat the claim while the handler runs, so a job that legitimately
        # outlives its lease is NOT reaped and double-executed. Cadence lease/3 (floor 1s); stops
        # the moment the fence is lost (reaped + reclaimed) — fenced complete()/fail() then keep
        # this stale worker from committing anything.
        stop_beat = threading.Event()

        def _beat() -> None:
            cadence = max(self.lease_seconds / 3.0, 1.0)
            while not stop_beat.wait(cadence):
                try:
                    with uow(self.session_factory) as s:
                        if not jobs.heartbeat(s, claimed, self.lease_seconds):
                            log.warning("job_heartbeat_lost", job_id=claimed.id)
                            return
                except Exception as exc:  # noqa: BLE001 — a beat failure must not kill the worker
                    log.warning("job_heartbeat_error", job_id=claimed.id, error=str(exc))

        beat_thread = threading.Thread(target=_beat, daemon=True)
        beat_thread.start()
        try:
            handler(claimed)
        except Exception as exc:  # noqa: BLE001 — worker must survive any handler error
            log.error("job_failed", job_id=claimed.id, kind=claimed.kind, error=str(exc))
            with uow(self.session_factory) as session:
                dead = jobs.fail(session, claimed, str(exc), self.backoff_base_seconds)
            if dead:
                log.error("job_dead_letter", job_id=claimed.id, kind=claimed.kind)
                if self.on_dead_letter is not None:
                    self.on_dead_letter(claimed, str(exc))
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
