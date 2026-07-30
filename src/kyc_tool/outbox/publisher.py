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
import re
import socket
import time
import uuid
from dataclasses import dataclass
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
from kyc_tool.outbox.fence import take_shared_fence

log = structlog.get_logger(__name__)

DECISION_CALLBACK = "decision_callback"
POC_EMAIL = "poc_email"

# How the callback body is encoded on the wire. 7b-core never puts decision_sequence on the wire,
# so every attempt it records is 'legacy'; 7b-activation introduces 'sequenced'. The vocabulary is
# pinned by ck_attempt_wire_vocab, so an unknown value fails at the database rather than silently
# becoming an uninterpretable witness.
_WIRE_VERSION = "legacy"


class _StaleClaim(Exception):
    """Raised BEFORE any network traffic when the claim is no longer live.

    Not a delivery failure: nothing was sent, so the caller must not record an attempt against a
    row another claimant now owns.
    """


@dataclass(frozen=True)
class DeliveryReceipt:
    """Proof-of-staging a decision-callback terminal must present (re-audit `4dfdf8a` F1).

    The requirement is derived from the ROW'S KIND, never from whether a caller happened to pass
    a digest: `_record_delivered(row, token)` with no receipt used to mark a decision callback
    delivered — run COMPLETE, published_at stamped — while the witness taxonomy simultaneously
    classified the same row `not_accepted`. A terminal for a decision callback now carries the
    attempt identity it completes, or it writes nothing.

    `attempt_id` is VERIFIED, not persisted: the terminal's fenced EXISTS matches it against the
    staged attempt (same id, claim, digest, encoding), so a receipt naming a different attempt is
    a no-op — but no winning-attempt link column is stored, because identical bytes are one event
    to the receiver and a stored winner would be a derived fact free to contradict its source.
    The DB trigger independently enforces claim+digest+encoding agreement; the id check is the
    application layer's contribution on top of it.
    """

    attempt_id: str
    wire_sha256: str
    wire_version: str

    def malformed(self) -> str | None:
        """Reason this receipt cannot witness a delivery, or None if well-formed."""
        if not re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            (self.attempt_id or "").lower(),
        ):
            return f"attempt_id is not a UUID: {self.attempt_id!r}"
        if not re.fullmatch(r"[0-9a-f]{64}", self.wire_sha256 or ""):
            return f"wire_sha256 is not 64-hex: {self.wire_sha256!r}"
        if self.wire_version not in ("legacy", "sequenced"):
            return f"unknown wire_version: {self.wire_version!r}"
        return None

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
            # created under the record-intent-first regime (014): with this stamp, "no attempt
            # row" is PROOF nothing was transmitted. The DB default is 'legacy' precisely so a
            # path that forgets this degrades to over-caution, never to false proof.
            witness_generation="attempt_v1",
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
        self.http = http_client or httpx.Client(
            timeout=settings.outbox_http_timeout_seconds)
        self.email_sender = email_sender or LoggingEmailSender()
        self._claimant = f"{socket.gethostname()}:{os.getpid()}"

    # -- delivery -----------------------------------------------------------

    def _build_callback_request(self, payload: dict) -> tuple[httpx.Request, str]:
        """Build the signed callback request and the digest of the bytes it will put on the wire.

        Returns `(request, wire_sha256)`. Split from the send so the digest is taken from
        `request.content` — the bytes httpx will actually transmit — rather than from a
        separately-encoded copy that merely happens to be equal today. Re-deriving the digest
        later from `payload_json` would be wrong outright: jsonb normalizes key order, so a
        stored payload cannot reproduce the sent bytes.
        """
        body = json.dumps(payload).encode()
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
        # Read the wire bytes back off the request rather than reusing `body`: this makes the
        # digest structurally the thing that is sent, so a future change to how the body reaches
        # httpx cannot silently decouple the witness from the wire.
        wire = request.content
        wire_sha256 = hashlib.sha256(wire).hexdigest()

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
            body=wire,
        )
        if not self._outbound_v1_sunset_passed():
            request.headers["X-KYC-Signature"] = security.sign(
                self.settings.platform_hmac_secret, timestamp, wire
            )
        return request, wire_sha256

    def _deliver_decision_callback(
        self, payload: dict, *, outbox_id: int, token: str
    ) -> DeliveryReceipt:
        """Record the attempt, then POST it. Returns the receipt binding terminal to attempt.

        Raises `_StaleClaim` — before any network traffic — if the claim is no longer live.
        """
        request, wire_sha256 = self._build_callback_request(payload)
        # COMMIT the attempt BEFORE the send. Everything about this ordering is the point: if the
        # attempt were recorded after, or in the same transaction as the terminal, then the
        # publisher's own proven residual (2xx received, terminal transaction faults, row stays
        # pending) would leave the platform holding bytes the tool has no record of. The converse
        # gap is accepted and named: die between this commit and the send and the attempt row
        # records staged INTENT for bytes that never left — which is why the witness state it
        # yields is send_intent_witnessed, not proof of transmission.
        attempt_id = self._record_attempt(
            outbox_id=outbox_id, token=token, wire_version=_WIRE_VERSION,
            request_sha256=wire_sha256,
        )
        self._send_for_status(request)
        return DeliveryReceipt(
            attempt_id=attempt_id, wire_sha256=wire_sha256, wire_version=_WIRE_VERSION
        )

    def _send_for_status(self, request: httpx.Request) -> None:
        """Send a callback request and treat the response status as the acknowledgement.

        `httpx.Timeout(0.2)` means each pool/connect/write/read wait may take up to 0.2s; it is not
        a whole-request wall-clock deadline. The publisher therefore does not consume the response
        body at all: for this callback contract, a 2xx status is the local delivery witness, and a
        slow-dripping body must not keep the claim lease occupied after the receiver has already
        accepted the request.
        """
        response = self.http.send(request, stream=True)
        try:
            response.raise_for_status()
        finally:
            response.close()

    def _record_attempt(self, *, outbox_id: int, token: str, wire_version: str,
                        request_sha256: str) -> str:
        """Commit one immutable attempt row under the live claim. Returns its `attempt_id`.

        This records staged INTENT — the exact bytes, durably, before any transmission is
        tried — not transmission itself: the process can die between this commit and the send.

        Fenced on the same `(status, claim_token, live lease)` predicate as every terminal, and
        it runs BEFORE the send: a claimant whose lease expired stages nothing and therefore
        transmits nothing. Raises `_StaleClaim` in that case.

        Precisely: expiry makes the row reclaimable and stops a stale claimant from staging or
        sending anything NEW. It cannot revoke a request already on the wire — no lease can, and
        the residual duplicate that follows from it is the documented at-least-once property (A6),
        which the platform dedupes on `(case_id, run_id)`.
        """
        attempt_id = str(uuid.uuid4())
        with uow(self.session_factory) as session:
            take_shared_fence(session)
            applied = session.execute(
                text(
                    "INSERT INTO outbox_delivery_attempts "
                    "(attempt_id, outbox_id, claim_token, wire_version, request_sha256) "
                    "SELECT :attempt_id, :outbox_id, :token, :wire_version, :sha "
                    "WHERE EXISTS (SELECT 1 FROM outbox WHERE id=:outbox_id "
                    "AND status='pending' AND claim_token=:token "
                    # an EXPIRED lease is reclaimable and must not stage evidence — wall clock,
                    # not transaction-start now(), so waiting past the deadline cannot win
                    # (re-audit 15d875d F4; the DB admission trigger enforces the same rule).
                    "AND claim_lease_expires_at > clock_timestamp()) "
                    "RETURNING attempt_id"
                ),
                {"attempt_id": attempt_id, "outbox_id": outbox_id, "token": token,
                 "wire_version": wire_version, "sha": request_sha256},
            ).first()
        if applied is None:
            raise _StaleClaim(outbox_id)
        return attempt_id

    def _outbound_v1_sunset_passed(self) -> bool:
        try:
            dt = parse_sunset(self.settings.hmac_v1_outbound_sunset_at)
        except ValueError:
            # Malformed dates fail the production kill switch at boot; never
            # crash delivery — treat an unparseable date as "not passed".
            return False
        return dt is not None and datetime.now(UTC) >= dt

    def _deliver(
        self, kind: str, payload: dict, *, outbox_id: int, token: str
    ) -> DeliveryReceipt | None:
        """Returns the delivery receipt for a decision callback; None for a POC email.

        POC emails get no attempt row: they carry no wire digest, nothing reconciles them against
        a remote ledger, and their duplicate-on-retry behaviour is the documented at-least-once
        property (A6) rather than a gap in evidence. They DO get the same presend ownership check
        (re-audit `cbb783b` F5) — evidence and ownership are different questions, and a stale
        claimant emailing a token it cached before losing the row is a real side effect.
        """
        if kind == DECISION_CALLBACK:
            return self._deliver_decision_callback(payload, outbox_id=outbox_id, token=token)
        if kind == POC_EMAIL:
            self._assert_claim_live(outbox_id=outbox_id, token=token)
            self.email_sender.send(payload["to"], payload["subject"], payload["body"])
            return None
        raise ValueError(f"unknown outbox kind: {kind}")

    def _assert_claim_live(self, *, outbox_id: int, token: str) -> None:
        """Refuse to produce an external side effect for a row this publisher no longer owns.

        The decision-callback path gets this for free: `_record_attempt`'s fenced INSERT is its
        presend check. A POC email stages no evidence, so it needs the ownership question asked
        directly — same predicate, same wall clock. Raises `_StaleClaim` BEFORE the provider call.

        This makes the row unsendable by a stale claimant; it does NOT revoke a request already on
        the wire. No lease can: once bytes leave the socket the side effect exists whatever the
        clock says. That residual is the documented at-least-once property (A6), and the real mail
        provider must key its idempotency on `outbox.id` so a duplicate is collapsed receiver-side.
        """
        with uow(self.session_factory) as session:
            take_shared_fence(session)
            live = session.execute(
                text(
                    "SELECT 1 FROM outbox WHERE id=:outbox_id AND status='pending' "
                    "AND claim_token=:token AND claim_lease_expires_at > clock_timestamp()"
                ),
                {"outbox_id": outbox_id, "token": token},
            ).first()
        if live is None:
            raise _StaleClaim(outbox_id)

    # -- loop ---------------------------------------------------------------

    def process_once(self) -> bool:
        """Claim and deliver one pending row of one (case, stream). Returns False when idle."""
        # The lease is its own setting (ge=1), never derived from the backoff schedule: with
        # admission fenced on an UNEXPIRED lease, a zero-backoff config would otherwise mint
        # already-expired claims that can never stage or deliver anything.
        lease = self.settings.outbox_lease_seconds
        with uow(self.session_factory) as session:
            row = session.execute(
                _CLAIM_SQL, {"lease_seconds": lease, "claimed_by": self._claimant}
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
            receipt = self._deliver(row.kind, row.payload_json, outbox_id=row.id, token=token)
        except _StaleClaim:
            # The lease expired and a reclaimer owns the row. Nothing was sent, so this is not a
            # delivery failure: recording one would burn an attempt and push back the backoff of
            # a row this publisher no longer owns.
            log.warning("outbox_stale_claim_presend", outbox_id=row.id)
            return True
        except Exception as exc:  # noqa: BLE001 — a failed delivery must never kill the loop
            self._record_failure(row, str(exc), token)
            return True

        self._record_delivered(row, token, receipt)
        return True

    def _record_delivered(self, row, token, receipt: DeliveryReceipt | None = None) -> None:
        # The requirement is the ROW'S, not the caller's (re-audit 4dfdf8a F1): a decision
        # callback without a well-formed receipt writes NOTHING — not the terminal, not the run,
        # not published_at. The row stays pending/claimed; the lease expires; a real delivery
        # retries. A POC email is the only kind that completes without a wire receipt, and a
        # receipt handed to one is the same caller bug in the other direction.
        if row.kind == DECISION_CALLBACK:
            reason = "missing receipt" if receipt is None else receipt.malformed()
            if reason is not None:
                log.error("outbox_terminal_rejected_unwitnessed",
                          outbox_id=row.id, kind=row.kind, reason=reason)
                return
        elif receipt is not None:
            log.error("outbox_terminal_rejected_unexpected_receipt",
                      outbox_id=row.id, kind=row.kind)
            return
        wire_sha256 = receipt.wire_sha256 if receipt else None
        now = datetime.now(UTC)
        with uow(self.session_factory) as session:
            take_shared_fence(session)
            # The wire witness lands in the SAME fenced statement as the terminal: the digest is
            # only meaningful for the delivery it describes, so it must not be writable by a
            # stale claimant whose UPDATE no longer matches. 7b-core never puts decision_sequence
            # on the wire, so the encoding is always 'legacy' here; 014 introduces 'sequenced'.
            redacted_payload = json.dumps({"redacted": True})
            applied = session.execute(
                text(
                    "UPDATE outbox SET status='delivered', delivered_at=:now, "
                    "claim_token=NULL, claim_lease_expires_at=NULL, claimed_by=NULL, "
                    "callback_wire_sha256=:sha, wire_version=:wire_version, "
                    "payload_json = CASE WHEN :redact_payload THEN CAST(:redacted AS jsonb) "
                    "ELSE payload_json END "
                    "WHERE id=:id AND status='pending' AND claim_token=:token "
                    "AND claim_lease_expires_at > clock_timestamp() "
                    # A terminal digest asserts "these exact bytes were staged and accepted", so
                    # it may only land when the matching attempt row — same claim, same digest,
                    # same encoding — actually exists (re-audit 1f8412e F6). Without this, a
                    # digest could be stamped for bytes no attempt ever recorded, and the
                    # attempt-vs-terminal agreement the taxonomy rests on would be unverifiable.
                    "AND (:needs_witness = false OR EXISTS ("
                    "  SELECT 1 FROM outbox_delivery_attempts a WHERE a.outbox_id=:id "
                    "  AND a.attempt_id=:attempt_id_probe "
                    "  AND a.claim_token=:token AND a.request_sha256=:sha_probe "
                    "  AND a.wire_version=:wire_version_probe "
                    # only an ADMITTED attempt underwrites a terminal — a pre-authority row's
                    # staging claim is unproven (re-audit 15d875d F1)
                    "  AND a.admission='admission_v1')) RETURNING id"
                ),
                # wire_version is decided in Python, not by a SQL CASE over :sha — reusing one
                # parameter as both a value and a NULL test leaves its type indeterminate to
                # psycopg, which rejects the statement outright. The *_probe copies exist for
                # the same reason: one parameter, one role.
                {"id": row.id, "now": now, "token": token, "sha": wire_sha256,
                 "wire_version": receipt.wire_version if receipt else None,
                 "redact_payload": row.kind == POC_EMAIL, "redacted": redacted_payload,
                 "needs_witness": receipt is not None,
                 "attempt_id_probe": receipt.attempt_id if receipt else None,
                 "sha_probe": wire_sha256,
                 "wire_version_probe": receipt.wire_version if receipt else None},
            ).first()
            if applied is None:
                # Two causes, both fail-closed no-ops: a stale loser (lease expired, a reclaimer
                # already finished the row), or — for a decision callback — no attempt row
                # matching this claim+digest, meaning this terminal cannot prove the bytes it
                # claims were ever staged. Either way this claimant writes nothing.
                log.warning("outbox_stale_or_unwitnessed_completion",
                            outbox_id=row.id, attempted="delivered")
                return
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
                redacted_payload = json.dumps({"redacted": True})
                applied = session.execute(
                    text(
                        "UPDATE outbox SET status='dead', attempts=:a, last_error=:e, "
                        "claim_token=NULL, claim_lease_expires_at=NULL, claimed_by=NULL, "
                        "payload_json = CASE WHEN :redact_payload THEN CAST(:redacted AS jsonb) "
                        "ELSE payload_json END "
                        "WHERE id=:id AND status='pending' AND claim_token=:token "
                        "AND claim_lease_expires_at > clock_timestamp() RETURNING id"
                    ),
                    {
                        "id": row.id,
                        "a": attempts,
                        "e": error[:2000],
                        "token": token,
                        "redact_payload": row.kind == POC_EMAIL,
                        "redacted": redacted_payload,
                    },
                ).first()
                if applied is None:
                    log.warning("outbox_stale_claim_completion", outbox_id=row.id, attempted="dead")
                    return
            else:
                applied = session.execute(
                    text(
                        "UPDATE outbox SET attempts=:a, last_error=:e, "
                        "next_attempt_at = now() + make_interval(secs => :delay), "
                        "claim_token=NULL, claim_lease_expires_at=NULL, claimed_by=NULL "
                        "WHERE id=:id AND status='pending' AND claim_token=:token "
                        "AND claim_lease_expires_at > clock_timestamp() RETURNING id"
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
                    "WHERE id=:id AND status='pending' AND claim_token=:token "
                    "AND claim_lease_expires_at > clock_timestamp() RETURNING id"
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
