"""Real PostgreSQL configuration transactions, replay, and corruption refusal."""

import importlib
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from kyc_tool.configuration.models import (
    ConfigurationConflict,
    ConfigurationInvalid,
    ConfigurationRequestConflict,
    ConfigurationUnavailable,
    derive_points,
)
from kyc_tool.db.session import uow
from kyc_tool.policy.loader import read_policy_files
from kyc_tool.policy_store import repo as policy_store

pytestmark = pytest.mark.usefixtures("clean_db")


def repo():
    assert importlib.util.find_spec("kyc_tool.configuration.repo") is not None, (
        "configuration repository is missing"
    )
    return importlib.import_module("kyc_tool.configuration.repo")


def baseline(sf, policy):
    with uow(sf) as s:
        return repo().bootstrap(s, policy=policy, actor="operator")


def save(s, base, **kw):
    return repo().save_section(
        s,
        section="mappings",
        expected_revision=base.revision,
        request_id=kw.pop("request_id", uuid4()),
        value=kw.pop("value", base.mappings | {"KYC_Score__c": "New_Score__c"}),
        actor="operator",
        **kw,
    )


def counts(sf):
    with sf() as s:
        return tuple(
            s.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
            for table in ("configuration_revisions", "configuration_requests", "audit_log")
        )


def test_schema_is_not_activation(session_factory):
    with session_factory() as s:
        assert repo().get_active(s) is None


def test_save_preserves_old_snapshot_and_untouched_sections(session_factory, policy):
    base = baseline(session_factory, policy)
    with uow(session_factory) as s:
        result = save(s, base)
    with session_factory() as s:
        active = repo().get_active(s)
        old = repo().get_revision(s, base.revision)
    assert result == {
        "revision": str(active.revision),
        "current_revision": str(active.revision),
        "changed": True,
        "replayed": False,
    }
    assert active.bundle.bundle_hash == old.bundle.bundle_hash == policy.bundle_hash
    assert active.brokers == old.brokers
    assert active.mappings["KYC_Score__c"] == "New_Score__c"
    assert old.mappings["KYC_Score__c"] == "KYC_Score__c"
    active.mappings["KYC_Score__c"] = "local-mutation"
    with session_factory() as s:
        assert repo().get_active(s).mappings["KYC_Score__c"] == "New_Score__c"


@pytest.mark.parametrize("noop", [False, True])
def test_replay_returns_original_outcome_and_current_pointer(session_factory, policy, noop):
    base = baseline(session_factory, policy)
    request_id = uuid4()
    value = base.mappings if noop else base.mappings | {"KYC_Score__c": "First__c"}
    with uow(session_factory) as s:
        first = save(s, base, request_id=request_id, value=value)
    with uow(session_factory) as s:
        active = repo().get_active(s)
        later = save(s, active, value=active.mappings | {"KYC_Score__c": "Later__c"})
    before = counts(session_factory)
    with uow(session_factory) as s:
        retried = save(s, base, request_id=request_id, value=value)
    assert retried == first | {"replayed": True, "current_revision": later["revision"]}
    assert first["changed"] is not noop
    assert counts(session_factory) == before


def test_request_uuid_reuse_with_other_payload_refuses(session_factory, policy):
    base = baseline(session_factory, policy)
    request_id = uuid4()
    with uow(session_factory) as s:
        save(s, base, request_id=request_id)
    with pytest.raises(ConfigurationRequestConflict), uow(session_factory) as s:
        save(s, base, request_id=request_id, value=base.mappings)


def test_rollback_leaves_no_partial_save_or_success_audit(session_factory, policy):
    base = baseline(session_factory, policy)
    before = counts(session_factory)
    request_id = uuid4()
    with pytest.raises(RuntimeError), uow(session_factory) as s:
        save(s, base, request_id=request_id)
        raise RuntimeError("simulate caller commit failure")
    assert counts(session_factory) == before
    with session_factory() as s:
        assert repo().get_active(s).revision == base.revision
    with uow(session_factory) as s:
        assert save(s, base, request_id=request_id)["replayed"] is False


def test_two_sessions_same_base_only_one_commits(session_factory, policy):
    base = baseline(session_factory, policy)
    started = Event()

    def competitor():
        with uow(session_factory) as s:
            started.set()
            return save(s, base, value=base.mappings | {"KYC_Score__c": "Loser__c"})

    with ThreadPoolExecutor(max_workers=1) as pool:
        with uow(session_factory) as s:
            winner = save(s, base)
            future = pool.submit(competitor)
            assert started.wait(2)
        with pytest.raises(ConfigurationConflict):
            future.result(timeout=10)
    with session_factory() as s:
        assert str(repo().get_active(s).revision) == winner["revision"]
    assert counts(session_factory)[:2] == (2, 1)


def test_points_derive_from_stored_bytes_without_directory(session_factory, policy):
    base = baseline(session_factory, policy)
    with uow(session_factory) as s:
        old = repo().get_active(s)
        assert old.bundle.policy_dir is None
        points = {i.check_type: i.points for i in old.bundle.rubric.items} | {"org_id_match": 0}
        repo().save_section(
            s,
            section="points",
            expected_revision=base.revision,
            request_id=uuid4(),
            value=points,
            actor="operator",
        )
    with session_factory() as s:
        active = repo().get_active(s)
        assert active.bundle.rubric.item("org_id_match").points == 0
        assert active.bundle.rubric.item("org_id_match").cap == 0
        assert active.brokers == base.brokers
        assert active.mappings == base.mappings


def test_missing_pointer_after_history_never_falls_back_or_rebootstraps(session_factory, policy):
    baseline(session_factory, policy)
    with uow(session_factory) as s:
        s.execute(text("DELETE FROM configuration_state"))
    with session_factory() as s, pytest.raises(ConfigurationUnavailable):
        repo().get_active(s)
    with pytest.raises(ConfigurationUnavailable), uow(session_factory) as s:
        repo().bootstrap(s, policy=policy, actor="operator")


def test_corrupt_stored_bundle_fails_closed(session_factory, policy):
    baseline(session_factory, policy)
    with uow(session_factory) as s:
        s.execute(text("UPDATE policy_bundles SET files_json='{}'"))
    with session_factory() as s, pytest.raises(ConfigurationUnavailable):
        repo().get_active(s)


def test_invalid_snapshot_fails_closed_on_resolution(session_factory, policy):
    base = baseline(session_factory, policy)
    with uow(session_factory) as s:
        # Direct insert simulates malformed history produced outside the strict writer.
        s.execute(
            text(
                "INSERT INTO configuration_revisions (parent_revision, policy_bundle_hash, "
                "brokers_json, mappings_json, created_by, change_kind) "
                "VALUES (:r,:h,'[]','{}','operator','mappings')"
            ),
            {"r": base.revision, "h": policy.bundle_hash},
        )
        s.execute(
            text(
                "UPDATE configuration_state SET active_revision=(SELECT max(id) FROM configuration_revisions)"
            )
        )
    with session_factory() as s, pytest.raises(ConfigurationUnavailable):
        repo().get_active(s)


@pytest.mark.parametrize("bad", [True, 1.5, "1", 0, -1])
def test_revision_is_strict_positive_integer(session_factory, policy, bad):
    base = baseline(session_factory, policy)
    with pytest.raises(ConfigurationInvalid), uow(session_factory) as s:
        repo().save_section(
            s,
            section="mappings",
            expected_revision=bad,
            request_id=uuid4(),
            value=base.mappings,
            actor="operator",
        )


def test_corrupt_preexisting_candidate_translates_and_rolls_back(session_factory, policy):
    base = baseline(session_factory, policy)
    value = {i.check_type: i.points for i in policy.rubric.items} | {"org_id_match": 0}
    raw = derive_points(read_policy_files(policy.policy_dir), value)
    with uow(session_factory) as s:
        candidate_hash = policy_store.store_bundle(s, raw)
        s.execute(
            text("UPDATE policy_bundles SET files_json='{}' WHERE bundle_hash=:h"), {"h": candidate_hash}
        )
    before = counts(session_factory)
    with pytest.raises(ConfigurationUnavailable), uow(session_factory) as s:
        repo().save_section(
            s,
            section="points",
            expected_revision=base.revision,
            request_id=uuid4(),
            value=value,
            actor="operator",
        )
    assert counts(session_factory) == before


def test_shared_admission_lock_blocks_save_until_admission_commits(session_factory, policy):
    base = baseline(session_factory, policy)
    with session_factory() as admission:
        assert repo().get_active(admission, lock=True).revision == base.revision
        # Real timeout proves FOR SHARE participates in the writer lock boundary.
        start = monotonic()
        with pytest.raises(ConfigurationUnavailable), uow(session_factory) as saver:
            save(saver, base)
        assert monotonic() - start < 8
    assert counts(session_factory)[:2] == (1, 0)


def test_empty_broker_save_and_point_noop_are_durable(session_factory, policy):
    base = baseline(session_factory, policy)
    with uow(session_factory) as s:
        cleared = repo().save_section(
            s,
            section="brokers",
            expected_revision=base.revision,
            request_id=uuid4(),
            value=[],
            actor="operator",
        )
    with uow(session_factory) as s:
        current = repo().get_active(s)
        assert current.brokers == ()
        noop = repo().save_section(
            s,
            section="points",
            expected_revision=current.revision,
            request_id=uuid4(),
            value={i.check_type: i.points for i in current.bundle.rubric.items},
            actor="operator",
        )
    assert noop["changed"] is False
    assert noop["revision"] == cleared["revision"]
    assert counts(session_factory)[:2] == (2, 2)


def test_actual_deferred_commit_failure_rolls_back_save(session_factory, policy):
    base = baseline(session_factory, policy)
    before = counts(session_factory)
    with pytest.raises(IntegrityError), uow(session_factory) as s:
        s.execute(
            text(
                "CREATE TEMP TABLE failing_commit (id bigint PRIMARY KEY, "
                "parent bigint REFERENCES failing_commit(id) "
                "DEFERRABLE INITIALLY DEFERRED) ON COMMIT DROP"
            )
        )
        save(s, base)
        s.execute(text("INSERT INTO failing_commit VALUES (1,2)"))
        # uow's COMMIT fails at the real deferred FK boundary, not in a mock.
    assert counts(session_factory) == before
    with session_factory() as s:
        assert repo().get_active(s).revision == base.revision
