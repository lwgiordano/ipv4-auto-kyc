"""PR 7b-core repair (re-audit `1f8412e` F4): `/v1/cases/{id}` and the latest-decision pointer.

`decided_at` is transaction-start `now()`, so two case-locked decides can COMMIT in one order
while their `decided_at` values sit in the other. Migration 013 wrote that fact down and ordered
its backfill by `outbox.id` because of it — while the read route was still selecting gates by
`ORDER BY decided_at DESC`, which pairs the projection's current decision with the PREVIOUS
decision's gates whenever the inversion occurs. The route now reads through
`cases.latest_decision_row_id`, set by a DB trigger in the same transaction as every decision
insert, so what these tests pin is: commit order wins, timestamps never do.
"""

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.postgres


def _insert_chain(conn, case_id, run_id, seq, gates_marker):
    """Full event→run→decision chain on an open connection; decided_at = the CONNECTION's
    transaction-start now()."""
    conn.execute(text(
        "INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, actor_json, "
        "payload_json, event_sequence) VALUES (:e,:c,:k,'h','x','{}'::jsonb,'{}'::jsonb,:s)"),
        {"e": f"{run_id}-ev", "c": case_id, "k": run_id, "s": seq})
    conn.execute(text("INSERT INTO runs (id, case_id, triggering_event_id, state) "
                      "VALUES (:r,:c,:e,'PUBLISH_DECISION')"),
                 {"r": run_id, "c": case_id, "e": f"{run_id}-ev"})
    conn.execute(text(
        "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
        "policy_shas, manual, decision_sequence) VALUES "
        "(:d,:c,:r,'approve',10,CAST(:g AS jsonb),'enabled','{}'::jsonb,false,:s)"),
        {"d": f"{run_id}-d", "c": case_id, "r": run_id, "s": seq,
         "g": f'{{"marker": "{gates_marker}"}}'})


def test_inverted_decided_at_cannot_cross_pair_gates(client, session_factory, clean_db):
    """The two-connection inversion, for real: B fixes its transaction clock EARLY, A commits its
    whole chain (seq 1), THEN B commits seq 2 — so seq 2 is the later commit with the EARLIER
    decided_at. The route must return seq 2's gates. (Mutation witness: restore the old
    `ORDER BY decided_at DESC` read and this returns seq 1's gates — the exact cross-pairing.)"""
    with session_factory() as s:
        s.execute(text("INSERT INTO cases (id, last_decision_sequence) VALUES ('cin', 2)"))
        s.commit()

    engine = session_factory.kw["bind"]
    conn_b = engine.connect()
    try:
        tx_b = conn_b.begin()
        conn_b.execute(text("SELECT now()"))          # B's decided_at is fixed HERE, early
        with engine.begin() as conn_a:                # A starts later → later decided_at...
            _insert_chain(conn_a, "cin", "cin-r1", 1, "one")
        _insert_chain(conn_b, "cin", "cin-r2", 2, "two")
        tx_b.commit()                                 # ...but B is the later COMMIT
    finally:
        conn_b.close()

    with session_factory() as s:
        d1, d2 = s.execute(text(
            "SELECT (SELECT decided_at FROM decisions WHERE id='cin-r1-d'), "
            "(SELECT decided_at FROM decisions WHERE id='cin-r2-d')")).one()
        assert d2 < d1, "the inversion must be real: seq 2 carries the EARLIER decided_at"
        pointer = s.execute(
            text("SELECT latest_decision_row_id FROM cases WHERE id='cin'")).scalar_one()
    assert pointer == "cin-r2-d", "the trigger tracks commit order, not timestamps"

    body = client.get("/v1/cases/cin").json()
    assert body["gates"] == {"marker": "two"}, (
        "gates must come from the pointer row (last commit), never from ORDER BY decided_at"
    )


def test_manual_decision_moves_the_pointer_too(client, session_factory, clean_db):
    """The trigger fires for BOTH decide paths — a manual approval is a decisions INSERT like any
    other, so the pointer (and therefore the served gates) follows it with no app-code call site
    to forget."""
    with session_factory() as s:
        s.execute(text("INSERT INTO cases (id, last_decision_sequence) VALUES ('cman', 1)"))
        s.commit()
    with session_factory.kw["bind"].begin() as conn:
        _insert_chain(conn, "cman", "cman-r1", 1, "auto")
        conn.execute(text(
            "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
            "buy_enablement, policy_shas, manual, reviewer_id) VALUES "
            "('cman-manual','cman',NULL,'approved_manual',10,'{}'::jsonb,'enabled',"
            "'{}'::jsonb,true,'rev-9')"))

    with session_factory() as s:
        pointer = s.execute(
            text("SELECT latest_decision_row_id FROM cases WHERE id='cman'")).scalar_one()
    assert pointer == "cman-manual"
    assert client.get("/v1/cases/cman").json()["gates"] == {}


def test_pointer_rejects_a_decision_of_another_case(session_factory, clean_db):
    """The composite FK makes a cross-case pointer unrepresentable — the exact raw-SQL tamper the
    read path would otherwise serve as truth."""
    with session_factory.kw["bind"].begin() as conn:
        conn.execute(text("INSERT INTO cases (id, last_decision_sequence) VALUES ('cx1', 1)"))
        conn.execute(text("INSERT INTO cases (id, last_decision_sequence) VALUES ('cx2', 0)"))
        _insert_chain(conn, "cx1", "cx1-r1", 1, "x")
    with (
        pytest.raises(Exception, match="fk_cases_latest_decision"),
        session_factory.kw["bind"].begin() as conn,
    ):
        conn.execute(text(
            "UPDATE cases SET latest_decision_row_id = 'cx1-r1-d' WHERE id = 'cx2'"))


def test_null_pointer_with_decisions_returns_empty_gates_not_a_guess(
    client, session_factory, clean_db
):
    """An ambiguous pre-014 history (migration left the pointer NULL) must yield empty gates —
    honest ignorance — rather than a decided_at guess. It heals on the next decision."""
    with session_factory() as s:
        s.execute(text("INSERT INTO cases (id, last_decision_sequence, latest_decision) "
                       "VALUES ('camb', 1, 'approve')"))
        s.commit()
    with session_factory.kw["bind"].begin() as conn:
        _insert_chain(conn, "camb", "camb-r1", 1, "amb")
        # simulate the ambiguous-legacy state 014 leaves behind: pointer cleared
        conn.execute(text("UPDATE cases SET latest_decision_row_id = NULL WHERE id='camb'"))
    body = client.get("/v1/cases/camb").json()
    assert body["gates"] == {}
    assert body["latest_decision"] == "approve"  # the projection itself is untouched
