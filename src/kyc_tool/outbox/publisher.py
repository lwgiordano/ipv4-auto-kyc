"""Transactional-outbox delivery: decision callbacks and POC token emails.

Rows are written inside the decide transaction; this publisher delivers them
at-least-once with exponential backoff (AUDIT:A6 — the platform dedupes on
(case_id, run_id); we never claim exactly-once). Claiming pushes
next_attempt_at forward as a lease, so a crash mid-delivery just retries.
"""

import json
import time
from datetime import UTC, datetime

import httpx
import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from kyc_tool import security
from kyc_tool.config import Settings
from kyc_tool.db.session import uow
from kyc_tool.db.tables import Outbox
from kyc_tool.outbox.emails import EmailSender, LoggingEmailSender

log = structlog.get_logger(__name__)

DECISION_CALLBACK = "decision_callback"
POC_EMAIL = "poc_email"

_CLAIM_SQL = text(
    """
    UPDATE outbox
    SET next_attempt_at = now() + make_interval(secs => :lease_seconds)
    WHERE id = (
        SELECT o.id FROM outbox o
        WHERE o.status = 'pending' AND o.next_attempt_at <= now()
          AND (
                o.case_id IS NULL
                OR o.id = (
                    SELECT min(o2.id) FROM outbox o2
                    WHERE o2.case_id = o.case_id AND o2.status = 'pending'
                )
              )
        ORDER BY o.id
        FOR UPDATE OF o SKIP LOCKED
        LIMIT 1
    )
    RETURNING id, kind, case_id, run_id, payload_json, attempts
    """
)


def enqueue_decision_callback(session: Session, *, case_id: str, run_id: str, body: dict) -> None:
    session.add(
        Outbox(kind=DECISION_CALLBACK, case_id=case_id, run_id=run_id, payload_json=body)
    )


def enqueue_poc_email(
    session: Session, *, case_id: str, to: str, subject: str, body: str
) -> None:
    session.add(
        Outbox(
            kind=POC_EMAIL,
            case_id=case_id,
            payload_json={"to": to, "subject": subject, "body": body},
        )
    )


class OutboxPublisher:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        *,
        http_client: httpx.Client | None = None,
        email_sender: EmailSender | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.http = http_client or httpx.Client(timeout=10.0)
        self.email_sender = email_sender or LoggingEmailSender()

    # -- delivery -----------------------------------------------------------

    def _deliver_decision_callback(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        timestamp = str(time.time())
        headers = {
            "Content-Type": "application/json",
            "X-KYC-Timestamp": timestamp,
            "X-KYC-Signature": security.sign(
                self.settings.platform_hmac_secret, timestamp, body
            ),
        }
        url = f"{self.settings.platform_callback_url.rstrip('/')}/kyc/decision"
        response = self.http.post(url, content=body, headers=headers)
        response.raise_for_status()

    def _deliver(self, kind: str, payload: dict) -> None:
        if kind == DECISION_CALLBACK:
            self._deliver_decision_callback(payload)
        elif kind == POC_EMAIL:
            self.email_sender.send(payload["to"], payload["subject"], payload["body"])
        else:
            raise ValueError(f"unknown outbox kind: {kind}")

    # -- loop ---------------------------------------------------------------

    def process_once(self) -> bool:
        """Claim and deliver one pending row. Returns False when queue is idle."""
        lease = self.settings.outbox_backoff_base_seconds * (
            2 ** (self.settings.outbox_max_attempts - 1)
        )
        with uow(self.session_factory) as session:
            row = session.execute(
                _CLAIM_SQL, {"lease_seconds": min(lease, 3600)}
            ).first()
        if row is None:
            return False

        try:
            self._deliver(row.kind, row.payload_json)
        except Exception as exc:  # noqa: BLE001 — a failed delivery must never kill the loop
            self._record_failure(row, str(exc))
            return True

        self._record_delivered(row)
        return True

    def _record_delivered(self, row) -> None:
        now = datetime.now(UTC)
        with uow(self.session_factory) as session:
            session.execute(
                text(
                    "UPDATE outbox SET status='delivered', delivered_at=:now WHERE id=:id"
                ),
                {"id": row.id, "now": now},
            )
            if row.kind == POC_EMAIL:
                # the raw POC token existed only to be emailed; don't retain it
                # at rest (delivered rows live for the full retention window).
                session.execute(
                    text("UPDATE outbox SET payload_json = CAST(:p AS jsonb) WHERE id=:id"),
                    {"id": row.id, "p": json.dumps({"redacted": True})},
                )
            if row.kind == DECISION_CALLBACK and row.run_id:
                # callback delivered → run reaches its terminal state
                session.execute(
                    text(
                        """
                        UPDATE runs SET state='COMPLETE', finished_at=:now
                        WHERE id=:run_id AND state='PUBLISH_DECISION'
                        """
                    ),
                    {"run_id": row.run_id, "now": now},
                )
                session.execute(
                    text(
                        "UPDATE decisions SET published_at=:now WHERE run_id=:run_id AND published_at IS NULL"
                    ),
                    {"run_id": row.run_id, "now": now},
                )
        log.info("outbox_delivered", outbox_id=row.id, kind=row.kind, case_id=row.case_id)

    def _record_failure(self, row, error: str) -> None:
        attempts = row.attempts + 1
        dead = attempts >= self.settings.outbox_max_attempts
        delay = self.settings.outbox_backoff_base_seconds * (2 ** (attempts - 1))
        with uow(self.session_factory) as session:
            if dead:
                session.execute(
                    text(
                        "UPDATE outbox SET status='dead', attempts=:a, last_error=:e WHERE id=:id"
                    ),
                    {"id": row.id, "a": attempts, "e": error[:2000]},
                )
                if row.kind == POC_EMAIL:
                    # a dead POC email is never retried — scrub the raw token so
                    # it doesn't sit at rest for the retention window (matches
                    # the redaction _record_delivered does on success)
                    session.execute(
                        text("UPDATE outbox SET payload_json = CAST(:p AS jsonb) WHERE id=:id"),
                        {"id": row.id, "p": json.dumps({"redacted": True})},
                    )
            else:
                session.execute(
                    text(
                        """
                        UPDATE outbox
                        SET attempts=:a, last_error=:e,
                            next_attempt_at = now() + make_interval(secs => :delay)
                        WHERE id=:id
                        """
                    ),
                    {"id": row.id, "a": attempts, "e": error[:2000], "delay": delay},
                )
        if dead:
            log.error("outbox_dead_letter", outbox_id=row.id, kind=row.kind, error=error)
        else:
            log.warning("outbox_retry", outbox_id=row.id, kind=row.kind, attempts=attempts)

    def process_pending(self, max_items: int = 100) -> int:
        done = 0
        while done < max_items and self.process_once():
            done += 1
        return done

    def run_forever(self, poll_seconds: float = 0.5) -> None:
        log.info("outbox_publisher_started")
        while True:
            if not self.process_once():
                time.sleep(poll_seconds)
