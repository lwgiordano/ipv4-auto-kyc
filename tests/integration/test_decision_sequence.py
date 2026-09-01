"""PR 7b-core: per-case decision_sequence allocation under the Case FOR UPDATE lock."""

import json
import threading

import pytest
from sqlalchemy import text

from kyc_tool.domain.models import RunState
from kyc_tool.queue.jobs import ClaimedJob

pytestmark = pytest.mark.postgres


def _seed_run_at_decide(session_factory, *, case_id, run_id, ev_seq):
    """Seed a case + one run poised at DECIDE (the broker-blocked short-circuit path decides
    from DECIDE) + its running run_transition job. Returns the job id."""
    with session_factory() as s:
        s.execute(text("INSERT INTO cases (id) VALUES (:c) ON CONFLICT DO NOTHING"), {"c": case_id})
        s.execute(text("INSERT INTO events (id, case_id, idempotency_key, "
                       "payload_hash, event_type, actor_json, "
                       "payload_json, event_sequence) VALUES (:e,:c,:k,'h','kyb.run_requested','{}'::jsonb,"
                       "CAST(:p AS jsonb),:sq)"),
                  {"e": run_id + "-ev", "c": case_id, "k": run_id,
                   "p": '{"company_legal_name":"X","jurisdiction":"GB"}', "sq": ev_seq})
        s.execute(text("INSERT INTO runs (id, case_id, triggering_event_id, state, input_snapshot_json) "
                       "VALUES (:r,:c,:e,'DECIDE', CAST(:p AS jsonb))"),
                  {"r": run_id, "c": case_id, "e": run_id + "-ev",
                   "p": '{"company_legal_name":"X","jurisdiction":"GB"}'})
        # the decide txn's jobs.complete() fences on the claim NONCE (R8 F1) — seed a live one
        jid = s.execute(text("INSERT INTO jobs (kind, case_id, payload_json, status, locked_by) "
                             "VALUES ('run_transition',:c, CAST(:p AS jsonb), 'running', :n) "
                             "RETURNING id"),
                        {"c": case_id, "p": json.dumps({"run_id": run_id}),
                         "n": f"worker-test:{run_id}"}).scalar_one()
        s.commit()
    return jid


def test_concurrent_decides_serialize_via_case_lock(session_factory, pipeline, policy, monkeypatch):
    """Deterministic lock-contention proof (F3). A `_load` wrapper keyed by run id: A takes the
    Case FOR UPDATE (real `_load`), signals `a_has_case_lock` AFTER it returns, and is HELD before
    allocation/commit; B signals `b_entered_load` before its `_load` and `b_returned_from_load`
    only AFTER it returns. With the real FOR UPDATE, B must block INSIDE `_load` while A holds —
    so `b_returned_from_load` stays unset. Releasing A → A=1, B=2, counter=2, two bound callbacks.
    MUTATION: removing `with_for_update=True` (or substituting `max()+1`) lets B return while A is
    held → `assert not b_returned_from_load.wait(2)` fails deterministically, regardless of commit
    order. Both threads must terminate (a timeout must not masquerade as success)."""
    jid_a = _seed_run_at_decide(session_factory, case_id="cc", run_id="ra", ev_seq=1)
    jid_b = _seed_run_at_decide(session_factory, case_id="cc", run_id="rb", ev_seq=2)

    a_has_case_lock = threading.Event()
    release_a = threading.Event()
    b_entered_load = threading.Event()
    b_returned_from_load = threading.Event()
    orig_load = pipeline._load

    def wrapped_load(session, run_id):
        if run_id == "ra":
            result = orig_load(session, run_id)   # A acquires the Case FOR UPDATE here
            a_has_case_lock.set()
            release_a.wait(timeout=20)            # hold A (with the lock) before allocation/commit
            return result
        b_entered_load.set()
        result = orig_load(session, run_id)       # B blocks here on the FOR UPDATE while A holds
        b_returned_from_load.set()
        return result

    monkeypatch.setattr(pipeline, "_load", wrapped_load)
    errors: list[Exception] = []

    def decide(run_id, jid):
        try:
            claimed = ClaimedJob(id=jid, kind="run_transition", case_id="cc",
                                 payload={"run_id": run_id}, attempts=0, max_attempts=5,
                                 claim_nonce=f"worker-test:{run_id}")
            pipeline._decide_txn(run_id, from_state=RunState.DECIDE, job=claimed, bundle=policy)
        except Exception as e:  # noqa: BLE001 — capture a uniqueness race for the assertion
            errors.append(e)

    # Every assertion below runs while thread A is parked inside `release_a.wait()` STILL HOLDING
    # the Case row lock. If one fails outside a finally, A is never released and never joined: it
    # sits on that lock for the full 20s wait, and every later test touching case 'cc' blocks
    # behind it — so a single logical failure here would surface as unrelated timeouts elsewhere.
    # The mutation witness (`not b_returned_from_load.wait`) is exactly the assertion designed to
    # fail, which makes unconditional release load-bearing rather than defensive.
    threads: list[threading.Thread] = []
    try:
        ta = threading.Thread(target=decide, args=("ra", jid_a), name="decide-A")
        threads.append(ta)
        ta.start()
        assert a_has_case_lock.wait(timeout=15), "A never acquired the case lock"
        tb = threading.Thread(target=decide, args=("rb", jid_b), name="decide-B")
        threads.append(tb)
        tb.start()
        assert b_entered_load.wait(timeout=15), "B never entered _load"
        assert not b_returned_from_load.wait(timeout=2), (
            "B returned from _load while A held the case lock — the FOR UPDATE is not serializing"
        )
    finally:
        release_a.set()
        for t in threads:
            t.join(timeout=20)
    assert not [t.name for t in threads if t.is_alive()], (
        f"worker thread(s) still running after join: {[t.name for t in threads if t.is_alive()]}"
    )

    assert errors == []  # no uq_decisions_case_decision_sequence violation
    with session_factory() as s:
        by_run = {r.run_id: r.decision_sequence for r in s.execute(text(
            "SELECT run_id, decision_sequence FROM decisions WHERE case_id='cc' AND manual=false"))}
        counter = s.execute(text("SELECT last_decision_sequence FROM cases WHERE id='cc'")).scalar_one()
        cbs = sorted(r.decision_sequence for r in s.execute(text(
            "SELECT decision_sequence FROM outbox WHERE case_id='cc' AND kind='decision_callback'")))
    assert by_run == {"ra": 1, "rb": 2}  # A (first under the lock) = 1, B = 2
    assert counter == 2 and cbs == [1, 2]


def test_manual_approve_allocates_no_sequence(client, session_factory, post_event, worker, sign):
    post_event("case-man", "kyb.run_requested", {"company_legal_name": "A", "jurisdiction": "GB"})
    worker.run_until_idle()
    # ReviewerManualApprovePayload requires reviewer_id; review_guard requires actor.id == reviewer_id.
    body = json.dumps({
        "event_type": "reviewer.manual_approve",
        "occurred_at": "2026-07-23T00:00:00Z",
        "actor": {"type": "reviewer", "id": "rev-1"},
        "payload": {"reviewer_id": "rev-1", "note": "ok"},
    }).encode()
    resp = client.post("/v1/cases/case-man/events", content=body, headers=sign(body))
    assert resp.status_code == 200  # accepted (no 422)

    with session_factory() as s:
        manual = s.execute(text("SELECT decision_sequence, run_id FROM decisions "
                                "WHERE case_id='case-man' AND manual=true")).one()
        counter = s.execute(text("SELECT last_decision_sequence FROM cases WHERE id='case-man'")).scalar_one()
    assert manual.decision_sequence is None and manual.run_id is None  # manual allocates nothing
    assert counter == 1  # only the one automatic decide bumped it; manual approve did not


def test_enqueue_decision_callback_requires_sequence_kwarg():
    """Re-audit F10: the FINAL 013 signature makes decision_sequence a REQUIRED keyword —
    omitting it fails at the Python boundary (TypeError), not late at commit on the
    ck_outbox_kind_stream_identity CHECK. (TypeError is raised at bind time, before the
    session argument is ever touched.)"""
    from kyc_tool.outbox.publisher import enqueue_decision_callback

    with pytest.raises(TypeError, match="decision_sequence"):
        enqueue_decision_callback(None, body={})
