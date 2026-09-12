"""Saved snapshots govern real admission, broker gates, scoring, and callbacks."""

import json
from uuid import uuid4

import pytest
from sqlalchemy import event, text

from kyc_tool.checkstore import repo as checkstore
from kyc_tool.config import ProcessRole
from kyc_tool.configuration import repo
from kyc_tool.configuration.models import ConfigurationUnavailable, validate_brokers
from kyc_tool.db.session import uow
from kyc_tool.db.tables import Case, Run
from kyc_tool.domain.models import BrokerStatus, CheckStatus
from kyc_tool.events.ingest import ingest_event
from kyc_tool.orchestration.broker_gate import BrokerGate
from kyc_tool.orchestration.pipeline import BundleUnavailable, Pipeline
from kyc_tool.queue.worker import Worker
from kyc_tool.storage.object_store import FsStore
from tests.conftest import bound_process, envelope, sign_headers
from tests.integration.shared import ACME_KYB
from tests.integration.test_configuration_api import AUTH, make_client
from tests.integration.test_configuration_repo import baseline

pytestmark = pytest.mark.usefixtures("clean_db")


def broker(policy="allowed", **updates):
    return {
        "id": "acme-broker",
        "name": "Acme Networks Ltd",
        "policy": policy,
        "aliases": [],
        "domains": [],
        "email_domains": [],
        "org_ids": [],
        "poc_handles": [],
        "asns": [],
        "notes": "saved snapshot",
        **updates,
    }


def save_section(sf, section, value):
    with uow(sf) as s:
        active = repo.get_active(s)
        return repo.save_section(
            s,
            section=section,
            expected_revision=active.revision,
            request_id=uuid4(),
            value=value,
            actor="test operator",
        )


def test_empty_snapshot_never_falls_back_and_blocked_match_records_exact_class(session_factory):
    with uow(session_factory) as s:
        s.execute(
            text(
                "INSERT INTO broker_entities (id,name,policy) "
                "VALUES ('legacy-only','Snapshot Company','blocked')"
            )
        )
    with session_factory() as s:
        gate = BrokerGate()
        assert gate(s, {"company_legal_name": "Snapshot Company"}) is BrokerStatus.BLOCKED
        clear = gate.match(s, {"company_legal_name": "Snapshot Company"}, snapshot=())
        assert (clear.status, clear.entity_id, clear.identifier_class) == (BrokerStatus.CLEAR, None, None)
        snapshot = validate_brokers(
            [
                broker(id="allowed", name="Allowed Name", domains=["ACME.EXAMPLE"]),
                broker("blocked", id="blocked", name="Blocked Name", domains=["acme.example"]),
                broker("blocked", id="other-class", name="Other Name", email_domains=["elsewhere.example"]),
            ]
        )
        hit = gate.match(s, {"website": "https://acme.example/path"}, snapshot=snapshot)
        assert (hit.status, hit.entity_id, hit.identifier_class) == (
            BrokerStatus.BLOCKED,
            "blocked",
            "domains",
        )
        miss = gate.match(s, {"website": "https://acme.example.evil"}, snapshot=snapshot)
        assert miss.status is BrokerStatus.CLEAR
        separate_class = gate.match(s, {"website": "https://elsewhere.example"}, snapshot=snapshot)
        assert separate_class.status is BrokerStatus.CLEAR


def send(client, case="acme", event_type="kyb.run_requested", value=None, key=None):
    body = json.dumps(envelope(event_type, ACME_KYB if value is None else value)).encode()
    return client.post(f"/v1/cases/{case}/events", content=body, headers=sign_headers(body, key=key))


def test_active_admission_uses_real_app_settings_and_central_pins(settings, session_factory, policy):
    baseline(session_factory, policy)
    client = make_client(settings, session_factory, policy)
    old = send(client)
    assert old.status_code == 202
    with session_factory() as s:
        run = s.get(Run, old.json()["run_id"])
        assert run.configuration_revision == repo.get_active(s).revision
    wrong = make_client(settings, session_factory, policy, enforce_bundle_pinning=False)
    assert send(wrong, case="refused").status_code == 503
    assert (
        wrong.post(
            "/ui/api/send-event",
            json={"case_id": "ui-refused", "event_type": "recalculate.requested", "payload": {}},
            headers=AUTH,
        ).status_code
        == 503
    )
    with session_factory() as s:
        assert s.get(Case, "refused") is None
        assert s.get(Case, "ui-refused") is None


def test_legacy_admission_holds_configuration_relation_fence_before_case_write(
    settings, session_factory, policy, engine
):
    observed = []

    def before_cursor(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO cases"):
            with session_factory() as separate, pytest.raises(ConfigurationUnavailable):
                repo.bootstrap(separate, policy=policy, actor="cutover")
            observed.append(True)

    event.listen(engine, "before_cursor_execute", before_cursor)
    try:
        result = ingest_event(
            session_factory,
            policy,
            case_id="legacy",
            idempotency_key="legacy",
            envelope=envelope("recalculate.requested", {}),
        )
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor)
    assert result.status_code == 202
    assert observed


def test_old_and_new_runs_use_saved_points_brokers_and_real_callback(
    settings, session_factory, policy, tmp_path
):
    baseline(session_factory, policy)
    save_section(session_factory, "brokers", [broker()])
    client = make_client(settings, session_factory, policy)
    body = json.dumps(envelope("kyb.run_requested", ACME_KYB)).encode()
    first = client.post("/v1/cases/acme/events", content=body, headers=sign_headers(body, key="original"))
    assert first.status_code == 202
    a = first.json()["run_id"]
    with uow(session_factory) as s:
        # Retained checks intentionally carry old stamped points. New decisions must reprice.
        for item in policy.rubric.items:
            checkstore.write_check(
                s,
                case_id="acme",
                check_type=item.check_type,
                status=CheckStatus.PASS,
                points_awarded=item.points,
                category=item.category,
                source="retained proof",
            )
        old_revision = s.get(Run, a).configuration_revision
    points = {i.check_type: i.points for i in policy.rubric.items}
    points["website_verified"] += 7
    save_section(session_factory, "points", points)
    save_section(session_factory, "brokers", [broker("blocked")])
    second = send(client, event_type="recalculate.requested", value={})
    assert second.status_code == 202
    b = second.json()["run_id"]
    replay = client.post("/v1/cases/acme/events", content=body, headers=sign_headers(body, key="original"))
    assert replay.status_code == 200 and replay.json()["run_id"] == a
    with uow(session_factory) as s:
        # Deliberately disagree with saved snapshots; neither worker should consult this table.
        s.execute(text("DELETE FROM broker_entities"))
    cfg = settings.model_copy(update={"enforce_bundle_pinning": True, "enforce_positive_decisions": False})
    pipeline = Pipeline(
        session_factory, policy, FsStore(tmp_path / "proof"), cfg, adapters={}, broker_matcher=BrokerGate()
    )
    worker = Worker(
        session_factory,
        {"run_transition": pipeline.handle_job},
        backoff_base_seconds=0,
        on_dead_letter=pipeline.on_dead_letter,
        **bound_process(ProcessRole.PIPELINE_WORKER),
    )
    worker.run_until_idle()
    with session_factory() as s:
        decisions = {
            r.run_id: r for r in s.execute(text("SELECT run_id,score,decision,gates_json FROM decisions"))
        }
        callbacks = {
            r.run_id: r.payload_json
            for r in s.execute(text("SELECT run_id,payload_json FROM outbox WHERE kind='decision_callback'"))
        }
        assert decisions[a].score == sum(i.points for i in policy.rubric.items)
        assert decisions[b].score == decisions[a].score + 7
        assert decisions[a].decision == "manual_review_insufficient"
        assert decisions[a].gates_json["broker_ok"] is True
        assert decisions[b].decision == "reject"
        assert decisions[b].gates_json["broker_ok"] is False
        assert callbacks[a]["enforcement_held"]["reason"] == "positive_enforcement_disabled"
        assert (
            next(c["points"] for c in callbacks[b]["checks"] if c["type"] == "website_verified")
            == points["website_verified"]
        )
        for run_id in (a, b):
            run = s.get(Run, run_id)
            assert run.matched_broker_entity_id == "acme-broker"
            assert run.matched_identifier_class == "legal_name"
        assert s.get(Run, a).configuration_revision == old_revision
        assert s.get(Run, b).configuration_revision != old_revision
        b_revision = str(s.get(Run, b).configuration_revision)
    save_section(session_factory, "points", points | {"website_verified": points["website_verified"] + 9})
    historical = client.get("/ui/api/cases/acme/full").json()
    assert historical["score"]["configuration_revision"] == b_revision
    assert (
        next(i["points"] for i in historical["score"]["items"] if i["check_type"] == "website_verified")
        == points["website_verified"]
    )
    assert historical["pointer_decision"]["score"] == decisions[b].score
    with uow(session_factory) as s:
        first_id = s.execute(text("SELECT id FROM decisions WHERE run_id=:r"), {"r": a}).scalar_one()
        s.execute(text("UPDATE cases SET latest_decision_row_id=:d WHERE id='acme'"), {"d": first_id})
    pointed = client.get("/ui/api/cases/acme/full").json()
    assert pointed["score"]["configuration_revision"] == str(old_revision)
    assert pointed["score"]["rubric_scope"] == "pointed_decision"
    assert pointed["pointer_decision"]["score"] == decisions[a].score
    with uow(session_factory) as s:
        s.execute(text("SET LOCAL session_replication_role='replica'"))
        s.execute(text("UPDATE cases SET latest_decision_row_id='missing' WHERE id='acme'"))
        s.execute(text("SET LOCAL session_replication_role='origin'"))
    unresolved = client.get("/ui/api/cases/acme/full").json()
    assert unresolved["score"]["items"] == []
    assert unresolved["score"]["bundle_hash"] is None
    assert unresolved["score"]["rubric_provenance"] == "unavailable_decision_authority"
    # Reconstructed process still uses active snapshot, not packaged settings.
    again = make_client(settings, session_factory, policy)
    c = send(again, case="other").json()["run_id"]
    reconstructed = Pipeline(
        session_factory, policy, FsStore(tmp_path / "restart"), cfg, broker_matcher=BrokerGate()
    )
    Worker(
        session_factory,
        {"run_transition": reconstructed.handle_job},
        backoff_base_seconds=0,
        **bound_process(ProcessRole.PIPELINE_WORKER),
    ).run_until_idle()
    with session_factory() as s:
        assert (
            s.execute(text("SELECT decision FROM decisions WHERE run_id=:r"), {"r": c}).scalar_one()
            == "reject"
        )


def test_decision_rechecks_run_broker_snapshot_not_mutable_case(settings, session_factory, policy, tmp_path):
    baseline(session_factory, policy)
    save_section(session_factory, "brokers", [broker("blocked")])
    client = make_client(settings, session_factory, policy)
    run_id = send(client).json()["run_id"]
    with uow(session_factory) as s:
        s.execute(text("UPDATE runs SET state='VALIDATE' WHERE id=:r"), {"r": run_id})
        s.execute(text("UPDATE cases SET broker_status='clear' WHERE id='acme'"))
    cfg = settings.model_copy(update={"enforce_bundle_pinning": True, "enforce_positive_decisions": False})
    pipeline = Pipeline(
        session_factory, policy, FsStore(tmp_path / "proof"), cfg, broker_matcher=BrokerGate()
    )
    Worker(
        session_factory,
        {"run_transition": pipeline.handle_job},
        backoff_base_seconds=0,
        **bound_process(ProcessRole.PIPELINE_WORKER),
    ).run_until_idle()
    with session_factory() as s:
        assert (
            s.execute(text("SELECT decision FROM decisions WHERE run_id=:r"), {"r": run_id}).scalar_one()
            == "reject"
        )
        assert s.get(Run, run_id).matched_broker_entity_id == "acme-broker"


@pytest.mark.parametrize("broken", ["flag_off", "missing_pointer", "corrupt_brokers", "corrupt_bundle"])
def test_configuration_failure_refuses_worker_before_any_side_effect(
    settings, session_factory, policy, tmp_path, broken
):
    baseline(session_factory, policy)
    client = make_client(settings, session_factory, policy)
    run_id = send(client).json()["run_id"]
    with uow(session_factory) as s:
        if broken == "missing_pointer":
            s.execute(text("DELETE FROM configuration_state"))
        elif broken == "corrupt_brokers":
            s.execute(text("ALTER TABLE configuration_revisions DISABLE TRIGGER USER"))
            s.execute(text("UPDATE configuration_revisions SET brokers_json='[{}]'"))
            s.execute(text("ALTER TABLE configuration_revisions ENABLE TRIGGER USER"))
        elif broken == "corrupt_bundle":
            s.execute(
                text(
                    "UPDATE policy_bundles SET files_json="
                    "jsonb_set(files_json,'{scoring_rubric.json}','\"%%%\"')"
                )
            )
    cfg = settings.model_copy(update={"enforce_bundle_pinning": broken != "flag_off"})
    pipeline = Pipeline(
        session_factory, policy, FsStore(tmp_path / "proof"), cfg, broker_matcher=BrokerGate()
    )
    with session_factory() as s, pytest.raises(BundleUnavailable):
        pipeline.resolve_bundle(s, s.get(Run, run_id))
    with session_factory() as s:
        for table in ("decisions", "checks", "adapter_results", "outbox", "review_tasks", "poc_tokens"):
            assert s.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0
        assert s.get(Run, run_id).state == "QUEUED"
    Worker(
        session_factory,
        {"run_transition": pipeline.handle_job},
        backoff_base_seconds=0,
        on_dead_letter=pipeline.on_dead_letter,
        **bound_process(ProcessRole.PIPELINE_WORKER),
    ).run_until_idle()
    with session_factory() as s:
        assert (
            s.execute(
                text("SELECT status FROM jobs WHERE payload_json->>'run_id'=:r"), {"r": run_id}
            ).scalar_one()
            == "dead"
        )
        for table in ("decisions", "checks", "adapter_results", "outbox", "review_tasks", "poc_tokens"):
            assert s.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0


def test_manual_record_only_uses_active_policy_and_legacy_requeue_refuses(
    settings, session_factory, policy, tmp_path
):
    baseline(session_factory, policy)
    points = {i.check_type: i.points for i in policy.rubric.items} | {"website_verified": 99}
    save_section(session_factory, "points", points)
    client = make_client(settings, session_factory, policy)
    body = json.dumps(
        envelope(
            "reviewer.manual_approve",
            {"reviewer_id": "Reviewer", "note": "record only"},
            {"type": "reviewer", "id": "Reviewer"},
        )
    ).encode()
    assert client.post("/v1/cases/manual/events", content=body, headers=sign_headers(body)).status_code == 200
    with session_factory() as s:
        assert (
            s.execute(text("SELECT policy_shas FROM decisions WHERE case_id='manual'")).scalar_one()
            == repo.get_active(s).bundle.shas
        )
        assert s.execute(text("SELECT count(*) FROM runs")).scalar_one() == 0
        assert s.execute(text("SELECT count(*) FROM outbox")).scalar_one() == 0
    run_id = send(client).json()["run_id"]
    with uow(session_factory) as s:
        # Simulate an old incompatible writer, not a supported repin operation.
        s.execute(text("ALTER TABLE runs DISABLE TRIGGER USER"))
        s.execute(
            text("UPDATE runs SET configuration_revision=NULL,state='FAILED' WHERE id=:r"), {"r": run_id}
        )
        s.execute(text("ALTER TABLE runs ENABLE TRIGGER USER"))
        job_id = s.execute(text("UPDATE jobs SET status='dead' RETURNING id")).scalar_one()
    cfg = settings.model_copy(update={"enforce_bundle_pinning": True})
    pipeline = Pipeline(session_factory, policy, FsStore(tmp_path / "proof"), cfg)
    with session_factory() as s, pytest.raises(BundleUnavailable):
        pipeline.resolve_bundle(s, s.get(Run, run_id))
    assert client.post(f"/v1/ops/requeue/job/{job_id}", headers=AUTH).status_code == 409
    with session_factory() as s:
        assert s.execute(text("SELECT status FROM jobs WHERE id=:j"), {"j": job_id}).scalar_one() == "dead"


def test_manual_pointer_does_not_guess_between_legacy_unsequenced_automatic_rubrics(client, engine):
    from tests.integration.test_read_latest_decision import _insert_chain

    with engine.begin() as conn:
        conn.execute(text("INSERT INTO cases (id,status) VALUES ('legacy-rubric','approved_manual')"))
        _insert_chain(conn, "legacy-rubric", "old-one", 1, "one")
        _insert_chain(conn, "legacy-rubric", "old-two", 2, "two")
        conn.execute(
            text(
                "INSERT INTO decisions (id,case_id,run_id,decision,score,gates_json,buy_enablement,"
                "policy_shas,manual,reviewer_id) VALUES ('manual-rubric','legacy-rubric',NULL,'approve',"
                "10,'{}','enabled','{}',true,'Reviewer')"
            )
        )
        definition = conn.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname='ck_decisions_manual_sequence'"
            )
        ).scalar_one()
        conn.execute(text("ALTER TABLE decisions DROP CONSTRAINT ck_decisions_manual_sequence"))
        conn.execute(
            text(
                "UPDATE decisions SET decision_sequence=NULL "
                "WHERE case_id='legacy-rubric' AND manual IS FALSE"
            )
        )
        conn.execute(
            text(
                "ALTER TABLE decisions ADD CONSTRAINT ck_decisions_manual_sequence "
                + definition
                + " NOT VALID"
            )
        )
    try:
        full = client.get("/ui/api/cases/legacy-rubric/full").json()
        assert full["pointer_decision"]["manual"] is True
        assert full["score"]["items"] == []
        assert full["score"]["rubric_provenance"] == "unavailable_legacy_order"
    finally:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE decisions SET decision_sequence=CASE run_id WHEN 'old-one' THEN 1 ELSE 2 END "
                    "WHERE case_id='legacy-rubric' AND manual IS FALSE"
                )
            )
            conn.execute(text("ALTER TABLE decisions VALIDATE CONSTRAINT ck_decisions_manual_sequence"))
