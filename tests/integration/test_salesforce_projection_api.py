"""Public, authenticated Salesforce projection over one PostgreSQL snapshot."""

import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text

from kyc_tool.api.app import create_app
from kyc_tool.config import ProcessRole
from kyc_tool.db.tables import Case, Check, DecisionRow, Run
from kyc_tool.orchestration.pipeline import Pipeline
from kyc_tool.queue.worker import Worker
from kyc_tool.storage.object_store import FsStore
from tests.conftest import envelope, process_context, sign_headers
from tests.integration.shared import ACME_KYB
from tests.integration.test_configuration_api import make_client
from tests.integration.test_configuration_repo import baseline
from tests.integration.test_configuration_runtime import save_section
from tests.integration.test_read_latest_decision import (
    _insert_chain,
    _restore_latest_decision_fk,
)

pytestmark = pytest.mark.usefixtures("clean_db")

PATH = "/v1/cases/{case_id}/salesforce-projection"


def _pinned_stack(settings, session_factory, policy, tmp_path):
    pinned_settings = settings.model_copy(update={"enforce_bundle_pinning": True})
    client = TestClient(create_app(pinned_settings, session_factory=session_factory, policy=policy))
    pipeline = Pipeline(
        session_factory,
        policy,
        FsStore(tmp_path / "evidence"),
        pinned_settings,
        adapters={},
    )
    worker = Worker(
        session_factory,
        {"run_transition": pipeline.handle_job},
        lease_seconds=pinned_settings.job_lease_seconds,
        backoff_base_seconds=0,
        on_dead_letter=pipeline.on_dead_letter,
        process_role=process_context(ProcessRole.PIPELINE_WORKER, pinned_settings),
        settings=pinned_settings,
    )

    def post(case_id, event_type, payload, actor=None):
        body = json.dumps(envelope(event_type, payload, actor)).encode()
        return client.post(
            f"/v1/cases/{case_id}/events",
            content=body,
            headers=sign_headers(body, key=uuid4().hex),
        )

    return client, worker, post


def _field(body, source_field):
    return next(field for field in body["fields"].values() if field["source_field"] == source_field)


def test_public_projection_is_mounted_without_the_ui_and_uses_identity_mapping(
    settings, session_factory, policy
):
    with session_factory() as session:
        session.add(Case(id="c1"))
        session.commit()
    client = make_client(settings, session_factory, policy, ui_enabled=False)

    response = client.get(PATH.format(case_id="c1"))

    assert response.status_code == 200
    body = response.json()
    assert body["mapping_revision"] is None
    assert body["configuration_revision"] is None
    assert body["fields"]["KYC_Status__c"] == {
        "source_field": "KYC_Status__c",
        "source_identity": "case.status",
        "value_type": "enum",
        "nullable": True,
        "value": "KYC Pending",
    }


def test_public_projection_uses_one_pointed_automatic_row_and_its_run_pin(
    settings, session_factory, policy, tmp_path
):
    base = baseline(session_factory, policy)
    client, worker, post = _pinned_stack(settings, session_factory, policy, tmp_path)
    created = post("automatic", "kyb.run_requested", ACME_KYB)
    assert created.status_code == 202
    worker.run_until_idle()
    with session_factory() as session:
        case = session.get(Case, "automatic")
        decision = session.get(DecisionRow, case.latest_decision_row_id)
        run = session.get(Run, decision.run_id)
        old_run_revision = str(run.configuration_revision)
    saved = save_section(
        session_factory,
        "mappings",
        base.mappings | {"KYC_Status__c": "Current_Status__c"},
    )

    body = client.get(PATH.format(case_id="automatic")).json()

    assert body["mapping_revision"] == saved["revision"]
    assert body["mapping_revision"] != old_run_revision
    assert body["configuration_revision"] == old_run_revision
    assert body["decision_authority"] == {
        "provenance": "latest_decision_row",
        "decision_row_id": decision.id,
        "run_id": run.id,
        "decision_kind": "automatic",
        "decision": decision.decision,
        "run_provenance": "resolved",
    }
    assert body["fields"]["Current_Status__c"]["source_field"] == "KYC_Status__c"


def test_unresolved_decision_authority_projects_null_decision_values(settings, session_factory, policy):
    client = make_client(settings, session_factory, policy, ui_enabled=False)
    with session_factory() as session:
        session.add(Case(id="legacy", last_decision_sequence=1, latest_decision="approve"))
        session.commit()
    with session_factory.kw["bind"].begin() as connection:
        _insert_chain(connection, "legacy", "legacy-r1", 1, "legacy")
        connection.execute(text("UPDATE cases SET latest_decision_row_id=NULL WHERE id='legacy'"))

    body = client.get(PATH.format(case_id="legacy")).json()

    assert body["decision_authority"] == {
        "provenance": "unresolved_legacy_order",
        "decision_row_id": None,
        "run_id": None,
        "decision_kind": None,
        "decision": None,
        "run_provenance": "not_applicable",
    }
    assert _field(body, "KYC_Score__c")["value"] is None
    assert _field(body, "Platform_Action_Taken__c")["value"] is None
    assert _field(body, "Hard_Conflict__c")["value"] is None


def test_decision_authority_matrix(settings, session_factory, policy):
    client = make_client(settings, session_factory, policy, ui_enabled=False)
    with session_factory() as session:
        session.add_all(
            [
                Case(id="matrix-auto", last_decision_sequence=1),
                Case(id="matrix-manual"),
                Case(id="matrix-legacy", last_decision_sequence=1),
                Case(id="matrix-drift"),
                Case(id="matrix-none"),
            ]
        )
        session.commit()
    with session_factory.kw["bind"].begin() as connection:
        _insert_chain(connection, "matrix-auto", "matrix-auto-r1", 1, "auto")
        _insert_chain(connection, "matrix-legacy", "matrix-legacy-r1", 1, "legacy")
        connection.execute(
            text(
                "INSERT INTO decisions (id,case_id,run_id,decision,score,gates_json,"
                "buy_enablement,policy_shas,manual,reviewer_id) VALUES "
                "('matrix-manual-d','matrix-manual',NULL,'approve',7,CAST(:g AS jsonb),"
                "'buy_locked_org_id_required','{}'::jsonb,true,'rev-matrix')"
            ),
            {"g": '{"bypassed":true}'},
        )
        connection.execute(text("UPDATE cases SET latest_decision_row_id=NULL WHERE id='matrix-legacy'"))
        connection.execute(text("SET LOCAL session_replication_role='replica'"))
        connection.execute(text("UPDATE cases SET latest_decision_row_id='missing' WHERE id='matrix-drift'"))
        connection.execute(text("SET LOCAL session_replication_role='origin'"))

    try:
        expected = {
            "matrix-auto": ("latest_decision_row", "automatic", "resolved"),
            "matrix-manual": ("latest_decision_row", "manual", "not_applicable"),
            "matrix-legacy": ("unresolved_legacy_order", None, "not_applicable"),
            "matrix-drift": ("unresolved_pointer_drift", None, "not_applicable"),
            "matrix-none": ("no_decisions", None, "not_applicable"),
        }
        for case_id, shape in expected.items():
            authority = client.get(PATH.format(case_id=case_id)).json()["decision_authority"]
            assert (
                authority["provenance"],
                authority["decision_kind"],
                authority["run_provenance"],
            ) == shape
            if shape[1] is None:
                assert authority["decision_row_id"] is authority["run_id"] is authority["decision"] is None
    finally:
        with session_factory.kw["bind"].begin() as connection:
            connection.execute(text("SET LOCAL session_replication_role='replica'"))
            connection.execute(text("UPDATE cases SET latest_decision_row_id=NULL WHERE id='matrix-drift'"))
            connection.execute(text("SET LOCAL session_replication_role='origin'"))


def test_mapping_save_changes_the_next_destination_key_not_old_decision_or_callback_bytes(
    settings, session_factory, policy, tmp_path
):
    base = baseline(session_factory, policy)
    client, worker, post = _pinned_stack(settings, session_factory, policy, tmp_path)
    run_id = post("history", "kyb.run_requested", ACME_KYB).json()["run_id"]
    worker.run_until_idle()
    with session_factory() as session:
        decision_before = dict(
            session.execute(
                text(
                    "SELECT id,case_id,run_id,decision,score,gates_json,buy_enablement,policy_shas,"
                    "engine_build_id,decided_at,published_at,decision_sequence,manual,reviewer_id "
                    "FROM decisions WHERE run_id=:run_id"
                ),
                {"run_id": run_id},
            )
            .mappings()
            .one()
        )
        callback_before = session.execute(
            text("SELECT payload_json FROM outbox WHERE run_id=:run_id"), {"run_id": run_id}
        ).scalar_one()
        run_revision_before = session.get(Run, run_id).configuration_revision
    saved = save_section(
        session_factory,
        "mappings",
        base.mappings | {"KYC_Status__c": "Saved_Status__c"},
    )

    body = client.get(PATH.format(case_id="history")).json()

    assert body["mapping_revision"] == saved["revision"]
    assert "Saved_Status__c" in body["fields"] and "KYC_Status__c" not in body["fields"]
    with session_factory() as session:
        decision_after = dict(
            session.execute(
                text(
                    "SELECT id,case_id,run_id,decision,score,gates_json,buy_enablement,policy_shas,"
                    "engine_build_id,decided_at,published_at,decision_sequence,manual,reviewer_id "
                    "FROM decisions WHERE run_id=:run_id"
                ),
                {"run_id": run_id},
            )
            .mappings()
            .one()
        )
        callback_after = session.execute(
            text("SELECT payload_json FROM outbox WHERE run_id=:run_id"), {"run_id": run_id}
        ).scalar_one()
        assert session.get(Run, run_id).configuration_revision == run_revision_before
    assert decision_after == decision_before
    assert callback_after == callback_before


def test_manual_projection_has_explicit_tool_provenance_without_platform_enforcement_claim(
    settings, session_factory, policy, tmp_path
):
    baseline(session_factory, policy)
    client, _, post = _pinned_stack(settings, session_factory, policy, tmp_path)
    response = post(
        "manual",
        "reviewer.manual_approve",
        {"reviewer_id": "rev-1"},
        actor={"type": "reviewer", "id": "rev-1"},
    )
    assert response.status_code == 200

    body = client.get(PATH.format(case_id="manual")).json()

    authority = body["decision_authority"]
    assert authority["provenance"] == "latest_decision_row"
    assert authority["decision_kind"] == "manual"
    assert authority["run_id"] is None
    assert body["configuration_revision"] is None
    assert authority["run_provenance"] == "not_applicable"
    assert body["manual_approval_authority"]["provenance"] == "latest_manual_row"
    assert _field(body, "Platform_Action_Taken__c")["value"] == "Manual Approve"
    assert _field(body, "Hard_Conflict__c")["value"] is None
    assert not ({"enforced", "accepted", "sequence"} & set(authority))


def test_sticky_manual_projection_survives_a_later_automatic_decision(
    settings, session_factory, policy, tmp_path
):
    baseline(session_factory, policy)
    client, worker, post = _pinned_stack(settings, session_factory, policy, tmp_path)
    post("sticky", "kyb.run_requested", ACME_KYB)
    worker.run_until_idle()
    post(
        "sticky",
        "reviewer.manual_approve",
        {"reviewer_id": "rev-sticky"},
        actor={"type": "reviewer", "id": "rev-sticky"},
    )
    with session_factory() as session:
        manual_id = session.get(Case, "sticky").latest_manual_decision_row_id
    later = post("sticky", "recalculate.requested", {})
    assert later.status_code == 202
    worker.run_until_idle()

    body = client.get(PATH.format(case_id="sticky")).json()

    assert body["decision_authority"]["decision_kind"] == "automatic"
    assert body["decision_authority"]["run_id"] == later.json()["run_id"]
    assert body["manual_approval_authority"]["decision_row_id"] == manual_id
    assert body["manual_approval_authority"]["reviewer_id"] == "rev-sticky"
    assert _field(body, "Platform_Action_Taken__c")["value"] == "Manual Approve"
    assert _field(body, "Manual_Approved_By__c")["value"] == "rev-sticky"
    assert _field(body, "Manual_Approved_At__c")["value"] == body["manual_approval_authority"]["decided_at"]


def test_projection_snapshot_never_mixes_a_mapping_revision_with_later_mapping_values(
    settings, session_factory, policy
):
    base = baseline(session_factory, policy)
    client = make_client(settings, session_factory, policy, ui_enabled=False)
    with session_factory() as session:
        session.add(Case(id="snapshot"))
        session.commit()
    engine = session_factory.kw["bind"]
    armed = True
    saved = None

    def save_after_pointer_read(connection, cursor, statement, parameters, context, executemany):
        nonlocal armed, saved
        if armed and "FROM configuration_state" in statement:
            armed = False
            saved = save_section(
                session_factory,
                "mappings",
                base.mappings | {"KYC_Status__c": "Later_Status__c"},
            )

    event.listen(engine, "after_cursor_execute", save_after_pointer_read)
    try:
        during = client.get(PATH.format(case_id="snapshot")).json()
    finally:
        event.remove(engine, "after_cursor_execute", save_after_pointer_read)

    assert saved is not None
    assert during["mapping_revision"] == str(base.revision)
    assert "KYC_Status__c" in during["fields"]
    assert "Later_Status__c" not in during["fields"]
    after = client.get(PATH.format(case_id="snapshot")).json()
    assert after["mapping_revision"] == saved["revision"]
    assert "Later_Status__c" in after["fields"]


def test_projection_snapshot_keeps_case_state_and_checks_in_one_reader_snapshot(
    settings, session_factory, policy
):
    case_id = "case-check-snapshot"
    client = make_client(settings, session_factory, policy, ui_enabled=False)
    with session_factory() as session:
        session.add(Case(id=case_id, status="kyc_pending"))
        session.commit()

    engine = session_factory.kw["bind"]
    armed = True
    transition_committed = False
    reader_modes = {}

    def transition_after_case_read(connection, cursor, statement, parameters, context, executemany):
        nonlocal armed, transition_committed
        if armed and "FROM cases" in statement and "WHERE cases.id =" in statement:
            armed = False
            reader_modes["isolation"] = connection.exec_driver_sql("SHOW transaction_isolation").scalar_one()
            reader_modes["read_only"] = connection.exec_driver_sql("SHOW transaction_read_only").scalar_one()
            with session_factory() as writer:
                writer.get(Case, case_id).status = "account_approved"
                writer.add(
                    Check(
                        id="case-check-snapshot-new",
                        case_id=case_id,
                        check_type="snapshot_transition",
                        status="pass",
                        points_awarded=9,
                        category="identity",
                        source="snapshot-test",
                        reason_codes=["COMMITTED_AFTER_CASE_READ"],
                    )
                )
                writer.commit()
            transition_committed = True

    event.listen(engine, "after_cursor_execute", transition_after_case_read)
    try:
        during = client.get(PATH.format(case_id=case_id)).json()
    finally:
        event.remove(engine, "after_cursor_execute", transition_after_case_read)

    assert transition_committed
    assert _field(during, "KYC_Status__c")["value"] == "KYC Pending"
    assert _field(during, "KYC_Check__c")["value"] == []

    after = client.get(PATH.format(case_id=case_id)).json()
    assert _field(after, "KYC_Status__c")["value"] == "Account Approved"
    after_checks = _field(after, "KYC_Check__c")["value"]
    assert len(after_checks) == 1
    assert {key: value for key, value in after_checks[0].items() if key != "Created_At__c"} == {
        "Check_Type__c": "snapshot_transition",
        "Status__c": "pass",
        "Points__c": 9,
        "Category__c": "identity",
        "Source__c": "snapshot-test",
        "Superseded__c": False,
        "Reason_Codes__c": "COMMITTED_AFTER_CASE_READ",
    }
    assert after_checks[0]["Created_At__c"]
    assert reader_modes == {"isolation": "repeatable read", "read_only": "on"}


def test_projection_pointer_drift_is_explicit(settings, session_factory, policy):
    client = make_client(settings, session_factory, policy, ui_enabled=False)
    engine = session_factory.kw["bind"]
    try:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE cases DROP CONSTRAINT fk_cases_latest_decision"))
            connection.execute(text("INSERT INTO cases (id) VALUES ('drift-a')"))
            connection.execute(text("INSERT INTO cases (id,last_decision_sequence) VALUES ('drift-b',1)"))
            _insert_chain(connection, "drift-b", "drift-b-r1", 1, "borrowed")
            connection.execute(
                text(
                    "INSERT INTO decisions (id,case_id,run_id,decision,score,gates_json,"
                    "buy_enablement,policy_shas,manual,reviewer_id) VALUES "
                    "('drift-a-manual','drift-a',NULL,'approve',1,CAST(:g AS jsonb),"
                    "'buy_locked_org_id_required','{}'::jsonb,true,'rev-a')"
                ),
                {"g": '{"bypassed":true}'},
            )
            connection.execute(
                text("UPDATE cases SET latest_decision_row_id='drift-b-r1-d' WHERE id='drift-a'")
            )

        body = client.get(PATH.format(case_id="drift-a")).json()
        assert body["decision_authority"]["provenance"] == "unresolved_pointer_drift"
        assert body["decision_authority"]["decision"] is None
        assert _field(body, "KYC_Score__c")["value"] is None
        assert _field(body, "Platform_Action_Taken__c")["value"] is None
        assert _field(body, "Hard_Conflict__c")["value"] is None
    finally:
        _restore_latest_decision_fk(engine)


def test_authenticated_public_projection_accepts_public_read_signature(settings, session_factory, policy):
    with session_factory() as session:
        session.add(Case(id="signed"))
        session.commit()
    client = make_client(
        settings,
        session_factory,
        policy,
        ui_enabled=False,
        read_auth_required=True,
    )

    response = client.get(PATH.format(case_id="signed"), headers=sign_headers(b""))

    assert response.status_code == 200


def test_projection_returns_404_for_an_unknown_case(settings, session_factory, policy):
    client = make_client(settings, session_factory, policy, ui_enabled=False)
    response = client.get(PATH.format(case_id="missing"))
    assert response.status_code == 404
    assert response.json()["detail"] == "case not found"


def test_projection_returns_safe_retry_when_configuration_is_unavailable(settings, session_factory, policy):
    baseline(session_factory, policy)
    with session_factory() as session:
        session.add(Case(id="unavailable"))
        session.execute(text("DELETE FROM configuration_state"))
        session.commit()
    client = make_client(settings, session_factory, policy, ui_enabled=False)

    response = client.get(PATH.format(case_id="unavailable"))

    assert response.status_code == 503
    assert response.json()["detail"] == "Configuration is unavailable; retry safely."


def test_openapi_exposes_typed_public_salesforce_projection(client):
    path = client.get("/openapi.json").json()["paths"][PATH]["get"]
    assert path["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/SalesforceProjectionResponse"
    )
    assert "304" not in path["responses"]
