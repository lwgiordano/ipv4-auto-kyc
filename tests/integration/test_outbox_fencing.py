"""PR 7b-core: stream-scoped fenced claim + fenced terminals (defect 3)."""

import json

import httpx
import pytest
import structlog
from sqlalchemy import text

pytestmark = pytest.mark.postgres


def _seed_case(session_factory, case_id="c1"):
    with session_factory() as s:
        s.execute(text("INSERT INTO cases (id) VALUES (:c) ON CONFLICT DO NOTHING"), {"c": case_id})
        s.commit()


def _enqueue_email(session_factory, *, case_id, to, status="pending", next_at="now()"):
    with session_factory() as s:
        s.execute(
            text(
                f"INSERT INTO outbox (kind, case_id, ordering_stream, payload_json, status, next_attempt_at) "
                f"VALUES ('poc_email',:c,'email',CAST(:p AS jsonb),:st,{next_at})"
            ),
            {
                "c": case_id,
                "p": json.dumps({"to": to, "subject": "s", "body": "b"}),
                "st": status,
            },
        )
        s.commit()


def _pub_for(session_factory, settings):
    """A publisher bound to `session_factory` whose HTTP + email always succeed (200)."""
    from kyc_tool.outbox.publisher import OutboxPublisher

    return OutboxPublisher(
        session_factory, settings,
        http_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
    )


def test_stuck_email_does_not_block_decision_callback(
    session_factory, settings, publisher, callback_capture, clean_db
):
    """A perpetually-'pending' POC email (future backoff) in the email stream must NOT
    block the same case's decision callback in the decision stream."""
    _seed_case(session_factory, "c1")
    # a stuck email far in the future (its stream is busy/backed-off)
    _enqueue_email(session_factory, case_id="c1", to="stuck@x", next_at="now() + interval '1 hour'")
    # a due decision callback for the same case — needs a real automatic decision for the triple FK
    from kyc_tool.outbox.publisher import enqueue_decision_callback

    with session_factory() as s:
        s.execute(
            text(
                "INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, actor_json, "
                "payload_json, event_sequence) VALUES ('ev1','c1','k1','h','x','{}'::jsonb,'{}'::jsonb,1)"
            )
        )
        s.execute(text("INSERT INTO runs (id, case_id, triggering_event_id, "
                       "state) VALUES ('r1','c1','ev1','PUBLISH_DECISION')"))
        s.execute(
            text(
                "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
                "policy_shas, manual, decision_sequence) VALUES ('d1','c1','r1','approve',10,'{}'::jsonb,"
                "'enabled','{}'::jsonb,false,1)"
            )
        )
        enqueue_decision_callback(s, case_id="c1", run_id="r1", body={"case_id": "c1"}, decision_sequence=1)
        s.commit()

    assert publisher.process_pending() == 1  # the callback delivers; the stuck email is skipped
    assert len(callback_capture.requests) == 1
    with session_factory() as s:
        statuses = dict(
            s.execute(text("SELECT kind, status FROM outbox WHERE case_id='c1' ORDER BY kind")).all()
        )
    assert statuses == {"decision_callback": "delivered", "poc_email": "pending"}


def _receipt_for(session_factory, row, sha="a" * 64):
    """Stage a real attempt under `row`'s live claim and return the matching receipt.

    The hand-built winner deliveries in this suite predate the receipt requirement; the DB now
    refuses a decision terminal whose attempt does not exist (admission + witness triggers), so
    the fixture does what the real publisher does — attempt first, terminal second."""
    from kyc_tool.outbox.publisher import DeliveryReceipt

    with session_factory() as s:
        attempt_id = s.execute(text(
            "INSERT INTO outbox_delivery_attempts (attempt_id, outbox_id, claim_token, "
            "wire_version, request_sha256) VALUES (gen_random_uuid(), :o, :t, 'legacy', :sha) "
            "RETURNING attempt_id"), {"o": row.id, "t": row.claim_token, "sha": sha}).scalar_one()
        s.commit()
    return DeliveryReceipt(attempt_id=str(attempt_id), wire_sha256=sha, wire_version="legacy")


def _claim(session_factory, claimed_by):
    """Claim ONE row via the real _CLAIM_SQL; return the returned Row (carries the token)."""
    from kyc_tool.outbox.publisher import _CLAIM_SQL

    with session_factory() as s:
        row = s.execute(_CLAIM_SQL, {"lease_seconds": 3600, "claimed_by": claimed_by}).first()
        s.commit()
    return row


def _expire_lease(session_factory, oid):
    with session_factory() as s:
        s.execute(text("UPDATE outbox SET claim_lease_expires_at = now() "
                       "- interval '1 second' WHERE id=:i"), {"i": oid})
        s.commit()


def test_stale_poc_loser_touches_nothing_before_reclaimer_sends(
    session_factory, settings, publisher, clean_db
):
    """Defect 3, POC: A claims (token A); A's lease expires; B RECLAIMS (token B) but does NOT
    terminalize yet. A resuming with token A — through BOTH stale success and stale FINAL-attempt
    failure — must touch nothing: payload byte-identical, status pending, B's complete claim tuple
    + attempts + next_attempt_at unchanged, and it emits only outbox_stale_claim_completion (no
    raise). THEN B delivers and redacts exactly once."""
    _seed_case(session_factory, "c1")
    _enqueue_email(session_factory, case_id="c1", to="keep@x")
    rowA = _claim(session_factory, "A")
    assert rowA is not None
    _expire_lease(session_factory, rowA.id)
    rowB = _claim(session_factory, "B")  # B reclaims; does NOT terminalize
    assert rowB is not None and rowB.claim_token != rowA.claim_token

    with session_factory() as s:
        before = s.execute(text("SELECT payload_json::text AS p, status, "
                                "claim_token, claim_lease_expires_at, "
                                "claimed_by, attempts, next_attempt_at FROM "
                                "outbox WHERE id=:i"), {"i": rowA.id}).one()

    s2 = settings.model_copy(update={"outbox_max_attempts": 1})  # A's failure would be terminal (dead)
    stale_pub = _pub_for(session_factory, s2)
    with structlog.testing.capture_logs() as logs:
        stale_pub._record_delivered(rowA, rowA.claim_token)          # stale success → no-op
        stale_pub._record_failure(rowA, "boom", rowA.claim_token)    # stale final-attempt failure → no-op
    assert [e for e in logs if e["event"] == "outbox_stale_claim_completion"]  # audited no-op, no raise

    with session_factory() as s:
        after = s.execute(text("SELECT payload_json::text AS p, status, claim_token, claim_lease_expires_at, "
                               "claimed_by, attempts, next_attempt_at FROM "
                               "outbox WHERE id=:i"), {"i": rowA.id}).one()
    assert after == before  # A changed NOTHING — payload, status, B's whole claim tuple, attempts, clock

    publisher._record_delivered(rowB, rowB.claim_token)  # B (the real winner) delivers + redacts once
    with session_factory() as s:
        final = s.execute(text("SELECT status, payload_json FROM outbox WHERE id=:i"), {"i": rowA.id}).one()
    assert final.status == "delivered" and final.payload_json == {"redacted": True}


def test_final_poc_failure_dead_letters_and_redacts_atomically(
    session_factory, settings, clean_db
):
    """Revision 022 requires a POC email's terminal status and redaction in one fenced write.

    The old two-step code (`status='dead'` then a second redaction UPDATE) now fails the trigger,
    so this regression proves the real publisher writes the final-attempt terminal legally.
    """
    _seed_case(session_factory, "c-poc-dead")
    _enqueue_email(session_factory, case_id="c-poc-dead", to="dead@x")
    row = _claim(session_factory, "winner")

    from kyc_tool.outbox.publisher import OutboxPublisher

    publisher = OutboxPublisher(
        session_factory,
        settings.model_copy(update={"outbox_max_attempts": 1}),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
    )
    publisher._record_failure(row, "provider failed", row.claim_token)

    with session_factory() as s:
        terminal = s.execute(
            text("SELECT status, payload_json, last_error FROM outbox WHERE id=:i"),
            {"i": row.id},
        ).one()
    assert terminal.status == "dead"
    assert terminal.payload_json == {"redacted": True}
    assert terminal.last_error == "provider failed"


def test_stale_decision_loser_cannot_stamp_run_or_published_at(
    session_factory, settings, publisher, callback_capture, clean_db
):
    """Defect 3, decision callback: same A/B ordering. Stale A must change neither the outbox
    tuple nor runs.state (stays PUBLISH_DECISION) nor decisions.published_at (stays NULL). Then B
    alone stamps both (run COMPLETE, published_at set)."""
    from kyc_tool.outbox.publisher import enqueue_decision_callback

    _seed_case(session_factory, "c1")
    with session_factory() as s:
        s.execute(text("INSERT INTO events (id, case_id, idempotency_key, "
                       "payload_hash, event_type, actor_json, "
                       "payload_json, event_sequence) VALUES "
                       "('ev1','c1','k1','h','x','{}'::jsonb,'{}'::jsonb,1)"))
        s.execute(text("INSERT INTO runs (id, case_id, triggering_event_id, "
                       "state) VALUES ('r1','c1','ev1','PUBLISH_DECISION')"))
        s.execute(text("INSERT INTO decisions (id, case_id, run_id, "
                       "decision, score, gates_json, buy_enablement, "
                       "policy_shas, manual, decision_sequence) VALUES "
                       "('d1','c1','r1','approve',10,'{}'::jsonb,"
                       "'enabled','{}'::jsonb,false,1)"))
        enqueue_decision_callback(s, case_id="c1", run_id="r1", body={"run_id": "r1"}, decision_sequence=1)
        s.commit()

    rowA = _claim(session_factory, "A")
    _expire_lease(session_factory, rowA.id)
    rowB = _claim(session_factory, "B")

    stale_pub = _pub_for(session_factory, settings.model_copy(update={"outbox_max_attempts": 1}))
    from kyc_tool.outbox.publisher import DeliveryReceipt

    # A "ghost" receipt: well-formed, so the call clears the kind gate and reaches the fenced
    # SQL — where it must die on the claim fence + missing attempt. (A cannot stage a real
    # attempt: the admission trigger refuses its dead claim, which is the point.)
    ghost = DeliveryReceipt(attempt_id="00000000-0000-0000-0000-00000000dead",
                            wire_sha256="e" * 64, wire_version="legacy")
    with structlog.testing.capture_logs() as logs:
        stale_pub._record_delivered(rowA, rowA.claim_token, ghost)   # dies at the SQL fence
        stale_pub._record_delivered(rowA, rowA.claim_token)          # dies at the kind gate
        stale_pub._record_failure(rowA, "boom", rowA.claim_token)
    assert [e for e in logs if e["event"] == "outbox_stale_claim_completion"]
    assert [e for e in logs if e["event"] == "outbox_terminal_rejected_unwitnessed"]
    assert [e for e in logs if e["event"] == "outbox_stale_or_unwitnessed_completion"]

    with session_factory() as s:
        run_state = s.execute(text("SELECT state FROM runs WHERE id='r1'")).scalar_one()
        pub_at = s.execute(text("SELECT published_at FROM decisions WHERE id='d1'")).scalar_one()
        ob = s.execute(text("SELECT status, claim_token FROM outbox WHERE id=:i"), {"i": rowA.id}).one()
    assert run_state == "PUBLISH_DECISION" and pub_at is None  # A stamped NOTHING
    assert ob.status == "pending" and ob.claim_token == rowB.claim_token  # still B's

    publisher._record_delivered(rowB, rowB.claim_token, _receipt_for(session_factory, rowB))
    with session_factory() as s:
        assert s.execute(text("SELECT state FROM runs WHERE id='r1'")).scalar_one() == "COMPLETE"
        assert s.execute(text("SELECT published_at FROM decisions WHERE id='d1'")).scalar_one() is not None


def test_expired_unreclaimed_owner_stages_no_attempt(session_factory, settings, clean_db):
    """Re-audit 15d875d F4: between lease expiry and anyone reclaiming, the old owner's token
    still matches and the row is still pending — only the CLOCK has ruled against it. The claim
    SQL already treats that row as reclaimable, so the attempt fence must agree: _record_attempt
    raises _StaleClaim BEFORE any transmission and commits nothing."""
    from kyc_tool.outbox.publisher import _StaleClaim

    _seed_decision_chain(session_factory, case_id="c1", run_id="r1", decision_id="d1",
                         seq=1, ev_seq=1)
    with session_factory() as s:
        from kyc_tool.outbox.publisher import enqueue_decision_callback
        enqueue_decision_callback(s, case_id="c1", run_id="r1", body={"run_id": "r1"},
                                  decision_sequence=1)
        s.commit()
    rowA = _claim(session_factory, "A")
    _expire_lease(session_factory, rowA.id)  # nobody reclaims: token intact, status pending

    stale_pub = _pub_for(session_factory, settings)
    with pytest.raises(_StaleClaim):
        stale_pub._record_attempt(outbox_id=rowA.id, token=str(rowA.claim_token),
                                  wire_version="legacy", request_sha256="a" * 64)
    with session_factory() as s:
        assert s.execute(text("SELECT count(*) FROM outbox_delivery_attempts")).scalar_one() == 0


def test_expired_unreclaimed_poc_success_cannot_terminalize(session_factory, settings, clean_db):
    """An expired-but-not-yet-reclaimed owner is no longer authoritative.

    Production mutation this catches: `_record_delivered` checking only `claim_token=:token`.
    Without the live-lease predicate, a publisher that resumes after its lease expires can mark a
    POC token email delivered and redact its body even though `_CLAIM_SQL` already considers that
    row reclaimable by someone else.
    """
    _seed_case(session_factory, "c-expired-delivered")
    _enqueue_email(session_factory, case_id="c-expired-delivered", to="expired@x")
    row = _claim(session_factory, "A")
    assert row is not None
    _expire_lease(session_factory, row.id)

    pub = _pub_for(session_factory, settings)
    with structlog.testing.capture_logs() as logs:
        pub._record_delivered(row, row.claim_token)

    assert [e for e in logs if e["event"] == "outbox_stale_or_unwitnessed_completion"]
    with session_factory() as s:
        after = s.execute(
            text(
                "SELECT status, payload_json, delivered_at, claim_token, claimed_by "
                "FROM outbox WHERE id=:i"
            ),
            {"i": row.id},
        ).one()
    assert after.status == "pending"
    assert after.payload_json != {"redacted": True}
    assert after.delivered_at is None
    assert after.claim_token == row.claim_token and after.claimed_by == "A"


@pytest.mark.parametrize("max_attempts, expected_attempted", [(3, "retry"), (1, "dead")])
def test_expired_unreclaimed_failure_cannot_retry_or_dead_letter(
    session_factory, settings, clean_db, max_attempts, expected_attempted
):
    """An expired owner cannot burn attempts, reschedule, or dead-letter the row.

    Production mutation this catches: either `_record_failure` branch omitting the live-lease
    predicate. Token equality is not enough once the lease is past `clock_timestamp()`.
    """
    _seed_case(session_factory, f"c-expired-{expected_attempted}")
    _enqueue_email(session_factory, case_id=f"c-expired-{expected_attempted}", to="expired@x")
    row = _claim(session_factory, "A")
    assert row is not None
    _expire_lease(session_factory, row.id)

    pub = _pub_for(session_factory, settings.model_copy(update={"outbox_max_attempts": max_attempts}))
    with session_factory() as s:
        before = s.execute(
            text(
                "SELECT status, attempts, next_attempt_at, last_error, claim_token, "
                "claim_lease_expires_at, claimed_by, payload_json::text AS payload "
                "FROM outbox WHERE id=:i"
            ),
            {"i": row.id},
        ).one()
    with structlog.testing.capture_logs() as logs:
        pub._record_failure(row, "expired failure", row.claim_token)

    stale = [e for e in logs if e["event"] == "outbox_stale_claim_completion"]
    assert stale and stale[0]["attempted"] == expected_attempted
    with session_factory() as s:
        after = s.execute(
            text(
                "SELECT status, attempts, next_attempt_at, last_error, claim_token, "
                "claim_lease_expires_at, claimed_by, payload_json::text AS payload "
                "FROM outbox WHERE id=:i"
            ),
            {"i": row.id},
        ).one()
    assert after == before


def test_expired_unreclaimed_claimant_cannot_supersede(session_factory, settings, clean_db):
    """An expired owner cannot suppress a callback and complete its run.

    Production mutation this catches: `_record_superseded` checking only `claim_token=:token`.
    The higher sequence is real and already published, so the only thing that should stop this
    branch is the expired ownership lease.
    """
    from kyc_tool.outbox.publisher import enqueue_decision_callback

    _seed_decision_chain(
        session_factory, case_id="c-expired-supersede", run_id="old-r", decision_id="old-d",
        seq=1, ev_seq=1,
    )
    _seed_decision_chain(
        session_factory, case_id="c-expired-supersede", run_id="new-r", decision_id="new-d",
        seq=2, ev_seq=2,
    )
    with session_factory() as s:
        enqueue_decision_callback(
            s, case_id="c-expired-supersede", run_id="old-r", body={"run_id": "old-r"},
            decision_sequence=1,
        )
        s.execute(text("UPDATE decisions SET published_at=now() WHERE id='new-d'"))
        s.commit()
    row = _claim(session_factory, "A")
    assert row is not None
    _expire_lease(session_factory, row.id)

    pub = _pub_for(session_factory, settings)
    with structlog.testing.capture_logs() as logs:
        pub._record_superseded(row, row.claim_token)

    stale = [e for e in logs if e["event"] == "outbox_stale_claim_completion"]
    assert stale and stale[0]["attempted"] == "superseded"
    with session_factory() as s:
        after = s.execute(
            text("SELECT status, resolved_at, claim_token, claimed_by FROM outbox WHERE id=:i"),
            {"i": row.id},
        ).one()
        run_state = s.execute(text("SELECT state FROM runs WHERE id='old-r'")).scalar_one()
        audit_rows = s.execute(
            text("SELECT count(*) FROM audit_log WHERE action='outbox.superseded'")
        ).scalar_one()
    assert after.status == "pending"
    assert after.resolved_at is None
    assert after.claim_token == row.claim_token and after.claimed_by == "A"
    assert run_state == "PUBLISH_DECISION"
    assert audit_rows == 0


def _seed_decision_chain(session_factory, *, case_id, run_id, decision_id, seq, ev_seq):
    """One valid case→event→run→automatic-decision chain (decision carries `seq`); the
    caller enqueues its callback separately."""
    with session_factory() as s:
        s.execute(text("INSERT INTO cases (id) VALUES (:c) ON CONFLICT DO NOTHING"), {"c": case_id})
        s.execute(
            text(
                "INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, actor_json, "
                "payload_json, event_sequence) VALUES (:e,:c,:k,'h','x','{}'::jsonb,'{}'::jsonb,:s)"
            ),
            {"e": run_id + "-ev", "c": case_id, "k": run_id, "s": ev_seq},
        )
        s.execute(text("INSERT INTO runs (id, case_id, triggering_event_id, "
                       "state) VALUES (:r,:c,:e,'PUBLISH_DECISION')"),
                  {"r": run_id, "c": case_id, "e": run_id + "-ev"})
        s.execute(
            text(
                "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
                "policy_shas, manual, decision_sequence) VALUES (:d,:c,:r,'approve',10,'{}'::jsonb,"
                "'enabled','{}'::jsonb,false,:seq)"
            ),
            {"d": decision_id, "c": case_id, "r": run_id, "seq": seq},
        )
        s.commit()


def test_same_stream_fifo_holds_under_backoff(
    session_factory, settings, publisher, callback_capture, clean_db
):
    """Re-audit F6: strict same-stream FIFO survives retry backoff. One case, decision
    stream: seq1 (older outbox id) sits in FUTURE backoff; seq2 (newer id) is due NOW.
    The claim's inner min(o2.id) head-of-stream subquery deliberately ignores due-time/
    lease eligibility, so the backed-off head BLOCKS its whole stream: process_once() is
    idle (False), ZERO HTTP occurred, and seq2 is untouched (still pending, no claim).
    Once seq1 is due again, delivery order is exactly [seq1, seq2].
    MUTATION WITNESS (named, Step 5c): adding due/lease filtering (e.g.
    `AND o2.next_attempt_at <= now()`) to the INNER `min(o2.id)` subquery of _CLAIM_SQL
    lets seq2 leapfrog its backed-off elder — this test then FAILS."""
    from kyc_tool.outbox.publisher import enqueue_decision_callback

    _seed_decision_chain(session_factory, case_id="cf", run_id="cf-r1", decision_id="cf-d1",
                         seq=1, ev_seq=1)
    _seed_decision_chain(session_factory, case_id="cf", run_id="cf-r2", decision_id="cf-d2",
                         seq=2, ev_seq=2)
    with session_factory() as s:  # enqueue seq1 FIRST (lower outbox.id), then seq2
        enqueue_decision_callback(s, case_id="cf", run_id="cf-r1", body={"run_id": "cf-r1"},
                                  decision_sequence=1)
        s.commit()
    with session_factory() as s:
        enqueue_decision_callback(s, case_id="cf", run_id="cf-r2", body={"run_id": "cf-r2"},
                                  decision_sequence=2)
        s.commit()
    with session_factory() as s:  # push ONLY seq1 into future backoff; seq2 stays due
        s.execute(text("UPDATE outbox SET next_attempt_at = now() + interval '1 hour' "
                       "WHERE case_id='cf' AND decision_sequence=1"))
        s.commit()

    assert publisher.process_once() is False       # idle: the stream head is backed off
    assert callback_capture.requests == []         # ZERO HTTP — seq2 did NOT leapfrog
    with session_factory() as s:
        row2 = s.execute(text("SELECT status, claim_token, claim_lease_expires_at, claimed_by "
                              "FROM outbox WHERE case_id='cf' AND decision_sequence=2")).one()
    assert row2.status == "pending"                # seq2 untouched: pending, never claimed
    assert row2.claim_token is None and row2.claim_lease_expires_at is None
    assert row2.claimed_by is None

    with session_factory() as s:                   # make seq1 due again
        s.execute(text("UPDATE outbox SET next_attempt_at = now() "
                       "WHERE case_id='cf' AND decision_sequence=1"))
        s.commit()
    assert publisher.process_pending() == 2
    assert [r["body"]["run_id"] for r in callback_capture.requests] == ["cf-r1", "cf-r2"]


def test_stale_retry_failure_cannot_touch_reclaimed_row(session_factory, settings, clean_db):
    """Re-audit F7: the fenced NONTERMINAL (retry) failure branch, proven stale-winner/loser.
    outbox_max_attempts=3 so a failure is a RETRY, not dead. A claims; A's lease expires;
    B reclaims (and does NOT terminalize). A's stale _record_failure must leave B's ENTIRE
    claim tuple (claim_token, claim_lease_expires_at, claimed_by), attempts, next_attempt_at,
    and last_error byte-for-byte unchanged, emitting outbox_stale_claim_completion with
    attempted="retry" (the nonterminal branch). Then B's OWN _record_failure increments
    attempts to rowB.attempts+1 (its claim snapshot), schedules backoff per the
    base*2^(attempts-1) formula, and clears ONLY B's claim tuple — status stays pending.
    MUTATION WITNESS (named, Step 5d): removing `claim_token=:token` from ONLY the
    nonterminal retry UPDATE in _record_failure lets stale A rewrite B's attempts/backoff/
    claim — this test then FAILS at the byte-for-byte assertion."""
    _seed_case(session_factory, "c1")
    _enqueue_email(session_factory, case_id="c1", to="keep@x")
    retry_settings = settings.model_copy(
        update={"outbox_max_attempts": 3, "outbox_backoff_base_seconds": 100}
    )
    pub = _pub_for(session_factory, retry_settings)

    rowA = _claim(session_factory, "A")
    assert rowA is not None
    _expire_lease(session_factory, rowA.id)
    rowB = _claim(session_factory, "B")             # B reclaims; does NOT terminalize
    assert rowB is not None and rowB.claim_token != rowA.claim_token

    with session_factory() as s:
        before = s.execute(text("SELECT status, claim_token, claim_lease_expires_at, claimed_by, "
                                "attempts, next_attempt_at, last_error "
                                "FROM outbox WHERE id=:i"), {"i": rowA.id}).one()
    with structlog.testing.capture_logs() as logs:
        pub._record_failure(rowA, "boom", rowA.claim_token)   # stale RETRY-branch loser
    stale = [e for e in logs if e["event"] == "outbox_stale_claim_completion"]
    assert stale and stale[0]["attempted"] == "retry"          # the nonterminal branch ran
    with session_factory() as s:
        after = s.execute(text("SELECT status, claim_token, claim_lease_expires_at, claimed_by, "
                               "attempts, next_attempt_at, last_error "
                               "FROM outbox WHERE id=:i"), {"i": rowA.id}).one()
    assert after == before  # B's ENTIRE claim tuple + attempts + retry clock: untouched

    pub._record_failure(rowB, "boom", rowB.claim_token)        # B's OWN retry failure
    with session_factory() as s:
        final = s.execute(text("SELECT status, claim_token, claim_lease_expires_at, claimed_by, "
                               "attempts FROM outbox WHERE id=:i"), {"i": rowA.id}).one()
        backoff_ok = s.execute(text(
            "SELECT next_attempt_at > now() + interval '50 seconds' "
            "AND next_attempt_at <= now() + interval '150 seconds' "
            "FROM outbox WHERE id=:i"), {"i": rowA.id}).scalar_one()
    assert final.status == "pending"                # nonterminal: NOT dead, NOT delivered
    assert final.attempts == rowB.attempts + 1      # == 1 — relative to B's claim snapshot
    assert final.claim_token is None                # only B's tuple cleared, under B's token
    assert final.claim_lease_expires_at is None and final.claimed_by is None
    assert backoff_ok  # rescheduled per the formula: 100 * 2**0 = 100s into the future


def test_stale_loser_cannot_supersede_reclaimed_row(session_factory, settings, publisher, clean_db):
    """Sibling of the delivered/retry-failure fencing tests, for the SUPERSEDED terminal: same
    A/B ordering. A claims; A's lease expires; B reclaims (and does NOT terminalize). Stale A's
    _record_superseded must be a complete no-op: the outbox row's status/resolved_at/claim tuple
    stay exactly as B left them (still pending, still B's claim), runs.state stays
    PUBLISH_DECISION, decisions.published_at stays NULL, and no audit_log row is written — only
    outbox_stale_claim_completion (attempted="superseded") is logged. Then B alone legitimately
    supersedes: status->superseded, resolved_at set, claim tuple cleared, run reaches COMPLETE,
    and exactly one audit_log row is written.
    MUTATION WITNESS: removing `claim_token=:token` from the leading UPDATE in _record_superseded
    (or its early return on no-match) lets stale A supersede the row out from under B — this test
    then FAILS."""
    from kyc_tool.outbox.publisher import enqueue_decision_callback

    _seed_decision_chain(session_factory, case_id="c1", run_id="r1", decision_id="d1", seq=1, ev_seq=1)
    with session_factory() as s:
        enqueue_decision_callback(s, case_id="c1", run_id="r1", body={"run_id": "r1"},
                                  decision_sequence=1)
        s.commit()

    rowA = _claim(session_factory, "A")
    assert rowA is not None
    _expire_lease(session_factory, rowA.id)
    rowB = _claim(session_factory, "B")  # B reclaims; does NOT terminalize
    assert rowB is not None and rowB.claim_token != rowA.claim_token

    stale_pub = _pub_for(session_factory, settings)
    with session_factory() as s:
        before_ob = s.execute(text("SELECT status, resolved_at, claim_token, claim_lease_expires_at, "
                                   "claimed_by FROM outbox WHERE id=:i"), {"i": rowA.id}).one()
        before_run = s.execute(text("SELECT state FROM runs WHERE id='r1'")).scalar_one()
        before_pub_at = s.execute(text("SELECT published_at FROM decisions WHERE id='d1'")).scalar_one()
        before_audit = s.execute(text("SELECT count(*) FROM audit_log WHERE case_id='c1'")).scalar_one()

    with structlog.testing.capture_logs() as logs:
        stale_pub._record_superseded(rowA, rowA.claim_token)  # stale loser: fenced no-op
    stale = [e for e in logs if e["event"] == "outbox_stale_claim_completion"]
    assert stale and stale[0]["attempted"] == "superseded"

    with session_factory() as s:
        after_ob = s.execute(text("SELECT status, resolved_at, claim_token, claim_lease_expires_at, "
                                  "claimed_by FROM outbox WHERE id=:i"), {"i": rowA.id}).one()
        after_run = s.execute(text("SELECT state FROM runs WHERE id='r1'")).scalar_one()
        after_pub_at = s.execute(text("SELECT published_at FROM decisions WHERE id='d1'")).scalar_one()
        after_audit = s.execute(text("SELECT count(*) FROM audit_log WHERE case_id='c1'")).scalar_one()
    assert after_ob == before_ob  # status, resolved_at, and B's WHOLE claim tuple: untouched
    assert after_ob.status == "pending" and after_ob.claim_token == rowB.claim_token  # still B's
    assert before_run == "PUBLISH_DECISION" and after_run == "PUBLISH_DECISION"
    assert before_pub_at is None and after_pub_at is None
    assert after_audit == before_audit  # no audit_log row written

    publisher._record_superseded(rowB, rowB.claim_token)  # B (the rightful claimant) supersedes
    with session_factory() as s:
        final_ob = s.execute(text("SELECT status, resolved_at, claim_token, claim_lease_expires_at, "
                                  "claimed_by FROM outbox WHERE id=:i"), {"i": rowA.id}).one()
        final_run = s.execute(text("SELECT state FROM runs WHERE id='r1'")).scalar_one()
        final_audit = s.execute(text("SELECT count(*) FROM audit_log WHERE case_id='c1'")).scalar_one()
    assert final_ob.status == "superseded" and final_ob.resolved_at is not None
    assert final_ob.claim_token is None and final_ob.claim_lease_expires_at is None
    assert final_ob.claimed_by is None
    assert final_run == "COMPLETE"
    assert final_audit == before_audit + 1  # exactly one audit_log row written


def test_stale_poc_claimant_sends_no_email(session_factory, settings, clean_db, monkeypatch):
    """Re-audit `cbb783b` F5: the decision-callback path gets its presend check for free (the
    fenced attempt INSERT), but a POC email staged no evidence and so called the provider with no
    ownership check at all. A publisher paused after claiming, whose lease then expires and whose
    row is reclaimed and delivered by B, must make ZERO provider calls when it resumes."""
    from kyc_tool.outbox.publisher import _StaleClaim

    class _CountingSender:
        def __init__(self) -> None:
            self.sent: list[tuple] = []

        def send(self, to, subject, body):
            self.sent.append((to, subject, body))

    _seed_case(session_factory, "c1")
    _enqueue_email(session_factory, case_id="c1", to="keep@x")

    rowA = _claim(session_factory, "A")
    assert rowA is not None
    _expire_lease(session_factory, rowA.id)
    rowB = _claim(session_factory, "B")           # B reclaims under a fresh token
    assert rowB is not None and rowB.claim_token != rowA.claim_token

    senderA = _CountingSender()
    pubA = _pub_for(session_factory, settings)
    pubA.email_sender = senderA
    with pytest.raises(_StaleClaim):              # A resumes: presend gate refuses
        pubA._deliver("poc_email", {"to": "keep@x", "subject": "s", "body": "b"},
                      outbox_id=rowA.id, token=str(rowA.claim_token))
    assert senderA.sent == [], "a stale claimant must not reach the mail provider at all"

    senderB = _CountingSender()                   # B, the real owner, still delivers
    pubB = _pub_for(session_factory, settings)
    pubB.email_sender = senderB
    pubB._deliver("poc_email", {"to": "keep@x", "subject": "s", "body": "b"},
                  outbox_id=rowB.id, token=str(rowB.claim_token))
    assert len(senderB.sent) == 1


def test_configured_lease_reaches_the_database_unclamped(session_factory, settings, clean_db):
    """The claim SQL used `min(lease, 3600)`, so a configured 7200 silently became one hour and
    nothing said so. The bound is declared at the settings layer now; what is configured is what
    the row's lease actually becomes."""
    _seed_case(session_factory, "c1")
    _enqueue_email(session_factory, case_id="c1", to="lease@x")

    leased = settings.model_copy(update={"outbox_lease_seconds": 1800})
    pub = _pub_for(session_factory, leased)
    row = None
    from kyc_tool.outbox.publisher import _CLAIM_SQL
    with session_factory() as s:
        row = s.execute(_CLAIM_SQL, {"lease_seconds": leased.outbox_lease_seconds,
                                     "claimed_by": pub._claimant}).first()
        s.commit()
    assert row is not None
    with session_factory() as s:
        delta = s.execute(text(
            "SELECT EXTRACT(EPOCH FROM (claim_lease_expires_at - now())) FROM outbox WHERE id=:i"),
            {"i": row.id}).scalar_one()
    assert 1700 < float(delta) <= 1800, f"configured lease was not what landed: {delta}"
