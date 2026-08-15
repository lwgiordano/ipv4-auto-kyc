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

import contextlib
import hashlib
import json
import os
import queue
import re
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from kyc_tool import security
from kyc_tool.api.schemas import encode_decision_callback
from kyc_tool.config import (
    CAP_CALLBACK_PUBLISH,
    OUTBOX_ATTEMPT_DEADLINE_PHASES,
    ProcessContext,
    Settings,
    parse_sunset,
    require_role_capability,
)
from kyc_tool.db.audit import audit
from kyc_tool.db.session import uow
from kyc_tool.db.tables import Outbox
from kyc_tool.outbox.emails import EmailSender, LoggingEmailSender
from kyc_tool.outbox.fence import take_shared_fence
from kyc_tool.queue.backoff import saturating_backoff_seconds

log = structlog.get_logger(__name__)

DECISION_CALLBACK = "decision_callback"
POC_EMAIL = "poc_email"

# How the callback body is encoded on the wire. 7b-core never puts decision_sequence on the wire,
# so every attempt it records is 'legacy'; 7b-activation introduces 'sequenced'. The vocabulary is
# pinned by ck_attempt_wire_vocab, so an unknown value fails at the database rather than silently
# becoming an uninterpretable witness.
# The active callback wire generation, compared by the pending-024 gate against the pre-024
# LITERAL held in the verifier itself (re-gate-3 finding 4). The previous arrangement compared this
# to a sibling alias in this same module, so the natural two-line edit — move the alias and the
# active value together — passed the gate while every persisted attempt advertised sequenced wire.
# A name declared beside the thing it checks is not an authority. The vocabulary itself is pinned
# independently by the ck_attempt_wire_vocab DB constraint.
_WIRE_VERSION = "legacy"

# Detached (deadline-overrun) send threads tolerated at once. This is a HARD resource cap, not a
# log threshold: at capacity the publisher STOPS CLAIMING (circuit breaker), so a pathologically
# wedged callback endpoint cannot accrete unbounded threads/connections — it stalls delivery
# (health goes red) until an operator intervenes, which is the safe failure.
_MAX_ORPHAN_SENDS = 8
# Total (not per-orphan) seconds close() waits for detached sends to finish — bounded shutdown.
_ORPHAN_DRAIN_SECONDS = 2.0

# Ceiling on a single backoff delay. The schedule is base × 2**(attempts-1); with a large base or
# attempts that grows unbounded, `now() + make_interval(secs => delay)` would overflow PostgreSQL's
# timestamptz (µs since epoch, int64) and raise mid-write, leaving the row pending/claimed/unredacted
# (re-audit `d3c0852..23e005e` F5). 24h is far beyond any real retry cadence and is overflow-safe.
_MAX_BACKOFF_SECONDS = 86_400

# How much of a callback acknowledgement body the publisher will read to keep the connection
# reusable. A platform ack is a few hundred bytes; past this it is not an ack we need, and reading
# further would let the receiver decide how long the tool holds its claim.
_MAX_ACK_BODY_BYTES = 64 * 1024


class _AttemptDeadlineExceeded(Exception):
    """One delivery attempt overran its enforced wall-clock deadline and was cancelled.

    HTTPX's per-operation timeouts reset on I/O activity, so a receiver dribbling bytes under
    the timeout could otherwise hold the (single-threaded) publisher — and its claim — for as
    long as it liked. Routed through the ordinary failure path: accounted, backed off, retried.
    """


class _StaleClaim(Exception):
    """Raised BEFORE any network traffic when the claim is no longer live.

    Not a delivery failure: nothing was sent, so the caller must not record an attempt against a
    row another claimant now owns.
    """


class OutboxSaturated(RuntimeError):
    """The orphan-cap circuit breaker tripped: detached wedged sends have reached
    `_MAX_ORPHAN_SENDS`, so the publisher can stage nothing new. `run_forever` raises this instead
    of sleeping forever like an empty queue, making saturation SUPERVISOR-VISIBLE (re-audit
    `538e55e..42e1c7d` F6): the worker exits nonzero, a supervisor restarts it, and the fresh
    process drops the daemon attempts and reclaims their connections — observable recovery."""


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
#
# The CTE captures the PRE-claim claim_token as `prev_claim_token`: a non-NULL value means we
# reclaimed a row whose prior claim expired WITHOUT being cleared by a terminal — i.e. the
# predecessor admitted an attempt (or crashed mid-cycle) and never terminalized. process_once
# reconciles that instead of sending again, so a crash cannot push real sends past
# outbox_max_attempts (re-audit `42e1c7d..b39b82a` F1). A cleanly-released row (post-terminal or
# never claimed) has prev_claim_token NULL and takes the ordinary admit+send path.
_CLAIM_SQL = text(
    """
    WITH claimed AS (
        SELECT o.id, o.claim_token AS prev_claim_token
        FROM outbox o
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
    UPDATE outbox
    SET claim_token = gen_random_uuid(),
        claim_lease_expires_at = now() + make_interval(secs => :lease_seconds),
        claimed_by = :claimed_by
    FROM claimed
    WHERE outbox.id = claimed.id
    RETURNING outbox.id, outbox.kind, outbox.case_id, outbox.run_id, outbox.payload_json,
              outbox.attempts, outbox.ordering_stream, outbox.decision_sequence,
              outbox.claim_token, claimed.prev_claim_token
    """
)


def enqueue_decision_callback(session: Session, *, body: dict, decision_sequence: int) -> None:
    """Enqueue a decision callback. THIS is the serialization boundary (re-gate finding 3).

    Validating in the pipeline sanitized one caller's dict and left the boundary open: adding
    `decision_sequence` after that call, or calling this function directly, stored the ordering key
    verbatim because the enqueue accepted a raw `dict`. Encoding here means the last thing before
    `Outbox` is a validated body, whatever the caller assembled — and `DecisionCallback` is
    `extra="forbid"`, so an unmodelled field is REFUSED rather than silently dropped in one path
    while leaking through another.

    The row's `case_id`/`run_id` are DERIVED from the validated body (re-gate-3 finding 2). They
    used to be separate keyword arguments, so the outbox could account, order, and complete a row
    under one identity while the wire body named another — and several integration tests were
    doing exactly that without noticing. One authority now: the platform dedupes on the BODY's
    `(case_id, run_id)` (A6), so the body is the identity, and the row follows it.
    `decision_sequence` stays an argument because it is row-only — it is the per-case ordering
    column, deliberately absent from the pre-024 wire.
    """
    payload = encode_decision_callback(body)
    session.add(
        Outbox(
            kind=DECISION_CALLBACK,
            case_id=payload["case_id"],
            run_id=payload["run_id"],
            payload_json=payload,
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
        process_role: "ProcessContext",
        http_client: httpx.Client | None = None,
        email_sender: EmailSender | None = None,
    ) -> None:
        # Constructing a publisher is acquiring the callback-publish capability (re-audit
        # `1826661..b5c7a83` finding 5): the declared role must carry it in the canonical map,
        # checked here so no alias, factory, or disposable entry point publishes unaccounted.
        require_role_capability(process_role, CAP_CALLBACK_PUBLISH, "OutboxPublisher",
                                settings=settings)
        self.process_role = process_role.role
        self.session_factory = session_factory
        self.settings = settings
        self.http = http_client or httpx.Client(timeout=settings.outbox_http_timeout_seconds)
        self.email_sender = email_sender or LoggingEmailSender()
        self._claimant = f"{socket.gethostname()}:{os.getpid()}"
        self._orphans: list[threading.Thread] = []
        self._saturated = False

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
        # COMMIT the attempt BEFORE the send, and in that same commit COUNT it and EXTEND the
        # claim over the whole send + accounting window. Everything about this ordering is the
        # point: if the attempt were recorded after, or in the same transaction as the terminal,
        # then the publisher's own proven residual (2xx received, terminal transaction faults, row
        # stays pending) would leave the platform holding bytes the tool has no record of. The
        # converse gap is accepted and named: die between this commit and the send and the attempt
        # row records staged INTENT for bytes that never left — which is why the witness state it
        # yields is send_intent_witnessed, not proof of transmission. Counting at admission (not at
        # accounting) is what makes the count survive a crash between the two; extending the lease
        # here is what stops a reclaimer from sending a duplicate while this attempt is still being
        # accounted (re-audit `538e55e..42e1c7d` F1).
        attempt_id = self._record_attempt(
            outbox_id=outbox_id, token=token, wire_version=_WIRE_VERSION,
            request_sha256=wire_sha256,
        )
        self._send_for_status(request)
        return DeliveryReceipt(
            attempt_id=attempt_id, wire_sha256=wire_sha256, wire_version=_WIRE_VERSION
        )

    def _send_for_status(self, request: httpx.Request) -> None:
        """One delivery attempt under an ENFORCED wall-clock wait-bound.

        HTTPX's per-operation timeouts reset on I/O activity, so no combination of them bounds
        an attempt: a receiver dripping one header byte per interval holds a bare `send()`
        indefinitely (measured: 32s against a 0.5s timeout), stalling the single-threaded
        publisher and outliving the claim lease. The attempt therefore runs in a DAEMON worker
        thread and the publisher waits at most `OUTBOX_ATTEMPT_DEADLINE_PHASES × timeout` — the
        same number the production config requires the lease to exceed.

        What the bound guarantees, precisely (re-audits `f495de8` F2, `8377440` F4): the
        publisher RETURNS within `deadline` — the `queue.get(timeout=deadline)` is the only wait,
        and the overrun branch does no unbounded work (no synchronous `client.close()`, no join),
        so failure accounting lands well inside the lease the config sizes against. And clean
        process exit, since the worker is daemon. What it cannot guarantee: retraction of the
        attempt itself. A request whose bytes are already moving may still complete — a late 2xx
        after failure accounting is the documented at-least-once residual (A6) the platform
        dedupes on (case_id, run_id). No in-process design retracts an in-flight request; a
        supervised child process is the only stronger boundary and is deferred (recorded on the
        bus). Detached attempts are tracked, reaped, and CAPPED: at `_MAX_ORPHAN_SENDS` the
        publisher stops claiming (`at_capacity`), so a wedged endpoint stalls delivery instead of
        leaking threads/connections without bound.
        """
        deadline = OUTBOX_ATTEMPT_DEADLINE_PHASES * self.settings.outbox_http_timeout_seconds
        self._reap_orphans()
        outcome: queue.Queue = queue.Queue(maxsize=1)
        client = self.http

        def attempt() -> None:
            try:
                self._send_and_drain(client, request)
                outcome.put((None,))
            except BaseException as exc:  # noqa: BLE001 — marshalled to the caller verbatim
                outcome.put((exc,))

        worker = threading.Thread(target=attempt, name="outbox-send", daemon=True)
        worker.start()
        try:
            (exc,) = outcome.get(timeout=deadline)
        except queue.Empty:
            # Detach STRICTLY WITHIN the budget (re-audit `8377440` F4): NO synchronous
            # `client.close()` and NO join here — both are themselves unbounded (a wedged pool
            # never closes) and would push the publisher's return past the `4 × timeout` the
            # lease is sized against, so a valid lease could expire before failure accounting.
            # The worker is daemon (never blocks process exit) and keeps using the SHARED client;
            # its connection releases when it finishes or its own per-phase httpx timeout fires.
            # A truly wedged OS call holds one connection until the OS gives up — bounded by the
            # capacity gate (`at_capacity`, checked before the next claim) and reclaimed fully
            # only by the supervised child-process boundary (deferred; recorded on the bus). No
            # client rebuild: a fresh client is pointless while the old one is neither closed nor
            # exhausted, and rebuilding is just more unbudgeted work on the hot path.
            self._orphans.append(worker)
            log.error("outbox_attempt_deadline_exceeded", url=str(request.url),
                      deadline_seconds=deadline, live_orphans=len(self._orphans))
            raise _AttemptDeadlineExceeded(
                f"delivery attempt exceeded its {deadline}s wait-bound and was detached"
            ) from None
        # The worker put its result as its last act; it is finished. No join needed.
        if exc is not None:
            raise exc

    def _reap_orphans(self) -> None:
        self._orphans = [t for t in self._orphans if t.is_alive()]

    def at_capacity(self) -> bool:
        """True when detached (wedged) sends have reached the resource cap. A REAL gate, not a
        log line (re-audit `8377440` F5): `process_once` refuses to claim while this holds, so
        resources stay bounded — no new attempt is staged past the cap — instead of one thread
        and one held connection accreting per stuck send."""
        self._reap_orphans()
        return len(self._orphans) >= _MAX_ORPHAN_SENDS

    def close(self) -> None:
        """Release the HTTP client under ONE bounded TOTAL deadline covering BOTH the orphan joins
        and `self.http.close()` itself (re-audit `8377440` F5 bounded the joins; `538e55e..42e1c7d`
        F5 bounds the close). `http.close()` can hang on a wedged pool, so it runs in a daemon
        joined only for the remaining budget: a bounded close may leak a connection for the OS to
        reap, but it never waits forever — even at zero orphans. Daemon threads never block
        interpreter exit, so this is hygiene for a long-lived embedder (tests, dev worker)."""
        cutoff = time.monotonic() + _ORPHAN_DRAIN_SECONDS
        for t in self._orphans:
            t.join(timeout=max(0.0, cutoff - time.monotonic()))
        self._reap_orphans()
        closer = threading.Thread(
            target=self._safe_http_close, name="outbox-http-close", daemon=True
        )
        closer.start()
        closer.join(timeout=max(0.0, cutoff - time.monotonic()))
        if closer.is_alive():
            log.warning("outbox_http_close_timeout", drain_seconds=_ORPHAN_DRAIN_SECONDS)

    def _safe_http_close(self) -> None:
        with contextlib.suppress(Exception):
            self.http.close()

    def _send_and_drain(self, client: httpx.Client, request: httpx.Request) -> None:
        """Send, treat the response status as the acknowledgement, and drain the body safely.

        The status is the witness, but the body is still DRAINED — under a byte cap and a
        wall-clock budget — before the response is closed. Closing an unconsumed HTTP/1.1
        response is not free: httpcore cannot know where the next response begins on that
        socket, so it discards the connection. Measured on real sockets, four sequential
        callbacks opened four connections instead of one (a full TCP+TLS handshake per delivery
        against the production `https` callback URL) and the receiver took a broken pipe writing
        its response body every single time. Draining also restores the truncation signal: a
        gateway that emits `200` headers before its origin commits, then closes, raises here and
        is retried, instead of stamping a terminal witness for bytes nobody kept.

        Abandoning the drain past the cap or budget is deliberate and DELIVERS: the witness this
        taxonomy records is receipt of the 2xx status for the exact staged request bytes — the
        response body carries no callback semantics, and completing its framing would prove
        nothing more about what the platform accepted. Failing here instead would let any
        verbose-but-healthy receiver drive a delivered callback through retries into a dead
        letter — a false negative manufactured from our own refusal to keep reading. The cost of
        abandonment is one dropped connection, paid in the pathological case instead of the
        normal one. (A framing violation the drain OBSERVES — premature close, bad chunking —
        still raises and retries; that is the receiver breaking HTTP, not us walking away.)
        """
        deadline = time.monotonic() + self.settings.outbox_http_timeout_seconds
        response = client.send(request, stream=True)
        try:
            response.raise_for_status()
            if response.is_stream_consumed:
                # The transport already read the whole body (any eager-content client, and every
                # `MockTransport` handler that returns a plain `httpx.Response`). Nothing is left
                # on the socket, so there is nothing to drain — and `iter_raw()` would raise
                # `StreamConsumed` for a delivery that in fact succeeded.
                return
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > _MAX_ACK_BODY_BYTES:
                # The receiver has promised more than the cap: the drain would be abandoned
                # anyway, so skip the pointless read instead of paying for _MAX_ACK_BODY_BYTES
                # of a body we will not keep.
                log.warning("outbox_callback_ack_body_abandoned",
                            declared_bytes=int(declared), url=str(request.url))
                return
            consumed = 0
            for chunk in response.iter_raw():
                consumed += len(chunk)
                if consumed > _MAX_ACK_BODY_BYTES or time.monotonic() > deadline:
                    log.warning(
                        "outbox_callback_ack_body_abandoned",
                        consumed_bytes=consumed, url=str(request.url),
                    )
                    return
        finally:
            response.close()

    def _admission_budget(self) -> float:
        """Seconds a freshly admitted attempt's claim must survive: the enforced per-attempt send
        deadline (`OUTBOX_ATTEMPT_DEADLINE_PHASES × timeout`) plus the DB-accounting margin. Sized
        so the failure/terminal write always lands before any reclaimer can take the row — the
        explicit accounting budget re-audit `538e55e..42e1c7d` F1 asks for, not a boot-time margin
        alone."""
        return (
            OUTBOX_ATTEMPT_DEADLINE_PHASES * self.settings.outbox_http_timeout_seconds
            + self.settings.outbox_lease_margin_seconds
        )

    def _admit(self, session: Session, *, outbox_id: int, token: str) -> bool:
        """Count this transport attempt and re-anchor the claim over the send + accounting window,
        fenced on the LIVE claim, inside the caller's transaction. Returns True if admitted, False
        if the claim is no longer live (expired lease → reclaimable).

        Counting happens HERE — before the send — not at accounting after it (re-audit
        `538e55e..42e1c7d` F1): a publisher that admits then dies still leaves `attempts`
        incremented, so a reclaimer counts its own attempt on top and max-attempt dead-letter
        stays reachable; no admitted transport call is ever under-counted. `_record_failure` then
        writes the same absolute count (`row.attempts + 1`) idempotently, so a live claimant's own
        failure accounting is unchanged. Re-anchoring the lease to `deadline + accounting budget`
        (wall clock, not transaction now()) means a production-valid claim cannot expire between
        admission and the failure/terminal write, so no reclaimer sends a duplicate while the first
        attempt is still being accounted."""
        admitted = session.execute(
            text(
                "UPDATE outbox SET attempts = attempts + 1, "
                # GREATEST so admission only ever EXTENDS the claim — never shortens a healthy lease
                # to the (smaller) attempt budget (re-audit `42e1c7d..b39b82a` F2). A near-expiry
                # claim is extended to cover the send + accounting; a fresh 300s claim keeps its 300s.
                "claim_lease_expires_at = GREATEST(claim_lease_expires_at, "
                "clock_timestamp() + make_interval(secs => :budget)) "
                "WHERE id = :id AND status = 'pending' AND claim_token = :token "
                "AND claim_lease_expires_at > clock_timestamp() RETURNING id"
            ),
            {"id": outbox_id, "token": token, "budget": self._admission_budget()},
        ).first()
        return admitted is not None

    def _record_attempt(self, *, outbox_id: int, token: str, wire_version: str,
                        request_sha256: str) -> str:
        """Admit (count + re-anchor the lease) and commit one immutable attempt row under the live
        claim, atomically. Returns its `attempt_id`.

        This records staged INTENT — the exact bytes, durably, before any transmission is
        tried — not transmission itself: the process can die between this commit and the send.

        Admission and the evidence INSERT share ONE transaction fenced on the live claim, and both
        run BEFORE the send: a claimant whose lease expired admits nothing, stages nothing, and
        therefore transmits nothing. Raises `_StaleClaim` in that case.

        Precisely: expiry makes the row reclaimable and stops a stale claimant from staging or
        sending anything NEW. It cannot revoke a request already on the wire — no lease can, and
        the residual duplicate that follows from it is the documented at-least-once property (A6),
        which the platform dedupes on `(case_id, run_id)`.
        """
        attempt_id = str(uuid.uuid4())
        with uow(self.session_factory) as session:
            take_shared_fence(session)
            if not self._admit(session, outbox_id=outbox_id, token=token):
                raise _StaleClaim(outbox_id)
            # The admit above proved the claim live and re-anchored the lease in THIS transaction,
            # holding the row lock; the INSERT's admission trigger independently re-checks
            # token/status/kind, so a plain INSERT is safe and atomic with the count.
            session.execute(
                text(
                    "INSERT INTO outbox_delivery_attempts "
                    "(attempt_id, outbox_id, claim_token, wire_version, request_sha256) "
                    "VALUES (:attempt_id, :outbox_id, :token, :wire_version, :sha)"
                ),
                {"attempt_id": attempt_id, "outbox_id": outbox_id, "token": token,
                 "wire_version": wire_version, "sha": request_sha256},
            )
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
        property (A6) rather than a gap in evidence. They DO get the same presend admission
        (re-audit `cbb783b` F5, `538e55e..42e1c7d` F1) — counted and lease-re-anchored before the
        provider call, same as a callback's `_record_attempt` — because a stale claimant emailing a
        token it cached before losing the row is a real side effect, and an admitted-then-crashed
        email must not go under-counted either.
        """
        if kind == DECISION_CALLBACK:
            return self._deliver_decision_callback(payload, outbox_id=outbox_id, token=token)
        if kind == POC_EMAIL:
            self._admit_poc_claim(outbox_id=outbox_id, token=token)
            self.email_sender.send(payload["to"], payload["subject"], payload["body"])
            return None
        raise ValueError(f"unknown outbox kind: {kind}")

    def _admit_poc_claim(self, *, outbox_id: int, token: str) -> None:
        """POC-email counterpart of `_record_attempt`'s admission: count the attempt and re-anchor
        the claim over the send + accounting window, fenced on the live claim. Raises `_StaleClaim`
        BEFORE the provider call if the claim is no longer live.

        A POC email stages no wire-evidence row (nothing reconciles it against a remote ledger; its
        duplicate-on-retry is the at-least-once property, A6), but it is COUNTED here, before the
        send, for the same reason a callback is: a crash between admission and accounting must not
        leave the attempt uncounted (max-attempt dead-letter must stay reachable), and the claim
        must cover the send so a reclaimer cannot double-send while this one is still accounting.

        This makes the row unsendable by a stale claimant; it does NOT revoke a request already on
        the wire. No lease can: once bytes leave the socket the side effect exists whatever the
        clock says. That residual is the documented at-least-once property (A6), and the real mail
        provider must key its idempotency on `outbox.id` so a duplicate is collapsed receiver-side.
        """
        with uow(self.session_factory) as session:
            take_shared_fence(session)
            if not self._admit(session, outbox_id=outbox_id, token=token):
                raise _StaleClaim(outbox_id)

    # -- loop ---------------------------------------------------------------

    def process_once(self) -> bool:
        """Claim and deliver one pending row of one (case, stream). Returns False when idle."""
        # Circuit breaker (re-audit `8377440` F5): if detached wedged sends have hit the cap, do
        # NOT claim another row — claiming would stage a new attempt whose thread/connection we
        # cannot bound. Refusing to claim stalls delivery (surfaced as saturated) rather than
        # leaking resources; it is the safe failure while an operator addresses the wedged endpoint.
        if self.at_capacity():
            if not self._saturated:  # log the edge, not every poll
                log.critical("outbox_delivery_saturated",
                             live_orphans=len(self._orphans), cap=_MAX_ORPHAN_SENDS)
            self._saturated = True
            return False
        self._saturated = False
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

        # Domain floor (re-audit `8aba2df..2cee937` R3-F3): `outbox.attempts` is int4 with no >= 0
        # constraint, so a malformed/corrupt import with a NEGATIVE count made the ceiling check
        # (`attempts >= max`) practically unreachable and licensed sends past the limit (INT4_MIN ⇒
        # ~2^31 sends). Fail closed BEFORE supersession, reconciliation, admission or any transport:
        # a negative counter is not a sendable state. No increment, no send (POC body redacted). A
        # DB CHECK `attempts >= 0` is specified for the next mutable migration (ROADMAP PR 10) as the
        # durable backstop; this runtime guard holds until then and is the fail-closed authority.
        if row.attempts < 0:
            self._dead_letter_over_ceiling(
                row, token, note="attempts is negative (malformed counter) — fail closed"
            )
            return True

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

        # Expired-claim RECONCILIATION before any new send (re-audit `42e1c7d..b39b82a` F1). A
        # non-NULL prev_claim_token means the prior claim expired WITHOUT a terminal clearing it:
        # the predecessor admitted an attempt (attempts already incremented, before its send) or
        # crashed mid-cycle. Sending again here would make one more network call and could push
        # real sends past outbox_max_attempts. Reconcile instead — dead-letter at/over max, else
        # back off and release — and let a LATER fresh claim make the next attempt.
        if row.prev_claim_token is not None:
            self._reconcile_expired_claim(row, token)
            return True

        # Ceiling guard (re-audit `b39b82a..b53daf4` F2): never make another external call for a row
        # whose DURABLE attempts already meet the CURRENT max. Reconciliation above covers an
        # expired-but-uncleared claim; this covers a cleanly-released pending row left at/over the
        # ceiling when an operator LOWERS outbox_max_attempts. Terminalise here, not in _CLAIM_SQL,
        # so the row is dead-lettered rather than stranded pending forever.
        if row.attempts >= self.settings.outbox_max_attempts:
            self._dead_letter_over_ceiling(row, token)
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
        # `row.attempts` is the claim snapshot; admission already incremented the DB to exactly
        # this value, so writing it back ABSOLUTELY (SET attempts=:a, never attempts+1) is
        # idempotent with the count and never doubles it (re-audit `538e55e..42e1c7d` F1). This
        # method still owns the count for a claimant that reaches it directly (the fencing tests
        # drive it without admission), which is why the value is computed here too.
        attempts = row.attempts + 1
        dead = attempts >= self.settings.outbox_max_attempts
        delay = self._backoff_seconds(attempts)
        # Fenced on `claim_token` alone — deliberately NOT on a live lease. A failure write is
        # BOOKKEEPING (attempts, backoff, dead-letter), not a witness: the danger the live-lease
        # predicate exists for — a lapsed claimant stamping delivery evidence on a row someone
        # else now owns — cannot arise here, and token rotation already fences the reclaimed
        # case (a new claimant mints a new token, so this UPDATE matches zero rows). Requiring a
        # live lease ADDITIONALLY made any failure that landed after lease expiry unrecordable:
        # attempts never bumped, next_attempt_at stayed due, and the loop reclaimed and resent
        # immediately — an unthrottled external send per cycle, with max_attempts unreachable.
        with uow(self.session_factory) as session:
            if dead:
                redacted_payload = json.dumps({"redacted": True})
                applied = session.execute(
                    text(
                        "UPDATE outbox SET status='dead', attempts=:a, last_error=:e, "
                        "claim_token=NULL, claim_lease_expires_at=NULL, claimed_by=NULL, "
                        "payload_json = CASE WHEN :redact_payload THEN CAST(:redacted AS jsonb) "
                        "ELSE payload_json END "
                        "WHERE id=:id AND status='pending' AND claim_token=:token RETURNING id"
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

    def _backoff_seconds(self, attempts: int) -> int:
        """Saturating exponential backoff via the ONE shared queue-backoff helper (re-audit
        `5b0f0b8..b75a320` R4-F3), so the queue and outbox schedules cannot diverge and no attempt
        count overflows timestamptz. base × 2**(attempts-1), capped; a zero base yields 0 (retry when
        due). The outbox schedule carries no jitter (its ordering is per-row FIFO, not thundering-herd
        sensitive)."""
        return saturating_backoff_seconds(
            self.settings.outbox_backoff_base_seconds, attempts, cap_seconds=_MAX_BACKOFF_SECONDS
        )

    def _fenced_dead_letter(self, session, row, token, *, note: str):
        """Terminalise a claimed row to 'dead' fenced on `token`, redacting a POC body — no send, no
        attempts increment. Shared by the expired-claim reconciliation and the on-claim ceiling
        guard so the terminal write (columns cleared, POC redaction) stays defined once. Returns the
        applied row, or None if a straggler already moved it (caller logs the stale claim)."""
        redacted_payload = json.dumps({"redacted": True})
        return session.execute(
            text(
                "UPDATE outbox SET status='dead', last_error=:e, "
                "claim_token=NULL, claim_lease_expires_at=NULL, claimed_by=NULL, "
                "payload_json = CASE WHEN :redact_payload THEN CAST(:redacted AS jsonb) "
                "ELSE payload_json END "
                "WHERE id=:id AND status='pending' AND claim_token=:token RETURNING id"
            ),
            {"id": row.id, "e": note, "token": token,
             "redact_payload": row.kind == POC_EMAIL, "redacted": redacted_payload},
        ).first()

    def _dead_letter_over_ceiling(self, row, token, note=None) -> None:
        """Fenced dead-letter a freshly-claimed row that is outside the sendable attempts domain,
        before any transport: DURABLE attempts at/over the CURRENT outbox_max_attempts (re-audit
        `b39b82a..b53daf4` F2), or a negative/malformed counter (re-audit `8aba2df..2cee937` R3-F3,
        via the `note` argument). The expired-claim reconciliation only fires for a non-NULL
        prev_claim_token; a cleanly-released pending row left outside the domain would otherwise take
        the ordinary admit+send path and make one more external call. No increment, no send (POC body
        redacted)."""
        if note is None:
            note = "attempts at/above max_attempts on claim (ceiling lowered)"
        with uow(self.session_factory) as session:
            applied = self._fenced_dead_letter(session, row, token, note=note)
        if applied is None:
            log.warning("outbox_stale_claim_completion", outbox_id=row.id, attempted="ceiling_dead")
            return
        log.error("outbox_dead_letter", outbox_id=row.id, kind=row.kind, error=note)

    def _reconcile_expired_claim(self, row, token) -> None:
        """Reconcile a row reclaimed from an EXPIRED-but-UNCLEARED claim WITHOUT sending again
        (re-audit `42e1c7d..b39b82a` F1). The predecessor either admitted an attempt (attempts was
        incremented BEFORE its send, so it is already counted) or crashed between claim and
        admission (no attempt, attempts unchanged). Either way a fresh send here could exceed
        outbox_max_attempts, so we only account:

        - attempts >= max  → dead-letter, fenced, no send (redact POC body, same as _record_failure);
        - 0 < attempts < max → back off ONE boundary and release; a later fresh claim sends next;
        - attempts == 0 → nothing was ever sent; just release the stale claim, due now.

        Fenced on the reclaimer's token (like _record_failure): a straggler predecessor finishing
        late matches zero rows and is a no-op. attempts is NEVER incremented here — the crashed
        attempt, if any, was already counted at its own admission.
        """
        attempts = row.attempts
        note = "reconciled: prior claim expired without terminalizing"
        with uow(self.session_factory) as session:
            if attempts == 0:
                # No attempt was ever admitted under the dead claim; release and let a fresh claim
                # make attempt 1. Nothing sent, nothing to account.
                applied = session.execute(
                    text(
                        "UPDATE outbox SET claim_token=NULL, claim_lease_expires_at=NULL, "
                        "claimed_by=NULL, next_attempt_at=now() "
                        "WHERE id=:id AND status='pending' AND claim_token=:token RETURNING id"
                    ),
                    {"id": row.id, "token": token},
                ).first()
                marker = "reconcile_release"
            elif attempts >= self.settings.outbox_max_attempts:
                applied = self._fenced_dead_letter(session, row, token, note=note)
                marker = "reconcile_dead"
            else:
                delay = self._backoff_seconds(attempts)
                applied = session.execute(
                    text(
                        "UPDATE outbox SET last_error=:e, "
                        "next_attempt_at = now() + make_interval(secs => :delay), "
                        "claim_token=NULL, claim_lease_expires_at=NULL, claimed_by=NULL "
                        "WHERE id=:id AND status='pending' AND claim_token=:token RETURNING id"
                    ),
                    {"id": row.id, "e": note, "delay": delay, "token": token},
                ).first()
                marker = "reconcile_retry"
        if applied is None:
            log.warning("outbox_stale_claim_completion", outbox_id=row.id, attempted=marker)
            return
        if marker == "reconcile_dead":
            log.error("outbox_dead_letter", outbox_id=row.id, kind=row.kind,
                      error="reconciled at max_attempts (predecessor did not terminalize)")
        else:
            log.info("outbox_reclaim_reconciled", outbox_id=row.id, kind=row.kind,
                     attempts=attempts, outcome=marker)

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
            if self.at_capacity():
                # Saturation is a SUPERVISOR-VISIBLE terminal state for the long-lived process, not
                # an invisible forever-sleep (re-audit `538e55e..42e1c7d` F6). Exit nonzero (via the
                # worker) so a supervisor restarts us; the fresh process drops the daemon attempts
                # and reclaims their connections. process_pending (bounded) keeps its soft-stall.
                log.critical("outbox_delivery_saturated_exit",
                             live_orphans=len(self._orphans), cap=_MAX_ORPHAN_SENDS)
                raise OutboxSaturated(
                    f"outbox delivery saturated: {len(self._orphans)} detached send(s) "
                    f">= cap {_MAX_ORPHAN_SENDS}; exiting for supervised restart"
                )
            if not self.process_once():
                time.sleep(poll_seconds)
