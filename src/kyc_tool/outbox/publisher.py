"""Transactional-outbox delivery: decision callbacks and POC token emails.

Rows are written inside the decide transaction; this publisher delivers them
at-least-once with exponential backoff (AUDIT:A6 — the platform dedupes on
(case_id, run_id); we never claim exactly-once). Claiming pushes
next_attempt_at forward as a lease, so a crash mid-delivery just retries.

PR 7b-core: rows are claimed per (case_id, ordering_stream) under a fenced claim_token;
a decision callback proven obsolete by a higher locally-stamped delivery is terminally
`superseded` (zero sends; decision callbacks only — DB-enforced) — a best-effort LOCAL
suppression, not exactly-once and not platform-authoritative. Three residual reverts
remain for 7b-activation: (1) send-before-stamp; (2) cross-replica; (3) a queued
automatic callback may be delivered AFTER a later manual approval — manual rows have
run_id NULL, no callback, and no sequence, so the guard sees no higher locally-
published automatic sequence. All three are expected pre-activation.
"""

import hashlib
import json
import os
import socket
import time
from datetime import UTC, datetime

import httpx
import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from kyc_tool import security
from kyc_tool.config import Settings, parse_sunset
from kyc_tool.db.audit import audit
from kyc_tool.db.session import uow
from kyc_tool.db.tables import Outbox
from kyc_tool.outbox.emails import EmailSender, LoggingEmailSender

log = structlog.get_logger(__name__)

DECISION_CALLBACK = "decision_callback"
POC_EMAIL = "poc_email"

# Claim the min-id pending row of ONE (case_id, ordering_stream) FIFO stream, fencing it
# with a fresh claim_token + lease. next_attempt_at is left as the retry due time (the
# lease is separate). case_id is NOT NULL (013), so there is no case_id IS NULL bypass.
_CLAIM_SQL = text(
    """
    UPDATE outbox
    SET claim_token = gen_random_uuid(),
        claim_lease_expires_at = now() + make_interval(secs => :lease_seconds),
        claimed_by = :claimed_by
    WHERE id = (
        SELECT o.id FROM outbox o
        WHERE o.status = 'pending' AND o.next_attempt_at <= now()
          AND (o.claim_token IS NULL OR o.claim_lease_expires_at <= now())
          AND o.id = (
                SELECT min(o2.id) FROM outbox o2
                WHERE o2.case_id = o.case_id
                  AND o2.ordering_stream = o.ordering_stream
                  AND o2.status = 'pending'
            )
        ORDER BY o.id
        FOR UPDATE OF o SKIP LOCKED
        LIMIT 1
    )
    RETURNING id, kind, case_id, run_id, payload_json, attempts, ordering_stream,
              decision_sequence, claim_token
    """
)


def enqueue_decision_callback(
    session: Session, *, case_id: str, run_id: str, body: dict, decision_sequence: int
) -> None:
    session.add(
        Outbox(
            kind=DECISION_CALLBACK,
            case_id=case_id,
            run_id=run_id,
            payload_json=body,
            ordering_stream="decision",
            decision_sequence=decision_sequence,
        )
    )


def enqueue_poc_email(session: Session, *, case_id: str, to: str, subject: str, body: str) -> None:
    session.add(
        Outbox(
            kind=POC_EMAIL,
            case_id=case_id,
            payload_json={"to": to, "subject": subject, "body": body},
            ordering_stream="email",
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
        self._claimant = f"{socket.gethostname()}:{os.getpid()}"

    # -- delivery -----------------------------------------------------------

    def _deliver_decision_callback(self, payload: dict) -> str:
        """POST the callback. Returns the SHA-256 of the bytes actually sent.

        The digest is computed HERE, from the exact `body` handed to httpx, because this is the
        only place those bytes exist. Re-deriving it later from `payload_json` would be wrong:
        jsonb normalizes key order, so a stored payload cannot reproduce the sent bytes.
        """
        body = json.dumps(payload).encode()
        wire_sha256 = hashlib.sha256(body).hexdigest()
        timestamp = str(time.time())
        url = f"{self.settings.platform_callback_url.rstrip('/')}/kyc/decision"

        # Build the request FIRST, then sign the literal target httpx will put on
        # the wire. httpx percent-encodes non-ASCII and strips dot-segments when
        # it constructs the URL, so a base like ".../café" or ".../a/../hooks" is
        # normalized before it is sent. Signing request.url.raw_path binds the v2
        # signature to exactly what a conforming receiver verifies (finding 1) —
        # a pre-normalized string would disagree with the wire. (config validation
        # rejects query/fragment callback bases, which would misdirect the POST.)
        request = self.http.build_request(
            "POST",
            url,
            content=body,
            headers={"Content-Type": "application/json", "X-KYC-Timestamp": timestamp},
        )
        path_qs = request.url.raw_path.decode("ascii")

        # v2 (path-bound) is always emitted; v1 is dual-emitted until the OUTBOUND
        # sunset so the platform can migrate its receiver on its own schedule
        # (PR 5a §3). The two sunset dates are independent.
        request.headers["X-KYC-Key-Id"] = self.settings.hmac_outbound_key_id
        request.headers["X-KYC-Signature-V2"] = security.sign_v2(
            self.settings.hmac_outbound_secret,
            key_id=self.settings.hmac_outbound_key_id,
            direction=security.DIRECTION_OUTBOUND,
            method="POST",
            path_qs=path_qs,
            timestamp=timestamp,
            slot="",
            body=body,
        )
        if not self._outbound_v1_sunset_passed():
            request.headers["X-KYC-Signature"] = security.sign(
                self.settings.platform_hmac_secret, timestamp, body
            )

        response = self.http.send(request)
        response.raise_for_status()
        return wire_sha256

    def _outbound_v1_sunset_passed(self) -> bool:
        try:
            dt = parse_sunset(self.settings.hmac_v1_outbound_sunset_at)
        except ValueError:
            # Malformed dates fail the production kill switch at boot; never
            # crash delivery — treat an unparseable date as "not passed".
            return False
        return dt is not None and datetime.now(UTC) >= dt

    def _deliver(self, kind: str, payload: dict) -> str | None:
        """Returns the sent-bytes digest for a decision callback; None for a POC email."""
        if kind == DECISION_CALLBACK:
            return self._deliver_decision_callback(payload)
        if kind == POC_EMAIL:
            self.email_sender.send(payload["to"], payload["subject"], payload["body"])
            return None
        raise ValueError(f"unknown outbox kind: {kind}")

    # -- loop ---------------------------------------------------------------

    def process_once(self) -> bool:
        """Claim and deliver one pending row of one (case, stream). Returns False when idle."""
        lease = self.settings.outbox_backoff_base_seconds * (2 ** (self.settings.outbox_max_attempts - 1))
        with uow(self.session_factory) as session:
            row = session.execute(
                _CLAIM_SQL, {"lease_seconds": min(lease, 3600), "claimed_by": self._claimant}
            ).first()
        if row is None:
            return False
        token = row.claim_token

        # Best-effort local superseded guard (NOT an authority): suppress this older
        # decision-stream callback only when a HIGHER-sequence decision for the case already
        # has a locally-stamped published_at. If the higher callback was sent but its
        # _record_delivered had not committed (send-before-stamp), or a concurrent replica
        # sent it, or the case's current state is a MANUAL approval (run_id NULL, no
        # callback, no sequence — nothing here compares higher), this predicate is false
        # and the older callback IS sent — reverts that remain expected until
        # 7b-activation's platform high-water (§5).
        if row.ordering_stream == "decision" and row.decision_sequence is not None:
            with uow(self.session_factory) as session:
                superseded = session.execute(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM decisions WHERE case_id=:c "
                        "AND decision_sequence > :seq AND published_at IS NOT NULL)"
                    ),
                    {"c": row.case_id, "seq": row.decision_sequence},
                ).scalar_one()
            if superseded:
                self._record_superseded(row, token)
                return True

        try:
            wire_sha256 = self._deliver(row.kind, row.payload_json)
        except Exception as exc:  # noqa: BLE001 — a failed delivery must never kill the loop
            self._record_failure(row, str(exc), token)
            return True

        self._record_delivered(row, token, wire_sha256)
        return True

    def _record_delivered(self, row, token, wire_sha256: str | None = None) -> None:
        now = datetime.now(UTC)
        with uow(self.session_factory) as session:
            # The wire witness lands in the SAME fenced statement as the terminal: the digest is
            # only meaningful for the delivery it describes, so it must not be writable by a
            # stale claimant whose UPDATE no longer matches. 7b-core never puts decision_sequence
            # on the wire, so the encoding is always 'legacy' here; 014 introduces 'sequenced'.
            applied = session.execute(
                text(
                    "UPDATE outbox SET status='delivered', delivered_at=:now, "
                    "claim_token=NULL, claim_lease_expires_at=NULL, claimed_by=NULL, "
                    "callback_wire_sha256=:sha, wire_version=:wire_version "
                    "WHERE id=:id AND status='pending' AND claim_token=:token RETURNING id"
                ),
                # wire_version is decided in Python, not by a SQL CASE over :sha — reusing one
                # parameter as both a value and a NULL test leaves its type indeterminate to
                # psycopg, which rejects the statement outright.
                {"id": row.id, "now": now, "token": token, "sha": wire_sha256,
                 "wire_version": None if wire_sha256 is None else "legacy"},
            ).first()
            if applied is None:
                # stale loser: its lease expired and a reclaimer already finished this row.
                log.warning("outbox_stale_claim_completion", outbox_id=row.id, attempted="delivered")
                return
            if row.kind == POC_EMAIL:
                # the raw POC token existed only to be emailed; don't retain it at rest.
                session.execute(
                    text("UPDATE outbox SET payload_json = CAST(:p AS jsonb) WHERE id=:id"),
                    {"id": row.id, "p": json.dumps({"redacted": True})},
                )
            if row.kind == DECISION_CALLBACK and row.run_id:
                session.execute(
                    text(
                        "UPDATE runs SET state='COMPLETE', finished_at=:now "
                        "WHERE id=:run_id AND state='PUBLISH_DECISION'"
                    ),
                    {"run_id": row.run_id, "now": now},
                )
                session.execute(
                    text(
                        "UPDATE decisions SET published_at=:now "
                        "WHERE run_id=:run_id AND published_at IS NULL"
                    ),
                    {"run_id": row.run_id, "now": now},
                )
        log.info("outbox_delivered", outbox_id=row.id, kind=row.kind, case_id=row.case_id)

    def _record_failure(self, row, error: str, token) -> None:
        attempts = row.attempts + 1
        dead = attempts >= self.settings.outbox_max_attempts
        delay = self.settings.outbox_backoff_base_seconds * (2 ** (attempts - 1))
        with uow(self.session_factory) as session:
            if dead:
                applied = session.execute(
                    text(
                        "UPDATE outbox SET status='dead', attempts=:a, last_error=:e, "
                        "claim_token=NULL, claim_lease_expires_at=NULL, claimed_by=NULL "
                        "WHERE id=:id AND status='pending' AND claim_token=:token RETURNING id"
                    ),
                    {"id": row.id, "a": attempts, "e": error[:2000], "token": token},
                ).first()
                if applied is None:
                    log.warning("outbox_stale_claim_completion", outbox_id=row.id, attempted="dead")
                    return
                if row.kind == POC_EMAIL:
                    session.execute(
                        text("UPDATE outbox SET payload_json = CAST(:p AS jsonb) WHERE id=:id"),
                        {"id": row.id, "p": json.dumps({"redacted": True})},
                    )
            else:
                applied = session.execute(
                    text(
                        "UPDATE outbox SET attempts=:a, last_error=:e, "
                        "next_attempt_at = now() + make_interval(secs => :delay), "
                        "claim_token=NULL, claim_lease_expires_at=NULL, claimed_by=NULL "
                        "WHERE id=:id AND status='pending' AND claim_token=:token RETURNING id"
                    ),
                    {"id": row.id, "a": attempts, "e": error[:2000], "delay": delay, "token": token},
                ).first()
                if applied is None:
                    log.warning("outbox_stale_claim_completion", outbox_id=row.id, attempted="retry")
                    return
        if dead:
            log.error("outbox_dead_letter", outbox_id=row.id, kind=row.kind, error=error)
        else:
            log.warning("outbox_retry", outbox_id=row.id, kind=row.kind, attempts=attempts)

    def _record_superseded(self, row, token) -> None:
        """A higher locally-stamped delivery proved this callback obsolete: terminally
        suppress it (zero sends), fenced like every other terminal. published_at stays NULL
        (never sent); the run still reaches COMPLETE via the legal PUBLISH_DECISION edge. The
        durable audit records BOTH the superseded and the superseding sequence (A6 evidence)."""
        now = datetime.now(UTC)
        with uow(self.session_factory) as session:
            applied = session.execute(
                text(
                    "UPDATE outbox SET status='superseded', resolved_at=:now, "
                    "claim_token=NULL, claim_lease_expires_at=NULL, claimed_by=NULL "
                    "WHERE id=:id AND status='pending' AND claim_token=:token RETURNING id"
                ),
                {"id": row.id, "now": now, "token": token},
            ).first()
            if applied is None:
                log.warning("outbox_stale_claim_completion", outbox_id=row.id, attempted="superseded")
                return
            superseding = session.execute(
                text(
                    "SELECT min(decision_sequence) FROM decisions WHERE case_id=:c "
                    "AND decision_sequence > :seq AND published_at IS NOT NULL"
                ),
                {"c": row.case_id, "seq": row.decision_sequence},
            ).scalar()
            if row.run_id:
                session.execute(
                    text(
                        "UPDATE runs SET state='COMPLETE', finished_at=:now "
                        "WHERE id=:run_id AND state='PUBLISH_DECISION'"
                    ),
                    {"run_id": row.run_id, "now": now},
                )
            audit(
                session, "outbox.superseded", case_id=row.case_id,
                outbox_id=row.id, superseded_sequence=row.decision_sequence,
                superseding_sequence=superseding,
            )
        log.info(
            "outbox_superseded", outbox_id=row.id, case_id=row.case_id,
            superseded_sequence=row.decision_sequence, superseding_sequence=superseding,
        )

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
