"""Generic worker loop over the Postgres job queue.

Handlers own their transactions (and call jobs.complete() inside their final
commit). The loop only wraps claim/fail/reap in small transactions of its own.
`run_until_idle()` lets tests drive the pipeline synchronously.
"""

import time
import uuid

import structlog
from sqlalchemy.orm import Session, sessionmaker

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

        # Defensive: a handler that returned without completing its job.
        with uow(self.session_factory) as session:
            jobs.complete(session, claimed.id)
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
