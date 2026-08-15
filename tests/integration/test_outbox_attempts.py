"""PR 7b-core: the pre-HTTP attempt authority, and the witness taxonomy it makes possible.

`outbox.callback_wire_sha256` is written in the fenced terminal transaction — one transaction too
late to be evidence. This suite exercises the publisher's own documented residual (HTTP returns
2xx, the terminal then faults, the row stays `pending`) and proves that after it the tool still
holds the exact digest of the bytes it sent. Before the attempt row existed, that same sequence
left a NULL digest, and reading NULL as "never delivered" would have been wrong in exactly the
case an operator was reconciling.
"""

import hashlib

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from kyc_tool.config import ProcessRole
from kyc_tool.outbox import witness
from kyc_tool.outbox.publisher import _CLAIM_SQL, OutboxPublisher, _StaleClaim
from kyc_tool.workers.retention import prune
from tests.conftest import process_context
from tests.integration.test_outbox_supersession import _enqueue_cb, _seed_decisions

pytestmark = pytest.mark.postgres

# Minimal but PRODUCTION-VALID outbox settings for the F1 accounting tests: the lease (int, ge=1)
# must exceed 4 × http_timeout + margin = 4 × 0.1 + 0.5 = 0.9, and 1 does. This is the exact tuple
# whose race Codex reproduced (`538e55e..42e1c7d` F1): a claim that is valid at claim time yet has
# little lease left by the time the send starts.
_F1_SETTINGS = {
    "outbox_http_timeout_seconds": 0.1,
    "outbox_lease_margin_seconds": 0.5,
    "outbox_lease_seconds": 1,
    "outbox_backoff_base_seconds": 0,
}


class _InjectedFault(RuntimeError):
    pass


def _witness_of(session_factory, run_id):
    with session_factory() as s:
        return s.execute(
            text(witness.WITNESS_SELECT + " AND o.run_id = :r ORDER BY o.id DESC"), {"r": run_id}
        ).one()


def _attempts(session_factory, run_id):
    with session_factory() as s:
        return s.execute(text(
            "SELECT a.attempt_id, a.claim_token, a.wire_version, a.request_sha256 "
            "FROM outbox_delivery_attempts a JOIN outbox o ON o.id = a.outbox_id "
            "WHERE o.run_id = :r ORDER BY a.attempted_at"
        ), {"r": run_id}).all()


def test_send_before_stamp_still_leaves_the_exact_request_digest(
    session_factory, publisher, callback_capture, monkeypatch, clean_db
):
    """THE finding. 2xx received, terminal transaction faults → row stays pending with a NULL
    terminal digest, but the attempt row holds the digest of the exact bytes that were sent, and
    the row classifies as `send_intent_witnessed` rather than as never-delivered."""
    _seed_decisions(session_factory, "ca", [1])
    _enqueue_cb(session_factory, "ca", 1)

    def _boom(self, row, token, wire_sha256=None):
        raise _InjectedFault("send-before-stamp: HTTP sent, terminal not committed")

    monkeypatch.setattr(type(publisher), "_record_delivered", _boom)
    with pytest.raises(_InjectedFault):
        publisher.process_once()

    assert len(callback_capture.requests) == 1          # the bytes really went out
    sent = callback_capture.requests[0]["raw"]

    row = _witness_of(session_factory, "ca-r1")
    assert row.status == "pending"                      # terminal never committed
    assert row.callback_wire_sha256 is None             # ... so the terminal digest is absent
    assert row.witness == witness.SEND_INTENT_WITNESSED     # ... but the tool is NOT blind

    attempts = _attempts(session_factory, "ca-r1")
    assert len(attempts) == 1
    # byte-for-byte: the recorded digest is of the bytes httpx transmitted, not of a re-encoding
    # of payload_json (which jsonb would have reordered).
    assert attempts[0].request_sha256 == hashlib.sha256(sent).hexdigest()
    assert attempts[0].wire_version == "legacy"


def test_delivered_row_is_delivery_witnessed_and_digests_agree(
    session_factory, publisher, callback_capture, clean_db
):
    """Happy path: the terminal digest and the pre-HTTP attempt digest describe the same bytes."""
    _seed_decisions(session_factory, "cb", [1])
    _enqueue_cb(session_factory, "cb", 1)
    assert publisher.process_pending() == 1

    row = _witness_of(session_factory, "cb-r1")
    assert row.status == "delivered"
    assert row.witness == witness.DELIVERY_WITNESSED
    sent = callback_capture.requests[0]["raw"]
    assert row.callback_wire_sha256 == hashlib.sha256(sent).hexdigest()
    assert [a.request_sha256 for a in _attempts(session_factory, "cb-r1")] == [row.callback_wire_sha256]


def test_stale_claimant_records_no_attempt_and_sends_nothing(
    session_factory, publisher, callback_capture, clean_db
):
    """A claimant whose lease was stolen must not transmit at all — the fence is BEFORE the send,
    not only before the terminal. Recording an attempt (or sending) here would attribute bytes to
    a row this publisher no longer owns."""
    _seed_decisions(session_factory, "cc", [1])
    _enqueue_cb(session_factory, "cc", 1)
    with session_factory() as s:
        row = s.execute(text(
            "UPDATE outbox SET status='pending', claim_token=gen_random_uuid(), "
            "claim_lease_expires_at=now()+interval '1 hour', claimed_by='winner' "
            "WHERE run_id='cc-r1' RETURNING id, kind, payload_json")).one()
        s.commit()

    stale_token = "00000000-0000-0000-0000-0000000000ff"   # a token that never owned this row
    with pytest.raises(_StaleClaim):
        publisher._deliver(row.kind, row.payload_json, outbox_id=row.id, token=stale_token)

    assert callback_capture.requests == []                  # ZERO network traffic
    assert _attempts(session_factory, "cc-r1") == []         # and nothing written


def test_retry_under_a_new_claim_cannot_overwrite_the_first_attempt(
    session_factory, publisher, callback_capture, monkeypatch, clean_db
):
    """Attempts are insert-only: a second attempt adds a row, it does not amend the first. The
    first attempt remains the record of what was sent under the first claim even after a retry."""
    _seed_decisions(session_factory, "cd", [1])
    _enqueue_cb(session_factory, "cd", 1)

    def _boom(self, row, token, wire_sha256=None):
        raise _InjectedFault("terminal faulted")

    monkeypatch.setattr(type(publisher), "_record_delivered", _boom)
    with pytest.raises(_InjectedFault):
        publisher.process_once()
    first = _attempts(session_factory, "cd-r1")
    assert len(first) == 1

    # reclaim under a fresh token (what the lease reaper effectively does) and let it succeed
    monkeypatch.undo()
    with session_factory() as s:
        s.execute(text("UPDATE outbox SET claim_token=NULL, claim_lease_expires_at=NULL, "
                       "claimed_by=NULL, next_attempt_at=now() WHERE run_id='cd-r1'"))
        s.commit()
    assert publisher.process_pending() == 1

    after = _attempts(session_factory, "cd-r1")
    assert len(after) == 2, "the retry must ADD an attempt, never amend one"
    assert after[0] == first[0], "attempt 1 is immutable"
    assert after[0].claim_token != after[1].claim_token, "two distinct claims are distinguishable"
    assert _witness_of(session_factory, "cd-r1").witness == witness.DELIVERY_WITNESSED


def test_decision_callback_cannot_be_born_delivered(session_factory, clean_db):
    """At head, a decision callback cannot be INSERTed already-delivered: rows enter the
    outbox pending, unwitnessed and unclaimed, and reach `delivered` only through the
    witnessed transition. Genuine pre-013 rows are adopted by migration 014 at their own
    revision (classification coverage lives in test_migration_014.py); a head-schema writer
    producing a born-delivered row is fabricating history and must be refused."""
    _seed_decisions(session_factory, "ce", [1])
    with session_factory() as s, pytest.raises(ProgrammingError, match="enter the outbox pending"):
        s.execute(text(
            "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, "
            "status, delivered_at, payload_json) VALUES ('decision_callback','ce','ce-r1',"
            "'decision',1,'delivered',now(),'{\"run_id\":\"ce-r1\"}'::jsonb)"))


def test_never_sent_row_is_not_accepted(session_factory, clean_db):
    """The only state in which the tool may claim on its own evidence that the platform does not
    hold the callback: no attempt was ever committed, so nothing was transmitted."""
    _seed_decisions(session_factory, "cf", [1])
    _enqueue_cb(session_factory, "cf", 1)
    row = _witness_of(session_factory, "cf-r1")
    assert row.status == "pending"
    assert row.witness == witness.NOT_ACCEPTED


@pytest.mark.parametrize("bad", [
    ("not-hex-" + "0" * 56, "legacy"),          # request_sha256 shape
    ("a" * 64, "made-up-encoding"),             # wire_version vocabulary
])
def test_attempt_constraints_reject_uninterpretable_witnesses(session_factory, bad, clean_db):
    """A malformed digest or an unknown encoding is worse than no witness: it looks like evidence.
    The database refuses both."""
    sha, wire_version = bad
    _seed_decisions(session_factory, "cg", [1])
    _enqueue_cb(session_factory, "cg", 1)
    with session_factory() as s:
        # claim the row first: the 015 admission trigger otherwise refuses before the CHECKs,
        # and this test exists to prove the SHAPE constraints, not the admission gate
        row = s.execute(text(
            "UPDATE outbox SET claim_token = gen_random_uuid(), "
            "claim_lease_expires_at = now() + interval '1 hour', claimed_by = 'w' "
            "WHERE run_id='cg-r1' RETURNING id, claim_token")).one()
        with pytest.raises(Exception) as exc:
            s.execute(text(
                "INSERT INTO outbox_delivery_attempts (attempt_id, outbox_id, claim_token, "
                "wire_version, request_sha256) VALUES (gen_random_uuid(), :o, :t, "
                ":w, :s)"), {"o": row.id, "t": row.claim_token, "w": wire_version, "s": sha})
        assert "ck_attempt_sha_shape" in str(exc.value) or "ck_attempt_wire_vocab" in str(exc.value)
        s.rollback()


def test_retention_never_prunes_the_only_evidence_a_row_was_sent(session_factory, clean_db):
    """Retention may delete attempts for a row that already has a terminal digest, and must NOT
    delete them for one that does not — for a non-delivered row the attempt is the only proof
    bytes went out, and removing it would reclassify `send_intent_witnessed` into `not_accepted`,
    which is the tool asserting non-delivery on evidence it just destroyed."""
    _seed_decisions(session_factory, "ch", [1, 2])
    with session_factory() as s:
        # 015's triggers make fabrication illegal, so the seeding takes the LEGAL road: insert
        # pending, claim, stage the attempt under the live claim, and (for row 1 only) complete
        # the pending->delivered transition with the digest its attempt matches.
        rows = {}
        for seq, sha in ((1, "a" * 64), (2, None)):
            row_id = s.execute(text(
                "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, "
                "status) VALUES ('decision_callback','ch',:r,'decision',:seq,'pending') "
                "RETURNING id"), {"r": f"ch-r{seq}", "seq": seq}).scalar_one()
            rows[seq] = s.execute(text(
                "UPDATE outbox SET claim_token=gen_random_uuid(), "
                "claim_lease_expires_at=now() + interval '1 hour', claimed_by='seeder' "
                "WHERE id=:i RETURNING id, claim_token"), {"i": row_id}).one()
            s.execute(text(
                "INSERT INTO outbox_delivery_attempts (attempt_id, outbox_id, claim_token, "
                "wire_version, request_sha256, attempted_at) VALUES (gen_random_uuid(), :o, "
                ":t, 'legacy', :s, now() - interval '9999 days')"),
                {"o": rows[seq].id, "t": rows[seq].claim_token, "s": sha or "b" * 64})
        s.execute(text(
            "UPDATE outbox SET status='delivered', delivered_at=now(), "
            "callback_wire_sha256=:sha, wire_version='legacy', "
            "claim_token=NULL, claim_lease_expires_at=NULL, claimed_by=NULL "
            "WHERE id=:i"), {"sha": "a" * 64, "i": rows[1].id})
        s.commit()

    counts = prune(session_factory, retention_days=1)
    assert counts["outbox_attempts_pruned"] == 1  # only the delivery-witnessed one

    assert _attempts(session_factory, "ch-r1") == []                      # redundant, removed
    assert len(_attempts(session_factory, "ch-r2")) == 1                  # sole evidence, kept
    assert _witness_of(session_factory, "ch-r1").witness == witness.DELIVERY_WITNESSED
    assert _witness_of(session_factory, "ch-r2").witness == witness.SEND_INTENT_WITNESSED


def test_attempt_rows_are_immutable_at_the_database(
    session_factory, publisher, monkeypatch, clean_db
):
    """Re-audit F6: "insert-only" was a code-comment contract; now it is a trigger. UPDATE is
    always refused; DELETE is refused while the attempt is the row's sole evidence."""
    _seed_decisions(session_factory, "ci", [1])
    _enqueue_cb(session_factory, "ci", 1)

    def _boom(self, row, token, wire_sha256=None):
        raise _InjectedFault("terminal faulted")

    monkeypatch.setattr(type(publisher), "_record_delivered", _boom)
    with pytest.raises(_InjectedFault):
        publisher.process_once()  # leaves: pending row + its sole-evidence attempt
    monkeypatch.undo()

    with session_factory() as s:
        with pytest.raises(Exception, match="insert-only"):
            s.execute(text("UPDATE outbox_delivery_attempts SET request_sha256 = :s"),
                      {"s": "f" * 64})
        s.rollback()
        with pytest.raises(Exception, match="sole delivery evidence"):
            s.execute(text("DELETE FROM outbox_delivery_attempts"))
        s.rollback()
    assert len(_attempts(session_factory, "ci-r1")) == 1  # evidence intact after both tampers


def test_digest_cannot_land_on_an_undelivered_row(session_factory, clean_db):
    """Re-audit F6: without the status binding, a raw digest on a pending row reads as
    delivery_witnessed and licenses deleting its real attempts. The database now refuses."""
    _seed_decisions(session_factory, "cj", [1])
    _enqueue_cb(session_factory, "cj", 1)
    with session_factory() as s:
        with pytest.raises(
            Exception, match="pending->delivered|ck_outbox_wire_witness_delivered"
        ):
            s.execute(text(
                "UPDATE outbox SET callback_wire_sha256 = :sha, wire_version = 'legacy' "
                "WHERE run_id = 'cj-r1'"), {"sha": "a" * 64})
        s.rollback()


def test_terminal_cannot_stamp_a_digest_no_attempt_recorded(session_factory, publisher, clean_db):
    """Re-audit F6: `_record_delivered` must prove the matching attempt (same claim, same digest,
    same encoding) exists before stamping a terminal digest. A terminal that cannot is a no-op —
    the row stays pending rather than gaining a witness for bytes nothing ever staged."""
    _seed_decisions(session_factory, "ck", [1])
    _enqueue_cb(session_factory, "ck", 1)
    with session_factory() as s:  # claim the row by hand: live claim, but NO attempt row
        row = s.execute(text(
            "UPDATE outbox SET claim_token = gen_random_uuid(), "
            "claim_lease_expires_at = now() + interval '1 hour', claimed_by = 'w' "
            "WHERE run_id = 'ck-r1' RETURNING id, kind, claim_token")).one()
        s.commit()

    from kyc_tool.outbox.publisher import DeliveryReceipt

    ghost = DeliveryReceipt(attempt_id="00000000-0000-0000-0000-000000000001",
                            wire_sha256="b" * 64, wire_version="legacy")
    publisher._record_delivered(row, row.claim_token, ghost)

    with session_factory() as s:
        status, sha = s.execute(text(
            "SELECT status, callback_wire_sha256 FROM outbox WHERE run_id='ck-r1'")).one()
    assert status == "pending" and sha is None, (
        "an unwitnessed terminal must write NOTHING — not status, not a digest"
    )


def test_death_after_attempt_commit_before_send_is_intent_not_transmission(
    session_factory, publisher, callback_capture, monkeypatch, clean_db
):
    """Re-audit F9: kill the process after `_record_attempt` commits and before `http.send`
    opens a socket. The attempt row exists, ZERO bytes moved — which is why the state is named
    send_intent_witnessed and the reconciliation treats it as "ask the platform", never as
    proof of transmission."""
    _seed_decisions(session_factory, "cl", [1])
    _enqueue_cb(session_factory, "cl", 1)

    def _die(request):
        raise _InjectedFault("process died between attempt commit and socket open")

    monkeypatch.setattr(publisher.http, "send", _die)
    assert publisher.process_once() is True  # the failure is recorded, the loop survives

    assert callback_capture.requests == []                      # zero HTTP
    attempts = _attempts(session_factory, "cl-r1")
    assert len(attempts) == 1                                   # one immutable intent
    row = _witness_of(session_factory, "cl-r1")
    assert row.callback_wire_sha256 is None                     # no terminal witness
    assert row.witness == witness.SEND_INTENT_WITNESSED


def test_live_metrics_query_never_scans_terminal_history(session_factory, clean_db):
    """Re-audit F7: the exact endpoint SQL against 100k terminal rows must use 014's partial
    live-status index, not a sequential scan of history that grows for the life of the system.
    Pins the PLAN, not the values — the values were always right; the cost was the defect."""
    from kyc_tool.api.routes_metrics import LIVE_OUTBOX_SQL

    with session_factory() as s:
        s.execute(text("INSERT INTO cases (id) VALUES ('cm')"))
        # poc_email terminals: no decision-chain FKs needed, lifecycle-legal when delivered
        s.execute(text(
            "INSERT INTO outbox (kind, case_id, ordering_stream, status, delivered_at, "
            "payload_json) SELECT 'poc_email','cm','email','delivered',now(),"
            "'{\"redacted\": true}'::jsonb "
            "FROM generate_series(1, 100000)"))
        s.execute(text(
            "INSERT INTO outbox (kind, case_id, ordering_stream, status, payload_json) "
            "SELECT 'poc_email','cm','email','pending','{}'::jsonb FROM generate_series(1, 3)"))
        s.commit()
        s.execute(text("ANALYZE outbox"))
        plan = s.execute(text(f"EXPLAIN (FORMAT JSON) {LIVE_OUTBOX_SQL}")).scalar_one()
        counts = {r[0]: r[1] for r in s.execute(text(LIVE_OUTBOX_SQL))}

    assert counts == {"pending": 3}                             # exactness is untouched
    flat = str(plan)
    assert "ix_outbox_live_status" in flat, f"live index unused:\n{flat}"
    assert "'Node Type': 'Seq Scan'" not in flat.replace('"', "'"), (
        f"the live query seq-scanned outbox with 100k terminal rows present:\n{flat}"
    )


def test_decision_terminal_without_receipt_writes_nothing(
    session_factory, publisher, clean_db
):
    """Re-audit 4dfdf8a F1 — THE bypass, closed at the kind. `_record_delivered(row, token)` on a
    live-claimed decision callback used to stamp delivered/COMPLETE/published_at with no witness,
    while the taxonomy called the same row not_accepted. The requirement is now the ROW's: no
    receipt, no write of any kind."""
    _seed_decisions(session_factory, "cn", [1])
    _enqueue_cb(session_factory, "cn", 1)
    with session_factory() as s:
        row = s.execute(text(
            "UPDATE outbox SET claim_token = gen_random_uuid(), "
            "claim_lease_expires_at = now() + interval '1 hour', claimed_by = 'w' "
            "WHERE run_id = 'cn-r1' RETURNING id, kind, claim_token")).one()
        s.commit()

    publisher._record_delivered(row, row.claim_token)  # the exact call the old bypass used

    with session_factory() as s:
        status, sha, run_state, pub_at = s.execute(text(
            "SELECT o.status, o.callback_wire_sha256, r.state, d.published_at FROM outbox o "
            "JOIN runs r ON r.id = 'cn-r1' JOIN decisions d ON d.id = 'cn-r1-d' "
            "WHERE o.run_id = 'cn-r1'")).one()
    assert status == "pending" and sha is None
    assert run_state == "PUBLISH_DECISION" and pub_at is None
    assert _witness_of(session_factory, "cn-r1").witness == witness.NOT_ACCEPTED  # consistent


def test_poc_email_still_delivers_with_no_receipt_and_rejects_one(
    session_factory, publisher, email_sender, clean_db
):
    """POC email is the ONLY kind that completes without a wire receipt; handing it one is the
    same caller bug in the other direction and also writes nothing."""
    from kyc_tool.outbox.publisher import DeliveryReceipt, enqueue_poc_email

    with session_factory() as s:
        s.execute(text("INSERT INTO cases (id) VALUES ('cp')"))
        enqueue_poc_email(s, case_id="cp", to="a@x", subject="s", body="tok")
        s.commit()
    assert publisher.process_pending() == 1  # real path: claim → send → terminal, receipt None
    with session_factory() as s:
        assert s.execute(text(
            "SELECT status FROM outbox WHERE case_id='cp'")).scalar_one() == "delivered"

    with session_factory() as s:  # second email, then a direct call WITH a bogus receipt
        enqueue_poc_email(s, case_id="cp", to="b@x", subject="s", body="tok2")
        s.flush()  # the ORM INSERT must be visible to the raw claim UPDATE below
        row = s.execute(text(
            "UPDATE outbox SET claim_token = gen_random_uuid(), "
            "claim_lease_expires_at = now() + interval '1 hour', claimed_by = 'w' "
            "WHERE case_id='cp' AND status='pending' RETURNING id, kind, claim_token")).one()
        s.commit()
    publisher._record_delivered(row, row.claim_token, DeliveryReceipt(
        attempt_id="00000000-0000-0000-0000-000000000002",
        wire_sha256="c" * 64, wire_version="legacy"))
    with session_factory() as s:
        assert s.execute(text("SELECT status FROM outbox WHERE id=:i"),
                         {"i": row.id}).scalar_one() == "pending"


def test_every_delivered_decision_row_is_delivery_witnessed(
    session_factory, publisher, callback_capture, clean_db
):
    """The invariant the taxonomy rests on, asserted as a query: a delivered decision callback
    that is not delivery_witnessed cannot exist post-015 (the terminal requires the receipt, the
    trigger requires the attempt, the CHECK binds digest to delivered)."""
    _seed_decisions(session_factory, "cq", [1, 2])
    _enqueue_cb(session_factory, "cq", 1)
    _enqueue_cb(session_factory, "cq", 2)
    assert publisher.process_pending() == 2
    with session_factory() as s:
        bad = s.execute(text(witness.WITNESS_SELECT +
            " AND o.status = 'delivered' AND o.callback_wire_sha256 IS NULL")).all()
    assert bad == [], f"delivered decision rows without a witness: {bad}"


def test_receipt_naming_a_different_attempt_writes_nothing(
    session_factory, publisher, callback_capture, monkeypatch, clean_db
):
    """Re-audit 0c46443 F1: attempt_id is an AUTHORITY input now — a well-formed receipt whose id
    names some OTHER attempt (right claim, right digest, wrong identity) is a no-op terminal."""
    from kyc_tool.outbox.publisher import DeliveryReceipt

    _seed_decisions(session_factory, "cr2", [1])
    _enqueue_cb(session_factory, "cr2", 1)

    def _boom(self, row, token, receipt=None):
        raise _InjectedFault("capture the claim, skip the terminal")

    monkeypatch.setattr(type(publisher), "_record_delivered", _boom)
    with pytest.raises(_InjectedFault):
        publisher.process_once()          # stages the REAL attempt under the live claim
    monkeypatch.undo()

    with session_factory() as s:
        row = s.execute(text(
            "SELECT id, kind, case_id, run_id, attempts, claim_token "
            "FROM outbox WHERE run_id='cr2-r1'")).one()
        real = s.execute(text(
            "SELECT attempt_id, request_sha256 FROM outbox_delivery_attempts "
            "WHERE outbox_id=:o"), {"o": row.id}).one()

    wrong_id = DeliveryReceipt(
        attempt_id="00000000-0000-0000-0000-0000000000aa",   # NOT the staged attempt's id
        wire_sha256=real.request_sha256, wire_version="legacy")
    publisher._record_delivered(row, row.claim_token, wrong_id)
    with session_factory() as s:
        assert s.execute(text("SELECT status FROM outbox WHERE id=:i"),
                         {"i": row.id}).scalar_one() == "pending"

    exact = DeliveryReceipt(attempt_id=str(real.attempt_id),
                            wire_sha256=real.request_sha256, wire_version="legacy")
    publisher._record_delivered(row, row.claim_token, exact)
    with session_factory() as s:
        assert s.execute(text("SELECT status FROM outbox WHERE id=:i"),
                         {"i": row.id}).scalar_one() == "delivered"


# --- F1: attempt accounting is admission-timed, not send-timed (`538e55e..42e1c7d`) -----------


def test_attempt_is_counted_at_admission_before_the_send(session_factory, clean_db, settings):
    """outbox.attempts is incremented at ADMISSION — before the bytes leave — not deferred to
    _record_failure after the send. Proven by reading the counter from INSIDE the transport, i.e.
    at the instant of the send: if counting were send-timed the probe would see 0."""
    _seed_decisions(session_factory, "fa", [1])
    _enqueue_cb(session_factory, "fa", 1)
    sends: list[int] = []
    at_send: dict = {}

    def _probe_then_fail(request):
        sends.append(1)
        with session_factory() as s:
            at_send["attempts"] = s.execute(
                text("SELECT attempts FROM outbox WHERE run_id='fa-r1'")
            ).scalar_one()
        return httpx.Response(500)

    pub = OutboxPublisher(
        session_factory, f1 := settings.model_copy(update=_F1_SETTINGS),
        http_client=httpx.Client(transport=httpx.MockTransport(_probe_then_fail)),
        process_role=process_context(ProcessRole.OUTBOX_WORKER, f1))
    assert pub.process_once() is True
    assert sends == [1]                       # exactly one transport call
    assert at_send["attempts"] == 1           # already counted when those bytes went out


def test_admission_reanchors_a_nearly_expired_claim_to_cover_send_and_accounting(
    session_factory, clean_db, settings
):
    """A production-valid claim that reaches admission nearly expired is re-anchored to the full
    send + accounting budget, so a reclaimer cannot take the row (and double-send) while the first
    attempt is still being accounted. Deterministic: shorten the lease to ~200ms, admit, then read
    the re-anchored lease and prove a concurrent claim finds nothing."""
    _seed_decisions(session_factory, "fc", [1])
    _enqueue_cb(session_factory, "fc", 1)
    prod = settings.model_copy(update=_F1_SETTINGS)   # admission budget = 4*0.1 + 0.5 = 0.9s

    with session_factory() as s:
        rowA = s.execute(
            _CLAIM_SQL, {"lease_seconds": prod.outbox_lease_seconds, "claimed_by": "A"}
        ).one()
        s.execute(
            text("UPDATE outbox SET claim_lease_expires_at = now() + interval '200 milliseconds' "
                 "WHERE id=:i"), {"i": rowA.id},
        )
        s.commit()

    pub = OutboxPublisher(
        session_factory, prod,
        http_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
    process_role=process_context(ProcessRole.OUTBOX_WORKER, prod))
    pub._record_attempt(outbox_id=rowA.id, token=rowA.claim_token,
                        wire_version="legacy", request_sha256="a" * 64)

    with session_factory() as s:
        # re-anchored from ~200ms out to ~900ms out — comfortably past a 700ms floor
        covers = s.execute(
            text("SELECT claim_lease_expires_at > now() + interval '700 milliseconds' "
                 "FROM outbox WHERE id=:i"), {"i": rowA.id},
        ).scalar_one()
        reclaim = s.execute(_CLAIM_SQL, {"lease_seconds": 1, "claimed_by": "B"}).first()
    assert covers is True                      # lease was extended to the send+accounting window
    assert reclaim is None                     # so no reclaimer can double-send during accounting


def _admit_then_crash(pub, session_factory, prod, case):
    """Drive publisher A to claim + admit + stage one attempt under case `{case}`, then 'die'
    before terminalizing — leaving a pending row with an admitted attempt and an (expiring) claim.
    Returns the outbox id."""
    with session_factory() as s:
        rowA = s.execute(
            _CLAIM_SQL, {"lease_seconds": prod.outbox_lease_seconds, "claimed_by": "A"}
        ).one()
        s.commit()
    pub._record_attempt(outbox_id=rowA.id, token=rowA.claim_token,
                        wire_version="legacy", request_sha256="a" * 64)
    with session_factory() as s:  # A's lease expires without any terminal clearing the claim
        s.execute(text("UPDATE outbox SET claim_lease_expires_at = now() - interval '1 second' "
                       "WHERE id=:i"), {"i": rowA.id})
        s.commit()
    return rowA.id


def test_max1_crash_reclaim_dead_letters_without_a_second_send(session_factory, clean_db, settings):
    """Re-audit `42e1c7d..b39b82a` F1: with max_attempts=1, a publisher that admits (attempts=1)
    then dies must NOT be re-sent by the reclaimer — a second network call would breach the send
    ceiling. The reclaim RECONCILES the crashed attempt straight to dead-letter with ZERO new
    sends and no second attempt row."""
    _seed_decisions(session_factory, "gm", [1])
    _enqueue_cb(session_factory, "gm", 1)
    prod = settings.model_copy(update={**_F1_SETTINGS, "outbox_max_attempts": 1})
    sends: list[int] = []
    pub = OutboxPublisher(
        session_factory, prod,
        http_client=httpx.Client(transport=httpx.MockTransport(
            lambda r: (sends.append(1), httpx.Response(500))[1])),
    process_role=process_context(ProcessRole.OUTBOX_WORKER, prod))
    oid = _admit_then_crash(pub, session_factory, prod, "gm")

    assert pub.process_once() is True          # B reclaims → reconciles → dead, no send
    assert sends == []                         # the send ceiling held: zero new network calls
    with session_factory() as s:
        st = s.execute(text("SELECT status, attempts FROM outbox WHERE id=:i"), {"i": oid}).one()
    assert (st.status, st.attempts) == ("dead", 1)
    assert len(_attempts(session_factory, "gm-r1")) == 1  # no second attempt row


def test_malformed_int4_max_attempts_row_dead_letters_with_no_send_no_overflow(
    session_factory, clean_db, settings
):
    """Re-audit `d569a15..4938840` F6: a row whose attempts is already at the int4 boundary (only
    reachable via a malformed import) is dead-lettered by the pre-admission ceiling check with ZERO
    external sends and no `integer out of range` — it is NOT admitted, incremented and wedged."""
    from kyc_tool.config import PG_INT4_MAX

    _seed_decisions(session_factory, "ov", [1])
    _enqueue_cb(session_factory, "ov", 1)
    with session_factory() as s:
        s.execute(
            text("UPDATE outbox SET attempts=:a, next_attempt_at=now() - interval '1 minute' "
                 "WHERE run_id='ov-r1'"),
            {"a": PG_INT4_MAX},
        )
        s.commit()
    prod = settings.model_copy(update={**_F1_SETTINGS, "outbox_max_attempts": 8})
    sends: list[int] = []
    pub = OutboxPublisher(
        session_factory, prod,
        http_client=httpx.Client(transport=httpx.MockTransport(
            lambda r: (sends.append(1), httpx.Response(500))[1])),
    process_role=process_context(ProcessRole.OUTBOX_WORKER, prod))

    assert pub.process_once() is True            # ceiling check dead-letters before admission
    assert sends == []                           # zero external sends
    with session_factory() as s:
        st = s.execute(text("SELECT status, attempts FROM outbox WHERE run_id='ov-r1'")).one()
    assert (st.status, st.attempts) == ("dead", PG_INT4_MAX)   # unchanged — no increment, no overflow


@pytest.mark.parametrize("bad_attempts", [-1, -2147483648])
def test_negative_attempts_row_fails_closed_with_no_send(
    session_factory, clean_db, settings, bad_attempts
):
    """Re-audit `8aba2df..2cee937` R3-F3: a malformed NEGATIVE attempts counter is not a sendable
    state. `outbox.attempts` is int4 with no >= 0 floor, so a negative value made the ceiling check
    (`attempts >= max`) practically unreachable and licensed sends past the limit (INT4_MIN ⇒ ~2^31).
    The row is now dead-lettered before any transport, for both int4-min and -1."""
    _seed_decisions(session_factory, "nv", [1])
    _enqueue_cb(session_factory, "nv", 1)
    with session_factory() as s:
        s.execute(
            text("UPDATE outbox SET attempts=:a, next_attempt_at=now() - interval '1 minute' "
                 "WHERE run_id='nv-r1'"),
            {"a": bad_attempts},
        )
        s.commit()
    prod = settings.model_copy(update={**_F1_SETTINGS, "outbox_max_attempts": 1})
    sends: list[int] = []
    pub = OutboxPublisher(
        session_factory, prod,
        http_client=httpx.Client(transport=httpx.MockTransport(
            lambda r: (sends.append(1), httpx.Response(500))[1])),
    process_role=process_context(ProcessRole.OUTBOX_WORKER, prod))

    assert pub.process_once() is True
    assert sends == []                            # zero external sends, whatever the negative value
    with session_factory() as s:
        st = s.execute(text("SELECT status FROM outbox WHERE run_id='nv-r1'")).one()
    assert st.status == "dead"


def test_negative_attempts_poc_email_fails_closed_and_redacts(session_factory, clean_db, settings):
    """R3-F3 (POC arm): a negative-attempts poc_email is dead-lettered with ZERO provider calls and
    its raw-token body redacted — fail-closed, not sent."""
    from kyc_tool.outbox.publisher import enqueue_poc_email

    with session_factory() as s:
        s.execute(text("INSERT INTO cases (id) VALUES ('nq')"))
        enqueue_poc_email(s, case_id="nq", to="a@x", subject="s", body="raw-secret-token")
        s.commit()
    with session_factory() as s:  # separate session so the enqueue is durable before the raw UPDATE
        s.execute(text("UPDATE outbox SET attempts=-1, next_attempt_at=now() - interval '1 minute' "
                       "WHERE case_id='nq'"))
        s.commit()
    sender = _RaisingEmail()
    lo = settings.model_copy(update={**_F1_SETTINGS, "outbox_max_attempts": 1})

    OutboxPublisher(session_factory, lo, email_sender=sender,
           process_role=process_context(ProcessRole.OUTBOX_WORKER, lo)).process_once()
    assert sender.calls == 0                       # zero provider calls
    with session_factory() as s:
        st = s.execute(text("SELECT status, payload_json FROM outbox WHERE case_id='nq'")).one()
    assert st.status == "dead"
    assert "raw-secret-token" not in str(st.payload_json)   # POC body redacted on the terminal
    assert st.payload_json == {"redacted": True}


def test_max2_crash_reclaim_backs_off_then_a_later_cycle_sends_attempt_2(
    session_factory, clean_db, settings
):
    """Re-audit `42e1c7d..b39b82a` F1: with max_attempts=2 the reclaim of a crashed attempt only
    RECONCILES + backs off (no same-cycle send); the crashed attempt stays counted (1); a LATER
    due (fresh) claim alone sends attempt 2, and dead-letter is reached after exactly two real
    sends."""
    _seed_decisions(session_factory, "gn", [1])
    _enqueue_cb(session_factory, "gn", 1)
    prod = settings.model_copy(update={
        **_F1_SETTINGS, "outbox_max_attempts": 2, "outbox_backoff_base_seconds": 100})
    sends: list[int] = []
    pub = OutboxPublisher(
        session_factory, prod,
        http_client=httpx.Client(transport=httpx.MockTransport(
            lambda r: (sends.append(1), httpx.Response(500))[1])),
    process_role=process_context(ProcessRole.OUTBOX_WORKER, prod))
    oid = _admit_then_crash(pub, session_factory, prod, "gn")

    assert pub.process_once() is True          # reclaim → reconcile + backoff, NO send
    assert sends == []
    with session_factory() as s:
        st = s.execute(text("SELECT status, attempts, next_attempt_at > now() AS backed "
                            "FROM outbox WHERE id=:i"), {"i": oid}).one()
    assert (st.status, st.attempts, st.backed) == ("pending", 1, True)  # still 1, backed off
    assert len(_attempts(session_factory, "gn-r1")) == 1

    with session_factory() as s:  # make it due; the NEXT (fresh) claim sends attempt 2
        s.execute(text("UPDATE outbox SET next_attempt_at = now() WHERE id=:i"), {"i": oid})
        s.commit()
    assert pub.process_once() is True          # fresh claim → admit(2) → send → 500 → dead
    assert sends == [1]                        # exactly ONE new send (attempt 2)
    with session_factory() as s:
        st = s.execute(text("SELECT status, attempts FROM outbox WHERE id=:i"), {"i": oid}).one()
    assert (st.status, st.attempts) == ("dead", 2)
    assert len(_attempts(session_factory, "gn-r1")) == 2


def test_admission_never_shortens_a_healthy_claim(session_factory, clean_db, settings):
    """Re-audit `42e1c7d..b39b82a` F2: admission EXTENDS, never SHORTENS. A healthy 300s claim
    keeps its lease (GREATEST), rather than being cut to the ~0.9s attempt budget."""
    _seed_decisions(session_factory, "gh", [1])
    _enqueue_cb(session_factory, "gh", 1)
    prod = settings.model_copy(update={
        "outbox_http_timeout_seconds": 0.1, "outbox_lease_margin_seconds": 0.5,
        "outbox_lease_seconds": 300})
    pub = OutboxPublisher(
        session_factory, prod,
        http_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
    process_role=process_context(ProcessRole.OUTBOX_WORKER, prod))
    with session_factory() as s:
        rowA = s.execute(_CLAIM_SQL, {"lease_seconds": 300, "claimed_by": "A"}).one()
        before = s.execute(text("SELECT claim_lease_expires_at FROM outbox WHERE id=:i"),
                           {"i": rowA.id}).scalar_one()
        s.commit()
    pub._record_attempt(outbox_id=rowA.id, token=rowA.claim_token,
                        wire_version="legacy", request_sha256="a" * 64)
    with session_factory() as s:
        after = s.execute(text("SELECT claim_lease_expires_at FROM outbox WHERE id=:i"),
                          {"i": rowA.id}).scalar_one()
    assert after >= before  # healthy 300s lease not shortened to the smaller attempt budget


# --- F2: lowering outbox_max_attempts must not permit one more send (`b39b82a..b53daf4`) --------


def test_lowering_max_attempts_dead_letters_at_ceiling_without_a_send(
    session_factory, clean_db, settings
):
    """Re-audit `b39b82a..b53daf4` F2: a cleanly-released pending row left at/over the ceiling when
    an operator LOWERS outbox_max_attempts is dead-lettered on the next claim WITHOUT another
    external call. The normal claim path (prev_claim_token NULL) skipped the ceiling before this fix
    and would send once more past the new max — expired-claim reconciliation only covers a crash."""
    _seed_decisions(session_factory, "gp", [1])
    _enqueue_cb(session_factory, "gp", 1)
    sends: list[int] = []
    transport = httpx.MockTransport(lambda r: (sends.append(1), httpx.Response(500))[1])

    # max=3 world: one real send fails → clean release at attempts=1 (pending, claim cleared).
    hi = settings.model_copy(update={**_F1_SETTINGS, "outbox_max_attempts": 3})
    OutboxPublisher(session_factory, hi, http_client=httpx.Client(transport=transport),
           process_role=process_context(ProcessRole.OUTBOX_WORKER, hi)).process_once()
    assert sends == [1]
    with session_factory() as s:
        st = s.execute(text(
            "SELECT status, attempts, claim_token FROM outbox WHERE run_id='gp-r1'")).one()
    assert (st.status, st.attempts, st.claim_token) == ("pending", 1, None)  # cleanly released

    with session_factory() as s:  # operator makes it due AND lowers the ceiling to 1
        s.execute(text("UPDATE outbox SET next_attempt_at=now() WHERE run_id='gp-r1'"))
        s.commit()
    lo = settings.model_copy(update={**_F1_SETTINGS, "outbox_max_attempts": 1})
    assert OutboxPublisher(
        session_factory, lo, http_client=httpx.Client(transport=transport),
        process_role=process_context(ProcessRole.OUTBOX_WORKER, lo),
    ).process_once() is True                       # claim → ceiling guard → dead, no send

    assert sends == [1]                            # still exactly ONE send total — none past the max
    with session_factory() as s:
        st = s.execute(text("SELECT status, attempts FROM outbox WHERE run_id='gp-r1'")).one()
    assert (st.status, st.attempts) == ("dead", 1)         # terminalised, not stranded pending
    assert len(_attempts(session_factory, "gp-r1")) == 1   # no second attempt-evidence row


def test_backoff_saturates_below_timestamptz_overflow(session_factory, clean_db, settings):
    """Re-audit `d3c0852..23e005e` F5: the backoff schedule (base × 2**(attempts-1)) is CAPPED so
    `now() + make_interval(secs => delay)` can never overflow PostgreSQL's timestamptz. Previously,
    a production-valid `outbox_max_attempts=64` / base=10 reached ~10×2**62 s and faulted mid-write,
    leaving the row pending, claimed and unredacted. One shared helper feeds failure AND reconcile."""
    from kyc_tool.outbox.publisher import _MAX_BACKOFF_SECONDS

    prod = settings.model_copy(update={"outbox_backoff_base_seconds": 10, "outbox_max_attempts": 64})
    pub = OutboxPublisher(session_factory, prod, http_client=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200))),
               process_role=process_context(ProcessRole.OUTBOX_WORKER, prod))
    assert pub._backoff_seconds(1) == 10          # base
    assert pub._backoff_seconds(5) == 160         # 10 × 2**4
    assert pub._backoff_seconds(63) == _MAX_BACKOFF_SECONDS   # saturated, NOT 10×2**62
    assert pub._backoff_seconds(9999) == _MAX_BACKOFF_SECONDS

    zero = settings.model_copy(update={"outbox_backoff_base_seconds": 0})
    pub0 = OutboxPublisher(session_factory, zero, http_client=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200))),
               process_role=process_context(ProcessRole.OUTBOX_WORKER, zero))
    assert pub0._backoff_seconds(50) == 0         # zero base (dev/test) → retry when due

    with session_factory() as s:  # the capped delay is actually accepted by PostgreSQL, no overflow
        assert s.execute(text("SELECT now() + make_interval(secs => :d)"),
                         {"d": _MAX_BACKOFF_SECONDS}).scalar_one() is not None


class _RaisingEmail:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, to: str, subject: str, body: str) -> None:
        self.calls += 1
        raise RuntimeError("smtp down")


def test_ceiling_dead_letter_redacts_a_poc_body_and_does_not_resend(
    session_factory, clean_db, settings
):
    """Re-audit `b39b82a..b53daf4` F2 (POC arm): the on-claim ceiling dead-letter redacts a POC
    email body (it carries the raw token) exactly like every other terminal, and calls the provider
    ZERO more times."""
    from kyc_tool.outbox.publisher import enqueue_poc_email

    with session_factory() as s:
        s.execute(text("INSERT INTO cases (id) VALUES ('gq')"))
        enqueue_poc_email(s, case_id="gq", to="a@x", subject="s", body="raw-secret-token")
        s.commit()
    sender = _RaisingEmail()

    # max=2 world: one send fails → clean release at attempts=1 (body NOT yet redacted — not dead).
    hi = settings.model_copy(update={**_F1_SETTINGS, "outbox_max_attempts": 2})
    OutboxPublisher(session_factory, hi, email_sender=sender,
           process_role=process_context(ProcessRole.OUTBOX_WORKER, hi)).process_once()
    assert sender.calls == 1
    with session_factory() as s:
        st = s.execute(text("SELECT status, attempts, payload_json FROM outbox "
                            "WHERE case_id='gq'")).one()
    assert (st.status, st.attempts) == ("pending", 1)
    assert "raw-secret-token" in str(st.payload_json)   # still present pre-terminal

    with session_factory() as s:  # due + ceiling lowered to 1
        s.execute(text("UPDATE outbox SET next_attempt_at=now() WHERE case_id='gq'"))
        s.commit()
    lo = settings.model_copy(update={**_F1_SETTINGS, "outbox_max_attempts": 1})
    OutboxPublisher(session_factory, lo, email_sender=sender,
           process_role=process_context(ProcessRole.OUTBOX_WORKER, lo)).process_once()

    assert sender.calls == 1                              # ZERO additional provider calls
    with session_factory() as s:
        st = s.execute(text("SELECT status, payload_json FROM outbox WHERE case_id='gq'")).one()
    assert st.status == "dead"
    assert "raw-secret-token" not in str(st.payload_json)  # POC body redacted on the terminal
    assert st.payload_json == {"redacted": True}
