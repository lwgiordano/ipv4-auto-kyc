"""Explicit drained cutover; preflight reads, apply fences and rechecks."""

import importlib
import os
import subprocess
import sys
from dataclasses import replace
from urllib.parse import quote
from uuid import uuid4

import pytest
from sqlalchemy import event, text

from kyc_tool.configuration.models import ConfigurationInvalid, ConfigurationUnavailable, brokers_json
from kyc_tool.db.session import uow
from kyc_tool.db.tables import Case, Event, Run
from tests.integration.test_configuration_repo import baseline, counts, repo

pytestmark = pytest.mark.usefixtures("clean_db")


def ops():
    assert importlib.util.find_spec("kyc_tool.ops.activate_live_configuration") is not None, (
        "configuration activation is missing"
    )
    return importlib.import_module("kyc_tool.ops.activate_live_configuration")


def activate(sf, policy, settings, **kwargs):
    settings = settings.model_copy(update={"enforce_bundle_pinning": True, "ui_admin_token": "test-admin"})
    return ops().activate(sf, policy=policy, settings=settings, actor="operator", **kwargs)


def test_preflight_is_default_read_only_and_apply_requires_attestation(session_factory, policy, settings):
    report = activate(session_factory, policy, settings)
    assert report["ready"] is True
    assert report["active_revision"] is None
    assert counts(session_factory) == (0, 0, 0)
    with pytest.raises(ConfigurationInvalid):
        activate(session_factory, policy, settings, apply=True)
    assert counts(session_factory) == (0, 0, 0)


@pytest.mark.parametrize("pinning,token", [(False, "admin"), (True, ""), (True, "   ")])
def test_activation_requires_pinning_and_nonempty_admin_in_development(
    session_factory, policy, settings, pinning, token
):
    settings = settings.model_copy(update={"enforce_bundle_pinning": pinning, "ui_admin_token": token})
    with pytest.raises(ConfigurationInvalid):
        ops().activate(
            session_factory,
            policy=policy,
            settings=settings,
            actor="operator",
            apply=True,
            attest_writers_stopped=True,
        )
    assert counts(session_factory) == (0, 0, 0)


@pytest.mark.parametrize("status", ["queued", "running", "dead", "pending"])
def test_legacy_job_blocks_cutover_without_any_write(session_factory, policy, settings, status):
    with uow(session_factory) as s:
        s.execute(
            text("INSERT INTO jobs (kind,status,payload_json) VALUES ('run_transition',:status,'{}')"),
            {"status": status},
        )
    report = activate(session_factory, policy, settings)
    assert report["ready"] is False
    assert len(report["blocking_jobs"]) == 1
    with pytest.raises(ConfigurationUnavailable):
        activate(session_factory, policy, settings, apply=True, attest_writers_stopped=True)
    assert counts(session_factory) == (0, 0, 0)


def test_apply_rechecks_after_successful_preflight(session_factory, policy, settings):
    assert activate(session_factory, policy, settings)["ready"]
    with uow(session_factory) as s:
        s.execute(text("INSERT INTO jobs (kind,status,payload_json) VALUES ('run_transition','dead','{}')"))
    with pytest.raises(ConfigurationUnavailable):
        activate(session_factory, policy, settings, apply=True, attest_writers_stopped=True)
    assert counts(session_factory) == (0, 0, 0)


def test_activation_snapshots_ids_notes_and_never_resets_active(session_factory, policy, settings):
    with uow(session_factory) as s:
        s.execute(text("UPDATE broker_entities SET notes='retain this note'"))
        ids = set(s.execute(text("SELECT id FROM broker_entities")).scalars())
    result = activate(session_factory, policy, settings, apply=True, attest_writers_stopped=True)
    with session_factory() as s:
        saved = repo().get_active(s)
    assert str(saved.revision) == result["active_revision"]
    assert {b.id for b in saved.brokers} == ids
    assert all(b.notes == "retain this note" for b in saved.brokers)
    assert saved.mappings["KYC_Score__c"] == "KYC_Score__c"
    before = counts(session_factory)
    # No directory is needed when an already-active configuration is verified.
    again = activate(
        session_factory, replace(policy, policy_dir=None), settings, apply=True, attest_writers_stopped=True
    )
    assert again["active_revision"] == result["active_revision"]
    assert counts(session_factory) == before
    with session_factory() as s:
        audit = s.execute(text("SELECT actor,detail_json FROM audit_log")).one()
        assert audit.actor == "administrative_cli"
        assert audit.detail_json["operator_label"] == "operator"
        assert "test-admin" not in str(audit)


def test_oversized_legacy_broker_baseline_refuses_without_truncation(session_factory, policy, settings):
    with uow(session_factory) as s:
        s.execute(text("UPDATE broker_entities SET notes=:notes"), {"notes": "x" * 2001})
    report = activate(session_factory, policy, settings)
    assert report["ready"] is False
    with pytest.raises(ConfigurationUnavailable):
        activate(session_factory, policy, settings, apply=True, attest_writers_stopped=True)
    assert counts(session_factory) == (0, 0, 0)


def test_bootstrap_does_not_reset_lost_pointer(session_factory, policy, settings):
    baseline(session_factory, policy)
    with uow(session_factory) as s:
        s.execute(text("DELETE FROM configuration_state"))
    with pytest.raises(ConfigurationUnavailable):
        activate(session_factory, policy, settings, apply=True, attest_writers_stopped=True)


@pytest.mark.parametrize("state", ["QUEUED", "FAILED", "RUN_ADAPTERS"])
def test_legacy_retryable_run_without_job_is_a_blocker(session_factory, policy, settings, state):
    with uow(session_factory) as s:
        s.add(Case(id="legacy"))
        s.flush()
        s.add(
            Event(
                id="legacy-event",
                case_id="legacy",
                event_type="legacy",
                idempotency_key="old",
                payload_hash="old",
                event_sequence=1,
            )
        )
        s.flush()
        s.add(Run(id="legacy-run", case_id="legacy", triggering_event_id="legacy-event", state=state))
    report = activate(session_factory, policy, settings)
    assert report["blocking_runs"] == ["legacy-run"]
    with pytest.raises(ConfigurationUnavailable):
        activate(session_factory, policy, settings, apply=True, attest_writers_stopped=True)


def test_completed_historical_run_is_never_backfilled(session_factory, policy, settings):
    with uow(session_factory) as s:
        s.add(Case(id="legacy"))
        s.flush()
        s.add(
            Event(
                id="legacy-event",
                case_id="legacy",
                event_type="legacy",
                idempotency_key="old",
                payload_hash="old",
                event_sequence=1,
            )
        )
        s.flush()
        s.add(Run(id="legacy-run", case_id="legacy", triggering_event_id="legacy-event", state="COMPLETE"))
    assert activate(session_factory, policy, settings, apply=True, attest_writers_stopped=True)["ready"]
    with session_factory() as s:
        assert s.execute(
            text("SELECT configuration_revision,policy_bundle_hash FROM runs WHERE id='legacy-run'")
        ).one() == (None, None)


def test_apply_refuses_concurrent_legacy_writer_without_partial_seed(session_factory, policy, settings):
    with session_factory() as writer:
        writer.execute(text("UPDATE broker_entities SET notes='uncommitted legacy change'"))
        with pytest.raises(ConfigurationUnavailable):
            activate(session_factory, policy, settings, apply=True, attest_writers_stopped=True)
    assert counts(session_factory) == (0, 0, 0)


def test_readonly_preflight_does_not_block_unrelated_legacy_writer(session_factory, policy, settings):
    with session_factory() as writer:
        writer.execute(text("UPDATE broker_entities SET notes='uncommitted legacy change'"))
        assert activate(session_factory, policy, settings)["ready"]
    assert counts(session_factory) == (0, 0, 0)


def test_activation_refuses_admission_that_already_observed_legacy_mode(session_factory, policy, settings):
    with session_factory() as admission:
        assert repo().get_active(admission, lock=True) is None
        with pytest.raises(ConfigurationUnavailable):
            activate(session_factory, policy, settings, apply=True, attest_writers_stopped=True)
    assert counts(session_factory) == (0, 0, 0)


def test_activation_commit_failure_is_translated_without_partial_activation(
    session_factory, policy, settings
):
    def before_commit(session):
        session.execute(
            text(
                "CREATE TEMP TABLE failing_activation_commit "
                "(id bigint PRIMARY KEY, parent bigint REFERENCES failing_activation_commit(id) "
                "DEFERRABLE INITIALLY DEFERRED) ON COMMIT DROP"
            )
        )
        session.execute(text("INSERT INTO failing_activation_commit VALUES (1,2)"))

    def failing_factory():
        session = session_factory()
        event.listen(session, "before_commit", before_commit)
        return session

    with pytest.raises(ConfigurationUnavailable):
        activate(failing_factory, policy, settings, apply=True, attest_writers_stopped=True)
    assert counts(session_factory) == (0, 0, 0)


@pytest.mark.parametrize("token", [True, 1, ["admin"], {"token": "admin"}])
def test_malformed_admin_setting_refuses_cleanly(session_factory, policy, settings, token):
    settings = settings.model_copy(update={"enforce_bundle_pinning": True, "ui_admin_token": token})
    with pytest.raises(ConfigurationInvalid):
        ops().activate(
            session_factory,
            policy=policy,
            settings=settings,
            actor="operator",
            apply=True,
            attest_writers_stopped=True,
        )
    assert counts(session_factory) == (0, 0, 0)


def cli(url, *args):
    return subprocess.run(
        [sys.executable, "-m", "kyc_tool.ops.activate_live_configuration", *args],
        env={
            **os.environ,
            "KYC_DATABASE_URL": url,
            "KYC_ENFORCE_BUNDLE_PINNING": "true",
            "KYC_UI_ADMIN_TOKEN": "disposable-test-admin",
            "KYC_ENVIRONMENT": "development",
        },
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_shadow_search_path_cannot_redirect_cli_activation(engine, migrated):
    with engine.begin() as c:
        c.execute(text("CREATE SCHEMA configuration_shadow"))
        c.execute(
            text(
                "CREATE TABLE configuration_shadow.configuration_state "
                "(LIKE public.configuration_state INCLUDING ALL)"
            )
        )
        c.execute(
            text(
                "CREATE TABLE configuration_shadow.broker_entities "
                "(LIKE public.broker_entities INCLUDING ALL)"
            )
        )
        expected_ids = set(c.execute(text("SELECT id FROM public.broker_entities")).scalars())
    try:
        url = (
            migrated
            + ("&" if "?" in migrated else "?")
            + "options="
            + quote("-csearch_path=configuration_shadow,public")
        )
        result = cli(url, "--apply", "--attest-writers-stopped")
        assert result.returncode == 0, result.stdout + result.stderr
        with engine.connect() as c:
            assert c.execute(text("SELECT count(*) FROM public.configuration_state")).scalar_one() == 1
            assert (
                c.execute(text("SELECT count(*) FROM configuration_shadow.configuration_state")).scalar_one()
                == 0
            )
            brokers = c.execute(text("SELECT brokers_json FROM public.configuration_revisions")).scalar_one()
            assert {b["id"] for b in brokers} == expected_ids
    finally:
        with engine.begin() as c:
            c.execute(text("DROP SCHEMA configuration_shadow CASCADE"))


@pytest.mark.parametrize("stamp", ["023", "999"])
def test_wrong_schema_cli_refuses_before_activation(engine, migrated, session_factory, stamp):
    with engine.begin() as c:
        c.execute(text("UPDATE public.alembic_version SET version_num=:stamp"), {"stamp": stamp})
    try:
        result = cli(migrated, "--apply", "--attest-writers-stopped")
        assert result.returncode != 0
        assert "OPS_COMMAND_SCHEMA_REFUSED" in result.stderr
        assert "Traceback" not in result.stderr
        assert counts(session_factory) == (0, 0, 0)
    finally:
        with engine.begin() as c:
            c.execute(text("UPDATE public.alembic_version SET version_num='024'"))


def test_legacy_opaque_id_survives_activation_noop_and_replay(session_factory, policy, settings):
    with uow(session_factory) as s:
        s.execute(
            text(
                "INSERT INTO broker_entities (id,name,policy,notes) "
                "VALUES (' stable-id ','Opaque Identity Ltd','allowed','identity witness')"
            )
        )
    activate(session_factory, policy, settings, apply=True, attest_writers_stopped=True)
    with session_factory() as s:
        active = repo().get_active(s)
        opaque = next(b for b in active.brokers if b.name == "Opaque Identity Ltd")
        assert opaque.id == " stable-id "
        assert opaque.notes == "identity witness"
        assert (
            s.execute(text("SELECT id FROM broker_entities WHERE name='Opaque Identity Ltd'")).scalar_one()
            == opaque.id
        )
    value = brokers_json(active.brokers)
    request_id = uuid4()
    with uow(session_factory) as s:
        result = repo().save_section(
            s,
            section="brokers",
            expected_revision=active.revision,
            request_id=request_id,
            value=value,
            actor="operator",
        )
    assert result["changed"] is False
    before = counts(session_factory)
    with uow(session_factory) as s:
        replay = repo().save_section(
            s,
            section="brokers",
            expected_revision=active.revision,
            request_id=request_id,
            value=value,
            actor="operator",
        )
    assert replay == result | {"replayed": True}
    assert counts(session_factory) == before
    with session_factory() as s:
        saved = repo().get_active(s)
        assert saved.revision == active.revision
        assert next(b.id for b in saved.brokers if b.name == "Opaque Identity Ltd") == " stable-id "
