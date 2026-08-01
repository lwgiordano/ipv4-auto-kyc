"""Shadow-mode automation-readiness gauge on `/v1/metrics` — the read-only, NON-GATING "measure
before you enforce" surface. Every count is the engine's COMPUTED decision, reconstructed from
immutable stamped facts (five gates + buy_enablement), because with the enforcement kill switch OFF
(the production default) the pipeline stores a computed positive as a HOLD — reading the stored
label would count the overlay, not the engine, and the "would auto-approve" population would read
~zero in production (re-audit `b39b82a..b53daf4` F1). The HTTP route is read-auth gated (F4).
"""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from kyc_tool.api.app import create_app
from kyc_tool.checkstore import repo as checkstore
from kyc_tool.domain.models import CheckStatus
from kyc_tool.orchestration.pipeline import Pipeline
from kyc_tool.queue.worker import Worker
from kyc_tool.storage.object_store import FsStore
from tests.conftest import sign_headers_v2

pytestmark = pytest.mark.postgres

# Production-shaped gate stamps. A computed positive is held (label flips to manual_review) but the
# five gates stay all-true; a genuine insufficient-evidence hold has a failing gate.
_ALL_PASS = {
    "score_met": True,
    "legal_proof": True,
    "control_proof": True,
    "broker_ok": True,
    "no_hard_conflict": True,
}
_NOT_ALL_PASS = {**_ALL_PASS, "score_met": False}


def _seed_case(conn, case_id: str) -> None:
    conn.execute(text("INSERT INTO cases (id) VALUES (:c)"), {"c": case_id})


def _auto_decision(
    conn, case_id: str, run_id: str, seq: int, decision: str, *, gates=_ALL_PASS, buy="enabled"
) -> None:
    """A full event→run→AUTOMATIC decision (manual=false) chain, production-SHAPED: gates_json and
    buy_enablement carry what the pipeline actually stamps, so the gauge's computed-decision
    reconstruction is exercised rather than bypassed with empty gates (the gap that hid F1)."""
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
        "(:d,:c,:r,:dec,100,CAST(:g AS jsonb),:buy,'{}'::jsonb,false,:s)"),
        {"d": f"{run_id}-d", "c": case_id, "r": run_id, "dec": decision,
         "g": json.dumps(gates), "buy": buy, "s": seq})


def _manual_approval(conn, case_id: str, tag: str) -> None:
    """A human manual_approve decision. Per ck_decisions_manual_sequence a manual row MUST carry
    run_id IS NULL AND decision_sequence IS NULL (manual attribution is sticky, not sequenced)."""
    conn.execute(text(
        "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
        "policy_shas, manual, decision_sequence) VALUES "
        "(:d,:c,NULL,'approve',0,'{}'::jsonb,'enabled','{}'::jsonb,true,NULL)"),
        {"d": f"{case_id}-m{tag}", "c": case_id})


def test_gauge_counts_the_computed_decision_not_the_enforcement_held_label(engine, client, clean_db):
    """F1 (query level): with enforcement off the pipeline stores computed positives as holds with
    all-pass gates. The gauge must reconstruct the COMPUTED decision from those gates — counting the
    stored label would read zero would-auto-approve in production, the exact population to sample."""
    with engine.begin() as conn:
        # Held computed approve (all gates pass, org-id passed → buy enabled).
        _seed_case(conn, "cd-a")
        _auto_decision(conn, "cd-a", "cd-a-r1", 1, "manual_review_insufficient",
                       gates=_ALL_PASS, buy="enabled")
        # Held computed approve_buy_locked (all gates pass, no org-id → buy locked).
        _seed_case(conn, "cd-b")
        _auto_decision(conn, "cd-b", "cd-b-r1", 1, "manual_review_insufficient",
                       gates=_ALL_PASS, buy="locked_org_id_required")
        # Genuine insufficient-evidence hold (a gate fails) — NOT a computed positive.
        _seed_case(conn, "cd-c")
        _auto_decision(conn, "cd-c", "cd-c-r1", 1, "manual_review_insufficient", gates=_NOT_ALL_PASS)
        # Pathological all-pass reject: a reject is authoritative and must NEVER be reinterpreted
        # as a computed positive (the `decision <> reject` guard).
        _seed_case(conn, "cd-d")
        _auto_decision(conn, "cd-d", "cd-d-r1", 1, "reject", gates=_ALL_PASS, buy="enabled")

    ar = client.get("/v1/metrics").json()["automation_readiness"]

    assert ar["automatic_decisions_by_type"] == {
        "approve": 1,               # cd-a reconstructed
        "approve_buy_locked": 1,    # cd-b reconstructed
        "manual_review_insufficient": 1,  # cd-c genuine hold
        "reject": 1,                # cd-d authoritative reject, not reinterpreted
    }
    assert ar["cases_would_auto_approve"] == 2   # cd-a, cd-b — the sample-review population
    assert ar["cases_engine_held"] == 2          # cd-c (hold) + cd-d (reject)


def test_latest_automatic_decision_wins_over_an_earlier_one(engine, client, clean_db):
    """The population view is per-case on the LATEST automatic decision by decision_sequence: a case
    that first held for insufficient evidence and later reached an all-pass (held) positive counts
    as would-auto-approve, not held."""
    with engine.begin() as conn:
        _seed_case(conn, "cd-e")
        _auto_decision(conn, "cd-e", "cd-e-r1", 1, "manual_review_insufficient", gates=_NOT_ALL_PASS)
        _auto_decision(conn, "cd-e", "cd-e-r2", 2, "manual_review_insufficient",
                       gates=_ALL_PASS, buy="locked_org_id_required")  # later, all gates pass

    ar = client.get("/v1/metrics").json()["automation_readiness"]
    assert ar["cases_would_auto_approve"] == 1
    assert ar["cases_engine_held"] == 0


def test_manual_history_counter_is_the_honest_non_gating_weaker_fact(engine, client, clean_db):
    """F3: the renamed counter is exactly what it measures — a NON-positive latest computed decision
    in a case that has ANY manual approval in its history. It has no "after" ordering and is not
    bound to the reviewed decision, so a computed-positive case with a manual row does NOT inflate
    it (it is not a proven engine-vs-human disagreement)."""
    with engine.begin() as conn:
        # Non-positive latest + a manual approval → counts.
        _seed_case(conn, "mh-hold")
        _auto_decision(conn, "mh-hold", "mh-hold-r1", 1, "manual_review_insufficient",
                       gates=_NOT_ALL_PASS)
        _manual_approval(conn, "mh-hold", "1")
        # Computed-positive latest + a manual approval → does NOT count (positive, not held).
        _seed_case(conn, "mh-pos")
        _auto_decision(conn, "mh-pos", "mh-pos-r1", 1, "manual_review_insufficient",
                       gates=_ALL_PASS, buy="enabled")
        _manual_approval(conn, "mh-pos", "1")

    ar = client.get("/v1/metrics").json()["automation_readiness"]
    assert ar["manual_approvals_total"] == 2
    assert ar["cases_would_auto_approve"] == 1          # mh-pos
    assert ar["cases_engine_nonpositive_with_manual_history"] == 1  # mh-hold only
    assert "human_approved_after_engine_held" not in ar  # the misleading name is gone


def test_enforcement_off_real_pipeline_holds_positive_yet_gauge_counts_it(
    session_factory, policy, settings, tmp_path, engine, clean_db, post_event, client
):
    """F1 END-TO-END RED proof — the scenario the synthetic test missed. Drive the REAL pipeline with
    the enforcement kill switch OFF (the production default): a computed positive is STORED as a hold
    and the callback carries enforcement_held, yet the gauge — reconstructing from the stamped gates
    — counts it in the would-auto-approve population."""
    with session_factory() as s:
        s.execute(text("INSERT INTO cases (id) VALUES ('ar-e2e')"))
        for ct, cat in [
            ("official_registry_match", "legal_business_proof"),
            ("business_document_verified", "legal_business_proof"),
            ("verified_company_email", "control_proof"),
            ("linkedin_company_match", "supporting"),
            ("website_verified", "supporting"),
        ]:
            checkstore.write_check(s, case_id="ar-e2e", check_type=ct, status=CheckStatus.PASS,
                                   points_awarded=policy.rubric.item(ct).points, category=cat,
                                   source="seed")  # 25+25+25+20+10 = 105 ≥ 100, no org-id
        s.commit()

    post_event("ar-e2e", "recalculate.requested", {})  # trigger an automatic decision run
    off = settings.model_copy(update={"enforce_positive_decisions": False})  # production default
    pl = Pipeline(session_factory, policy, FsStore(tmp_path / "off"), off, adapters={})
    Worker(session_factory, {"run_transition": pl.handle_job}, backoff_base_seconds=0,
           on_dead_letter=pl.on_dead_letter).run_until_idle()

    with session_factory() as s:
        row = s.execute(text(
            "SELECT decision, gates_json, buy_enablement FROM decisions "
            "WHERE case_id='ar-e2e' AND manual=false ORDER BY decision_sequence DESC LIMIT 1"
        )).one()
        callback = s.execute(text(
            "SELECT payload_json FROM outbox WHERE case_id='ar-e2e' ORDER BY id DESC LIMIT 1"
        )).scalar_one()

    # The enforcement overlay HELD the computed positive...
    assert row.decision == "manual_review_insufficient"
    assert all(row.gates_json.values()) and len(row.gates_json) == 5   # ...but all five gates pass
    assert row.buy_enablement == "locked_org_id_required"              # no org-id → buy locked
    assert callback["enforcement_held"]["computed_decision"] == "approve_buy_locked"

    # ...and the gauge counts the COMPUTED decision, not the stored hold.
    ar = client.get("/v1/metrics").json()["automation_readiness"]
    assert ar["cases_would_auto_approve"] == 1
    assert ar["cases_engine_held"] == 0
    assert ar["automatic_decisions_by_type"] == {"approve_buy_locked": 1}


def test_readiness_is_not_served_on_the_unauthenticated_ui_overview(engine, client, clean_db):
    """Re-audit `d3c0852..23e005e` F1: the sensitive + expensive readiness block must NOT ride the
    `/ui/api/overview` path (which reuses collect_metrics and is UNAUTHENTICATED until the PR 1.1 UI
    read-GET sweep). It is exposed only on the read-auth-gated `/v1/metrics` route."""
    with engine.begin() as conn:
        _seed_case(conn, "ov-a")
        _auto_decision(conn, "ov-a", "ov-a-r1", 1, "manual_review_insufficient",
                       gates=_ALL_PASS, buy="enabled")

    overview_metrics = client.get("/ui/api/overview").json()["metrics"]
    assert "automation_readiness" not in overview_metrics          # off the unauth / 5s-polled path
    assert "automation_readiness" in client.get("/v1/metrics").json()  # only the authed route


def test_metrics_route_checks_read_auth_before_opening_a_db_session(
    session_factory, dual_accept_settings, policy, clean_db
):
    """F4: /v1/metrics carries business-sensitive volumes + automation posture, so it must be
    read-auth gated (it inherited the open route before this fix). An unauthenticated request is
    rejected BEFORE any DB session opens; a validly-signed read is served."""
    auth_settings = dual_accept_settings.model_copy(update={"read_auth_required": True})
    app = create_app(auth_settings, session_factory=session_factory, policy=policy)

    def _boom():
        raise AssertionError("a DB session was opened before the read-auth check")

    app.state.session_factory = _boom  # any DB access during the request now fails loudly
    tc = TestClient(app)

    unauth = tc.get("/v1/metrics")
    assert unauth.status_code == 401  # require_read_access rejected before collect_metrics ran

    app.state.session_factory = session_factory  # restore for the authorized path
    headers = sign_headers_v2(b"", method="GET", path_qs="/v1/metrics")
    ok = tc.get("/v1/metrics", headers=headers)
    assert ok.status_code == 200
    assert "automation_readiness" in ok.json()
