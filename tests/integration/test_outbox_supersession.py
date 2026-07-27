"""PR 7b-core: best-effort local superseded guard + A6 + honest residual risk."""

import json

import pytest
from sqlalchemy import text

from kyc_tool.outbox.publisher import enqueue_decision_callback

pytestmark = pytest.mark.postgres


def _seed_decisions(session_factory, case_id, seqs):
    """Seed case + one automatic decision (+ run at PUBLISH_DECISION) per seq in `seqs`;
    published_at stays NULL. Run id = f'{case_id}-r{seq}', decision id = f'{case_id}-r{seq}-d'."""
    with session_factory() as s:
        s.execute(text("INSERT INTO cases (id, last_decision_sequence) VALUES (:c,:m) "
                       "ON CONFLICT (id) DO UPDATE SET last_decision_sequence=:m"),
                  {"c": case_id, "m": max(seqs)})
        for seq in seqs:
            r = f"{case_id}-r{seq}"
            s.execute(text("INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
                           "actor_json, payload_json, event_sequence) VALUES (:e,:c,:k,'h','x','{}'::jsonb,"
                           "'{}'::jsonb,:s)"), {"e": r + "-ev", "c": case_id, "k": r, "s": seq})
            s.execute(text("INSERT INTO runs (id, case_id, triggering_event_id, state) "
                           "VALUES (:r,:c,:e,'PUBLISH_DECISION')"), {"r": r, "c": case_id, "e": r + "-ev"})
            s.execute(text("INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
                           "buy_enablement, policy_shas, manual, "
                           "decision_sequence) VALUES (:d,:c,:r,'approve',"
                           "10,'{}'::jsonb,'enabled','{}'::jsonb,false,:seq)"),
                      {"d": r + "-d", "c": case_id, "r": r, "seq": seq})
        s.commit()


def _enqueue_cb(session_factory, case_id, seq):
    r = f"{case_id}-r{seq}"
    with session_factory() as s:
        enqueue_decision_callback(s, case_id=case_id, run_id=r, body={"run_id": r}, decision_sequence=seq)
        s.commit()


def test_higher_delivered_then_older_superseded_a6(
    session_factory, settings, publisher, callback_capture, clean_db
):
    """A6 (F6) + local guard: the HIGHER callback (seq 2) is really SENT (>=1 HTTP) and stamps
    published_at; the OLDER requeued callback (seq 1) is then claimed, SUPERSEDED with ZERO HTTP,
    its run reaches COMPLETE, published_at stays NULL, and an audit_log row records BOTH the
    superseded and the superseding sequence."""
    _seed_decisions(session_factory, "c1", [1, 2])
    _enqueue_cb(session_factory, "c1", 2)
    assert publisher.process_pending() == 1        # seq 2 delivered (HTTP #1) + stamped
    assert len(callback_capture.requests) == 1
    _enqueue_cb(session_factory, "c1", 1)          # the older requeued callback
    assert publisher.process_pending() == 1        # seq 1 claimed → guard fires → superseded
    assert len(callback_capture.requests) == 1     # ZERO additional HTTP for the lower callback

    with session_factory() as s:
        row = s.execute(text("SELECT status, resolved_at, delivered_at "
                             "FROM outbox WHERE run_id='c1-r1'")).one()
        run = s.execute(text("SELECT state FROM runs WHERE id='c1-r1'")).scalar_one()
        pub_at = s.execute(text("SELECT published_at FROM decisions WHERE id='c1-r1-d'")).scalar_one()
        aud = s.execute(text("SELECT detail_json FROM audit_log WHERE action='outbox.superseded'")).fetchall()
    assert row.status == "superseded" and row.resolved_at is not None and row.delivered_at is None
    assert run == "COMPLETE" and pub_at is None
    assert aud and aud[0].detail_json["superseded_sequence"] == 1
    assert aud[0].detail_json["superseding_sequence"] == 2  # both sequences recorded (durable audit)


def test_normal_1_2_3_all_delivered_in_order(session_factory, settings, publisher, callback_capture):
    """seq 1→2→3 each delivered before the next → none superseded (the guard suppresses only
    a still-pending OLDER callback once a higher delivery has stamped)."""
    _seed_decisions(session_factory, "cn", [1, 2, 3])
    for seq in (1, 2, 3):
        _enqueue_cb(session_factory, "cn", seq)
    assert publisher.process_pending() == 3
    assert len(callback_capture.requests) == 3
    with session_factory() as s:
        statuses = [r.status for r in s.execute(text(
            "SELECT status FROM outbox WHERE case_id='cn' ORDER BY decision_sequence"))]
    assert statuses == ["delivered", "delivered", "delivered"]


class _InjectedFault(RuntimeError):
    pass


def test_residual_risk_send_before_stamp_reverts_expected(
    client, session_factory, settings, publisher, callback_capture, monkeypatch
):
    """Honest boundary (§5) via the REACHABLE lifecycle: seq 1 is an OLDER dead callback (lower
    outbox id); seq 2 is pending. The publisher SENDS seq 2 (HTTP #1, 2xx) but a fault injected
    in `_record_delivered` BEFORE its transaction leaves seq 2 pending/claimed and its decision
    UNSTAMPED (the real HTTP→terminal gap). seq 1 is requeued through the REAL UI endpoint; on the
    next pass its local guard predicate is false (seq 2 unstamped) and seq 1 IS sent (HTTP #2) —
    the exact residual revert 7b-activation later turns into a platform high-water no-op."""
    _seed_decisions(session_factory, "c3", [1, 2])  # decisions/runs at PUBLISH_DECISION, unstamped
    with session_factory() as s:  # seq 1 callback as an OLDER dead row (lower id)
        seq1_id = s.execute(text(
            "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, status) "
            "VALUES ('decision_callback','c3','c3-r1','decision',1,'dead') RETURNING id")).scalar_one()
        s.commit()
    _enqueue_cb(session_factory, "c3", 2)  # seq 2 pending, higher id

    def _boom(self, row, token, wire_sha256=None):  # raise BEFORE any terminal txn starts
        # signature mirrors _record_delivered, which now also carries the sent-bytes digest;
        # the fault must land AFTER the HTTP and BEFORE the terminal, which is the whole point
        raise _InjectedFault("send-before-stamp: HTTP sent, terminal not committed")

    monkeypatch.setattr(type(publisher), "_record_delivered", _boom)
    with pytest.raises(_InjectedFault):
        publisher.process_once()  # claims seq 2 → _deliver (HTTP #1) → _boom
    assert len(callback_capture.requests) == 1  # seq 2 was really SENT
    with session_factory() as s:
        assert s.execute(text("SELECT status FROM outbox WHERE run_id='c3-r2'")).scalar_one() == "pending"
        assert s.execute(text("SELECT published_at FROM decisions WHERE id='c3-r2-d'")).scalar_one() is None

    monkeypatch.undo()  # restore the real _record_delivered
    resp = client.post(f"/ui/api/requeue/outbox/{seq1_id}")  # REAL UI requeue (dead → pending)
    assert resp.status_code == 200

    assert publisher.process_pending() >= 1     # seq 1 (lower id) claimed; guard predicate false
    assert len(callback_capture.requests) == 2  # HTTP #2 — the documented, expected revert
    with session_factory() as s:
        assert s.execute(text("SELECT status FROM outbox WHERE run_id='c3-r1'")).scalar_one() == "delivered"


def test_guard_predicate_only_unit(session_factory, settings, publisher, callback_capture):
    """Named unit companion: with a higher decision UNSTAMPED (published_at NULL), the guard
    predicate is false, so an older callback is sent — isolates the predicate without the HTTP
    gap (kept separate from the lifecycle proof above, not a substitute)."""
    _seed_decisions(session_factory, "cu", [1, 2])  # both unstamped
    _enqueue_cb(session_factory, "cu", 1)
    assert publisher.process_pending() == 1
    assert len(callback_capture.requests) == 1  # seq 1 sent (no higher stamped delivery)


def _superseded_callback(session_factory, case_id, *, resolved_at="now()"):
    """Re-audit F8: 'superseded' is decision-only (lifecycle CHECK kind conjunct), so every
    fixture that needs a superseded row must build a VALID automatic decision+callback chain
    first — seq 1 on `case_id` via _seed_decisions — then insert its callback already
    superseded. Returns the outbox id."""
    _seed_decisions(session_factory, case_id, [1])
    r = f"{case_id}-r1"
    with session_factory() as s:
        oid = s.execute(
            text(
                "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, "
                "status, resolved_at) VALUES ('decision_callback',:c,:r,'decision',1,"
                f"'superseded', {resolved_at}) RETURNING id"
            ),
            {"c": case_id, "r": r},
        ).scalar_one()
        s.commit()
    return oid


def test_retention_redacts_callback_bodies_and_prunes_poc_email(session_factory, clean_db):
    """PR 7b-core durable ordering authority (re-review 6a408a3 F5/F1): retention keeps the
    decision_callback ROW but destroys its BODY past the window, deletes the poc_email outright,
    and leaves every field 7b-activation reconciles against intact — including the recorded wire
    digest, which is what makes discarding the body safe."""
    from kyc_tool.workers.retention import prune

    old = "now() - interval '3000 days'"
    _superseded_callback(session_factory, "c4", resolved_at=old)
    _seed_decisions(session_factory, "c4", [9])
    with session_factory() as s:
        s.execute(text("INSERT INTO cases (id) VALUES ('c4') ON CONFLICT DO NOTHING"))
        s.execute(text(
            f"INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, "
            f"status, delivered_at, payload_json, callback_wire_sha256, wire_version) VALUES "
            f"('decision_callback','c4','c4-r9','decision',9,'delivered',{old},"
            f"'{{\"decision\":\"approve\",\"checks\":[{{\"source\":\"reviewer:r-42\"}}]}}'::jsonb,"
            f"'{'a' * 64}','legacy')"))
        s.execute(text(
            f"INSERT INTO outbox (kind, case_id, ordering_stream, status, delivered_at) "
            f"VALUES ('poc_email','c4','email','delivered',{old})"))
        s.commit()

    counts = prune(session_factory, 7 * 365)

    assert counts["outbox_poc_email"] == 1          # the email row is DELETED
    assert counts["outbox_callback_redacted"] == 2  # delivered + superseded callbacks redacted
    with session_factory() as s:
        rows = s.execute(text(
            "SELECT status, run_id, decision_sequence, delivered_at, payload_json, "
            "callback_wire_sha256, wire_version FROM outbox WHERE case_id='c4' ORDER BY id")).all()
        assert [r.status for r in rows] == ["superseded", "delivered"]  # both rows survive
        assert s.execute(text(
            "SELECT count(*) FROM outbox WHERE case_id='c4' AND kind='poc_email'")).scalar_one() == 0
        delivered = rows[1]
        # every manifest field survives...
        assert delivered.run_id == "c4-r9" and delivered.decision_sequence == 9
        assert delivered.delivered_at is not None          # the ORIGINAL timestamp, not now()
        assert delivered.callback_wire_sha256 == "a" * 64  # the recorded witness, untouched
        assert delivered.wire_version == "legacy"
        # ...but the reviewer-bearing body is GONE
        assert delivered.payload_json == {"redacted": True}
        assert "reviewer:r-42" not in str(rows)


def test_retention_leaves_in_window_callback_bodies_alone(session_factory, clean_db):
    """Redaction is bounded by the window: a RECENT delivered callback keeps its body, so the
    prune cannot be mistaken for an unconditional scrub."""
    from kyc_tool.workers.retention import prune

    _seed_decisions(session_factory, "c4c", [3])
    with session_factory() as s:
        s.execute(text("INSERT INTO cases (id) VALUES ('c4c') ON CONFLICT DO NOTHING"))
        s.execute(text(
            "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, "
            "status, delivered_at, payload_json) VALUES ('decision_callback','c4c','c4c-r3',"
            "'decision',3,'delivered', now(), '{\"decision\":\"approve\"}'::jsonb)"))
        s.commit()
    counts = prune(session_factory, 7 * 365)
    assert counts["outbox_callback_redacted"] == 0
    with session_factory() as s:
        body = s.execute(text(
            "SELECT payload_json FROM outbox WHERE case_id='c4c'")).scalar_one()
    assert body == {"decision": "approve"}


def test_retention_still_prunes_its_other_targets(session_factory, clean_db):
    """The narrowing is scoped to the outbox: audit_log and poc_tokens pruning is unchanged."""
    from kyc_tool.workers.retention import prune

    with session_factory() as s:
        s.execute(text("INSERT INTO cases (id) VALUES ('c4b') ON CONFLICT DO NOTHING"))
        s.execute(text(
            "INSERT INTO audit_log (case_id, action, actor, detail_json, at) "
            "VALUES ('c4b','x','system','{}'::jsonb, now() - interval '3000 days')"))
        s.commit()
    counts = prune(session_factory, 7 * 365)
    assert counts["audit_log"] == 1


def test_ui_requeue_409s_superseded(client, session_factory):
    # conftest `settings` leaves ui_admin_token empty → the console is open in tests
    # (dev/test trust model; see tests/unit/test_ops_auth.py + tests/integration/test_ui.py),
    # so no auth header is sent and a non-dead row must 409 (never requeued).
    oid = _superseded_callback(session_factory, "c5")
    resp = client.post(f"/ui/api/requeue/outbox/{oid}")
    assert resp.status_code == 409  # superseded is not 'dead' → requeue_outbox rejects it


def test_metrics_reports_superseded_out_of_the_alert_set(client, session_factory):
    """superseded is counted as terminal history, never in the pending/dead alert set — and the
    endpoint stays bounded: PR 7b-core never prunes decision callbacks, so `outbox` grows without
    limit and a GROUP BY over the whole table would make this endpoint's cost grow with it."""
    _superseded_callback(session_factory, "cm")  # a VALID superseded decision callback (F8)
    with session_factory() as s:
        s.execute(text("INSERT INTO outbox (kind, case_id, ordering_stream, "
                       "status) VALUES ('poc_email','cm','email','pending')"))
        s.execute(text("INSERT INTO outbox (kind, case_id, ordering_stream, "
                       "status) VALUES ('poc_email','cm','email','dead')"))
        s.commit()
    body = client.get("/v1/metrics").json()
    # live statuses reported exactly; terminals are NOT enumerated per-status
    assert {"pending", "dead"} <= set(body["outbox_by_status"])
    assert "superseded" not in body["outbox_by_status"]
    assert "delivered" not in body["outbox_by_status"]
    assert body["outbox_terminal_total"] >= 1          # the superseded row is counted here
    assert "superseded" not in body["outbox_alerting"]  # governed terminal, not an alert


def test_manual_current_then_late_automatic_callback_sent_expected_pre_activation(
    client, session_factory, post_event, worker, publisher, callback_capture, sign
):
    """Re-audit F4 — the THIRD honest residual, real end-to-end: ingest → worker drives an
    automatic decision whose callback is ENQUEUED but NOT yet processed; a reviewer manual
    approval then becomes the case's current state (manual rows: run_id NULL, no callback,
    no decision_sequence); the publisher then runs. The local guard sees NO higher
    locally-published automatic sequence (manual allocates none), so the OLD automatic
    callback IS sent AFTER the manual approval — EXPECTED pre-activation behavior, closed
    only by 7b-activation's platform high-water (the strengthened 014 acceptance makes an
    unaccepted older callback a sticky no-op against a manual-current source)."""
    post_event("case-mc", "kyb.run_requested", {"company_legal_name": "A", "jurisdiction": "GB"})
    worker.run_until_idle()          # automatic decision made; callback enqueued, NOT processed
    with session_factory() as s:
        queued = s.execute(text("SELECT status, run_id FROM outbox "
                                "WHERE case_id='case-mc' AND kind='decision_callback'")).one()
    assert queued.status == "pending"  # the automatic callback is still queued

    body = json.dumps({
        "event_type": "reviewer.manual_approve",
        "occurred_at": "2026-07-24T00:00:00Z",
        "actor": {"type": "reviewer", "id": "rev-1"},
        "payload": {"reviewer_id": "rev-1", "note": "manual current"},
    }).encode()
    resp = client.post("/v1/cases/case-mc/events", content=body, headers=sign(body))
    assert resp.status_code == 200   # manual approval recorded — the case's CURRENT state

    assert publisher.process_pending() == 1          # the old automatic callback is claimed
    assert len(callback_capture.requests) == 1       # ... and the HTTP send REALLY happened
    assert callback_capture.requests[0]["body"]["run_id"] == queued.run_id
    with session_factory() as s:
        status = s.execute(text("SELECT status FROM outbox WHERE case_id='case-mc' "
                                "AND kind='decision_callback'")).scalar_one()
    assert status == "delivered"     # delivered AFTER manual approval — the documented residual
