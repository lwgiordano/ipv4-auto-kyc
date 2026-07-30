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
from sqlalchemy.exc import IntegrityError

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


def _restore_latest_decision_fk(engine):
    with engine.begin() as conn:
        conn.execute(text("UPDATE cases SET latest_decision_row_id = NULL WHERE id = 'drift-a'"))
        conn.execute(text("ALTER TABLE cases DROP CONSTRAINT IF EXISTS fk_cases_latest_decision"))
        conn.execute(text(
            "ALTER TABLE cases ADD CONSTRAINT fk_cases_latest_decision "
            "FOREIGN KEY (latest_decision_row_id, id) REFERENCES decisions(id, case_id)"
        ))


def _restore_latest_manual_fk(engine):
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE cases SET latest_manual_decision_row_id = NULL WHERE id = 'manual-a'")
        )
        conn.execute(text("ALTER TABLE cases DROP CONSTRAINT IF EXISTS fk_cases_latest_manual_decision"))
        conn.execute(text(
            "ALTER TABLE cases ADD CONSTRAINT fk_cases_latest_manual_decision "
            "FOREIGN KEY (latest_manual_decision_row_id, id) REFERENCES decisions(id, case_id)"
        ))
        conn.execute(text("DROP TRIGGER IF EXISTS trg_cases_latest_manual_guard ON cases"))
        conn.execute(text(
            "CREATE TRIGGER trg_cases_latest_manual_guard "
            "BEFORE UPDATE OF latest_manual_decision_row_id ON cases "
            "FOR EACH ROW EXECUTE FUNCTION cases_latest_manual_pointer_guard()"
        ))


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
    # the COMPLETE tuple from the one pointer row — value, gates, decision-time score — so a
    # cross-pair (one row's decision beside another row's gates) is structurally impossible
    assert (body["latest_decision"], body["gates"], body["decision_score"]) == (
        "approve", {"marker": "two"}, 10,
    ), "the whole decision tuple must come from the pointer row (last commit), never decided_at"
    assert body["decision_provenance"] == "latest_decision_row"


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
    body = client.get("/v1/cases/cman").json()
    assert (body["latest_decision"], body["gates"]) == ("approved_manual", {})
    assert body["decision_provenance"] == "latest_decision_row"


def test_pointer_rejects_a_decision_of_another_case(session_factory, clean_db):
    """The composite FK makes a cross-case pointer unrepresentable — the exact raw-SQL tamper the
    read path would otherwise serve as truth."""
    with session_factory.kw["bind"].begin() as conn:
        conn.execute(text("INSERT INTO cases (id, last_decision_sequence) VALUES ('cx1', 1)"))
        conn.execute(text("INSERT INTO cases (id, last_decision_sequence) VALUES ('cx2', 0)"))
        _insert_chain(conn, "cx1", "cx1-r1", 1, "x")
    with (
        pytest.raises(IntegrityError, match="fk_cases_latest_decision"),
        session_factory.kw["bind"].begin() as conn,
    ):
        conn.execute(text(
            "UPDATE cases SET latest_decision_row_id = 'cx1-r1-d' WHERE id = 'cx2'"))


def test_read_surfaces_do_not_dereference_cross_case_pointer_if_fk_drifted(
    client, session_factory, clean_db
):
    """Defense in depth for migration 023: even on a drifted DB, read paths fetch pointer rows
    by `(id, case_id)`, never by id alone."""
    engine = session_factory.kw["bind"]
    try:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE cases DROP CONSTRAINT fk_cases_latest_decision"))
            conn.execute(text(
                "INSERT INTO cases (id, last_decision_sequence) VALUES ('drift-a', 0)"
            ))
            conn.execute(text(
                "INSERT INTO cases (id, last_decision_sequence) VALUES ('drift-b', 1)"
            ))
            _insert_chain(conn, "drift-b", "drift-b-r1", 1, "borrowed")
            conn.execute(
                text(
                    "UPDATE cases SET latest_decision_row_id='drift-b-r1-d' "
                    "WHERE id='drift-a'"
                )
            )

        body = client.get("/v1/cases/drift-a").json()
        assert body["latest_decision"] is None
        assert body["gates"] == {}
        assert body["decision_provenance"] == "no_decisions"

        full = client.get("/ui/api/cases/drift-a/full").json()
        assert full["pointer_decision"] is None
    finally:
        _restore_latest_decision_fk(engine)


def test_full_view_does_not_dereference_cross_case_manual_pointer_if_fk_drifted(
    client, session_factory, clean_db
):
    engine = session_factory.kw["bind"]
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP TRIGGER IF EXISTS trg_cases_latest_manual_guard ON cases"))
            conn.execute(text("ALTER TABLE cases DROP CONSTRAINT fk_cases_latest_manual_decision"))
            conn.execute(text("INSERT INTO cases (id) VALUES ('manual-a')"))
            conn.execute(text("INSERT INTO cases (id) VALUES ('manual-b')"))
            conn.execute(
                text(
                    "INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
                    "actor_json, payload_json, event_sequence) VALUES "
                    "('manual-b-ev','manual-b','manual-b-k','h','x','{}'::jsonb,'{}'::jsonb,1)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
                    "buy_enablement, policy_shas, manual, reviewer_id) VALUES "
                    "('manual-b-d','manual-b',NULL,'approve',10,'{}'::jsonb,'enabled',"
                    "'{}'::jsonb,true,'rev-b')"
                )
            )
            conn.execute(
                text(
                    "UPDATE cases SET latest_manual_decision_row_id='manual-b-d' "
                    "WHERE id='manual-a'"
                )
            )

        full = client.get("/ui/api/cases/manual-a/full").json()
        assert full["latest_manual_decision"] is None
        assert full["manual_decision_provenance"] == "no_manual_decisions"
    finally:
        _restore_latest_manual_fk(engine)


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
    # NO decision tuple is served — and the state is OBSERVABLE, not a silent empty-gates value
    assert body["gates"] == {} and body["latest_decision"] is None
    assert body["decision_provenance"] == "unresolved_legacy_order"


def test_manual_approve_end_to_end_serves_one_row_on_every_surface(
    client, session_factory, post_event, worker, publisher, clean_db
):
    """Re-audit 4dfdf8a F4's real trigger, driven through the REAL paths: worker decide, then a
    real signed reviewer.manual_approve. API, UI full view, and the Salesforce projection must
    all read the SAME manual row — value, gates, reviewer attribution — with no decided_at sort
    anywhere in the chain. (The old behavior returned the stale automatic decision value beside
    the manual row's gates.)"""
    post_event("cme", "kyb.run_requested", {"company_legal_name": "A", "jurisdiction": "GB"})
    worker.run_until_idle()
    publisher.process_pending()
    auto = client.get("/v1/cases/cme").json()
    assert auto["decision_provenance"] == "latest_decision_row"
    assert auto["latest_decision"] == "manual_review_insufficient"  # sanity: the automatic value

    post_event("cme", "reviewer.manual_approve", {"reviewer_id": "rev-1"},
               actor={"type": "reviewer", "id": "rev-1"})

    body = client.get("/v1/cases/cme").json()
    assert body["status"] == "approved_manual"
    assert body["latest_decision"] == "approve"        # the MANUAL row's value...
    assert body["gates"] == {"bypassed": True}         # ...and the SAME row's gates
    assert body["decision_provenance"] == "latest_decision_row"

    full = client.get("/ui/api/cases/cme/full").json()
    assert full["pointer_decision"]["manual"] is True
    assert full["pointer_decision"]["reviewer_id"] == "rev-1"
    assert full["manual_decision_provenance"] == "latest_manual_row"
    sf = full["salesforce"]
    assert sf["Platform_Action_Taken__c"] == "Manual Approve"
    assert sf["Manual_Approved_By__c"] == "rev-1"      # attribution from the pointer row,
    assert sf["Hard_Conflict__c"] is False             # gates from the pointer row too


def test_full_view_disambiguates_missing_manual_from_unresolved_manual_history(
    client, session_factory, clean_db
):
    """A NULL manual pointer used to collapse two different facts: no manual approval exists, or
    legacy/manual order is present but unresolved. The full UI API now tells operators which one
    they are looking at, without guessing by `decided_at` or UUID order."""
    with session_factory() as s:
        s.execute(text("INSERT INTO cases (id) VALUES ('c-no-manual')"))
        s.execute(text("INSERT INTO cases (id) VALUES ('c-manual-legacy')"))
        s.commit()

    no_manual = client.get("/ui/api/cases/c-no-manual/full").json()
    assert no_manual["latest_manual_decision"] is None
    assert no_manual["manual_decision_provenance"] == "no_manual_decisions"

    with session_factory.kw["bind"].begin() as conn:
        conn.execute(text(
            "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
            "buy_enablement, policy_shas, manual, reviewer_id) VALUES "
            "('cml-manual-a','c-manual-legacy',NULL,'approved_manual',10,'{}'::jsonb,"
            "'enabled','{}'::jsonb,true,'rev-a')"))
        conn.execute(text(
            "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
            "buy_enablement, policy_shas, manual, reviewer_id) VALUES "
            "('cml-manual-b','c-manual-legacy',NULL,'rejected_manual',0,'{}'::jsonb,"
            "'locked','{}'::jsonb,true,'rev-b')"))
        # Model a migrated legacy ambiguity: manual rows exist, but the trigger-maintained
        # sticky pointer cannot name one as authoritative.
        conn.execute(text(
            "UPDATE cases SET latest_manual_decision_row_id = NULL "
            "WHERE id='c-manual-legacy'"))

    unresolved = client.get("/ui/api/cases/c-manual-legacy/full").json()
    assert unresolved["latest_manual_decision"] is None
    assert unresolved["manual_decision_provenance"] == "unresolved_legacy_order"


def test_unresolved_ambiguity_metric_counts_only_real_ambiguity(client, session_factory, clean_db):
    """Re-audit 0c46443 F7: the counter must include NULL-pointer-WITH-decisions, exclude
    NULL-pointer-without-decisions, and return to zero when the next decision heals the case."""
    with session_factory() as s:
        s.execute(text("INSERT INTO cases (id, last_decision_sequence) VALUES ('cm1', 1)"))
        s.execute(text("INSERT INTO cases (id) VALUES ('cm2')"))  # no decisions: NOT ambiguous
        s.commit()
    with session_factory.kw["bind"].begin() as conn:
        _insert_chain(conn, "cm1", "cm1-r1", 1, "m")
        conn.execute(text("UPDATE cases SET latest_decision_row_id = NULL WHERE id='cm1'"))
    assert client.get("/v1/metrics").json()["cases_with_unresolved_decision_order"] == 1

    with session_factory.kw["bind"].begin() as conn:  # the next decision heals the pointer
        conn.execute(text(
            "INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
            "actor_json, payload_json, event_sequence) VALUES "
            "('cm1-h-ev','cm1','cm1-h','h','x','{}'::jsonb,'{}'::jsonb,2)"))
        conn.execute(text("INSERT INTO runs (id, case_id, triggering_event_id, state) "
                          "VALUES ('cm1-h','cm1','cm1-h-ev','PUBLISH_DECISION')"))
        conn.execute(text(
            "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
            "buy_enablement, policy_shas, manual, decision_sequence) VALUES "
            "('cm1-h-d','cm1','cm1-h','approve',10,'{}'::jsonb,'enabled','{}'::jsonb,false,2)"))
    assert client.get("/v1/metrics").json()["cases_with_unresolved_decision_order"] == 0


def test_list_endpoint_serves_the_pointed_decision(client, session_factory, clean_db):
    """Re-audit 0c46443 F6: the cases LIST also reads through the pointer — after the raw manual
    row moves it, the list shows the manual verdict; with the pointer unresolved it shows NULL,
    never the stale projection column."""
    with session_factory() as s:
        s.execute(text("INSERT INTO cases (id, last_decision_sequence, latest_decision) "
                       "VALUES ('cl1', 1, 'reject')"))
        s.commit()
    with session_factory.kw["bind"].begin() as conn:
        _insert_chain(conn, "cl1", "cl1-r1", 1, "l")
        conn.execute(text(
            "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
            "buy_enablement, policy_shas, manual, reviewer_id) VALUES "
            "('cl1-manual','cl1',NULL,'approve',10,'{}'::jsonb,'enabled','{}'::jsonb,true,'r1')"))
    rows = {c["id"]: c for c in client.get("/ui/api/cases").json()["cases"]}
    assert rows["cl1"]["latest_decision"] == "approve"      # the POINTED (manual) row

    with session_factory.kw["bind"].begin() as conn:        # unresolved → NULL, not 'reject'
        conn.execute(text("UPDATE cases SET latest_decision_row_id = NULL WHERE id='cl1'"))
    rows = {c["id"]: c for c in client.get("/ui/api/cases").json()["cases"]}
    assert rows["cl1"]["latest_decision"] is None


def test_salesforce_action_follows_the_pointer_not_the_case_column(
    client, session_factory, clean_db
):
    """Re-audit 0c46443 F6: with the pointer unresolved, the Salesforce projection must emit NO
    action rather than mapping the stale cases.latest_decision column."""
    with session_factory() as s:
        s.execute(text("INSERT INTO cases (id, last_decision_sequence, latest_decision) "
                       "VALUES ('cs1', 1, 'approve')"))
        s.commit()
    with session_factory.kw["bind"].begin() as conn:
        _insert_chain(conn, "cs1", "cs1-r1", 1, "s")
        conn.execute(text("UPDATE cases SET latest_decision_row_id = NULL WHERE id='cs1'"))
    full = client.get("/ui/api/cases/cs1/full").json()
    assert full["pointer_decision"] is None
    assert full["salesforce"]["Platform_Action_Taken__c"] is None  # honest: order unresolved
