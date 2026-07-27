"""PR 7b-core: the pre-HTTP attempt authority, and the witness taxonomy it makes possible.

`outbox.callback_wire_sha256` is written in the fenced terminal transaction — one transaction too
late to be evidence. This suite exercises the publisher's own documented residual (HTTP returns
2xx, the terminal then faults, the row stays `pending`) and proves that after it the tool still
holds the exact digest of the bytes it sent. Before the attempt row existed, that same sequence
left a NULL digest, and reading NULL as "never delivered" would have been wrong in exactly the
case an operator was reconciling.
"""

import hashlib

import pytest
from sqlalchemy import text

from kyc_tool.outbox import witness
from kyc_tool.outbox.publisher import _StaleClaim
from kyc_tool.workers.retention import prune
from tests.integration.test_outbox_supersession import _enqueue_cb, _seed_decisions

pytestmark = pytest.mark.postgres


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
    the row classifies as `attempt_witnessed` rather than as never-delivered."""
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
    assert row.witness == witness.ATTEMPT_WITNESSED     # ... but the tool is NOT blind

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


def test_pre_013_delivered_row_is_legacy_unwitnessed(session_factory, clean_db):
    """A row delivered before 013 has no digest and no attempt. It must classify as
    `legacy_unwitnessed` — NOT as `not_accepted`, and never by fabricating a digest from
    payload_json, whose jsonb key order is not the order that was sent."""
    _seed_decisions(session_factory, "ce", [1])
    with session_factory() as s:
        s.execute(text(
            "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, "
            "status, delivered_at, payload_json) VALUES ('decision_callback','ce','ce-r1',"
            "'decision',1,'delivered',now(),'{\"run_id\":\"ce-r1\"}'::jsonb)"))
        s.commit()

    row = _witness_of(session_factory, "ce-r1")
    assert row.callback_wire_sha256 is None
    assert row.witness == witness.LEGACY_UNWITNESSED


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
        outbox_id = s.execute(text("SELECT id FROM outbox WHERE run_id='cg-r1'")).scalar_one()
        with pytest.raises(Exception) as exc:
            s.execute(text(
                "INSERT INTO outbox_delivery_attempts (attempt_id, outbox_id, claim_token, "
                "wire_version, request_sha256) VALUES (gen_random_uuid(), :o, gen_random_uuid(), "
                ":w, :s)"), {"o": outbox_id, "w": wire_version, "s": sha})
        assert "ck_attempt_sha_shape" in str(exc.value) or "ck_attempt_wire_vocab" in str(exc.value)
        s.rollback()


def test_retention_never_prunes_the_only_evidence_a_row_was_sent(session_factory, clean_db):
    """Retention may delete attempts for a row that already has a terminal digest, and must NOT
    delete them for one that does not — for a non-delivered row the attempt is the only proof
    bytes went out, and removing it would reclassify `attempt_witnessed` into `not_accepted`,
    which is the tool asserting non-delivery on evidence it just destroyed."""
    _seed_decisions(session_factory, "ch", [1, 2])
    with session_factory() as s:
        rows = {}
        for seq, sha in ((1, "a" * 64), (2, None)):
            rows[seq] = s.execute(text(
                "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, "
                "status, delivered_at, callback_wire_sha256, wire_version) VALUES "
                "('decision_callback','ch',:r,'decision',:seq,"
                ":st, :dt, :sha, :wv) RETURNING id"),
                {"r": f"ch-r{seq}", "seq": seq,
                 "st": "delivered" if sha else "pending",
                 "dt": "now()" if sha else None,
                 "sha": sha, "wv": "legacy" if sha else None}).scalar_one()
            # an attempt older than any retention window
            s.execute(text(
                "INSERT INTO outbox_delivery_attempts (attempt_id, outbox_id, claim_token, "
                "wire_version, request_sha256, attempted_at) VALUES (gen_random_uuid(), :o, "
                "gen_random_uuid(), 'legacy', :s, now() - interval '9999 days')"),
                {"o": rows[seq], "s": "b" * 64})
        s.commit()

    counts = prune(session_factory, retention_days=1)
    assert counts["outbox_attempts_pruned"] == 1  # only the delivery-witnessed one

    assert _attempts(session_factory, "ch-r1") == []                      # redundant, removed
    assert len(_attempts(session_factory, "ch-r2")) == 1                  # sole evidence, kept
    assert _witness_of(session_factory, "ch-r1").witness == witness.DELIVERY_WITNESSED
    assert _witness_of(session_factory, "ch-r2").witness == witness.ATTEMPT_WITNESSED
