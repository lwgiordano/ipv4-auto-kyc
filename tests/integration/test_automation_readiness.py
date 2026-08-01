"""Shadow-mode automation-readiness gauge on `/v1/metrics` — the read-only "measure before you
enforce" surface. It reports the engine's automatic-decision mix, the human-approval count, the
population that WOULD auto-approve under enforcement (the set to sample-review), and the one
engine-vs-human disagreement this system records (a human approving what the engine held). It
enforces nothing; it only counts decisions the engine already made.
"""

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.postgres


def _seed_case(conn, case_id: str) -> None:
    conn.execute(text("INSERT INTO cases (id) VALUES (:c)"), {"c": case_id})


def _auto_decision(conn, case_id: str, run_id: str, seq: int, decision: str) -> None:
    """A full event→run→AUTOMATIC decision (manual=false) chain."""
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
        "(:d,:c,:r,:dec,10,'{}'::jsonb,'enabled','{}'::jsonb,false,:s)"),
        {"d": f"{run_id}-d", "c": case_id, "r": run_id, "dec": decision, "s": seq})


def _manual_approval(conn, case_id: str, tag: str) -> None:
    """A human manual_approve decision. Per ck_decisions_manual_sequence a manual row MUST carry
    run_id IS NULL AND decision_sequence IS NULL (manual attribution is sticky, not sequenced)."""
    conn.execute(text(
        "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
        "policy_shas, manual, decision_sequence) VALUES "
        "(:d,:c,NULL,'approve',0,'{}'::jsonb,'enabled','{}'::jsonb,true,NULL)"),
        {"d": f"{case_id}-m{tag}", "c": case_id})


def test_automation_readiness_reports_distribution_population_and_agreement(engine, client, clean_db):
    with engine.begin() as conn:
        # A: engine auto-approved, no human — the auto-approve population (sample this for the
        #    false-approve rate; the code cannot compute it).
        _seed_case(conn, "ar-a")
        _auto_decision(conn, "ar-a", "ar-a-r1", 1, "approve")
        # B: engine HELD (manual_review) and a human later approved — measurable disagreement.
        _seed_case(conn, "ar-b")
        _auto_decision(conn, "ar-b", "ar-b-r1", 1, "manual_review_insufficient")
        _manual_approval(conn, "ar-b", "1")
        # C: engine HELD, no human approval yet.
        _seed_case(conn, "ar-c")
        _auto_decision(conn, "ar-c", "ar-c-r1", 1, "manual_review_insufficient")

    ar = client.get("/v1/metrics").json()["automation_readiness"]

    assert ar["automatic_decisions_by_type"] == {"approve": 1, "manual_review_insufficient": 2}
    assert ar["manual_approvals_total"] == 1
    assert ar["cases_would_auto_approve"] == 1          # A — the population to sample-review
    assert ar["cases_engine_held"] == 2                 # B, C — engine did not auto-approve
    assert ar["human_approved_after_engine_held"] == 1  # B — human stepped in over an engine hold


def test_latest_automatic_decision_wins_over_an_earlier_one(engine, client, clean_db):
    """The population view is per-case on the LATEST automatic decision: a case that first held and
    later auto-approved counts as would-auto-approve, not held (manual rows never shadow it)."""
    with engine.begin() as conn:
        _seed_case(conn, "ar-d")
        _auto_decision(conn, "ar-d", "ar-d-r1", 1, "manual_review_insufficient")
        _auto_decision(conn, "ar-d", "ar-d-r2", 2, "approve")  # later automatic decision

    ar = client.get("/v1/metrics").json()["automation_readiness"]
    assert ar["cases_would_auto_approve"] == 1
    assert ar["cases_engine_held"] == 0
