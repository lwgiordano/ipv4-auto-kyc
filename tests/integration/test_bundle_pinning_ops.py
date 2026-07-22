"""PR 6 §7, §8.11-15: the /readyz process-bundle check, the three ops
one-shot CLIs (verify_pinnable_backlog, seed_policy_bundle,
activate_bundle_pinning_epoch), the post-epoch NULL-provenance alert, and
the activation-recovery / flag-off-rollback / activation-identity-gate
scenarios these ops exist to support."""

import sys

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from kyc_tool.ops import activate_bundle_pinning_epoch as epoch_cli
from kyc_tool.ops.requeue_interrupted_jobs import requeue_interrupted
from kyc_tool.ops.seed_policy_bundle import seed_policy_bundle
from kyc_tool.ops.verify_pinnable_backlog import verify_pinnable_backlog
from kyc_tool.orchestration.pipeline import Pipeline
from kyc_tool.policy_store import repo as store
from kyc_tool.queue.worker import Worker
from kyc_tool.storage.object_store import FsStore
from tests.integration._bundle_helpers import bundle_x, raw_x


def _queue_run_transition(engine, case, run_id, bundle_hash, status="queued", kind="run_transition"):
    with engine.begin() as c:
        c.execute(text("INSERT INTO cases (id) VALUES (:c) ON CONFLICT DO NOTHING"), {"c": case})
        ev = f"{run_id}-ev"
        c.execute(
            text(
                "INSERT INTO events (id, case_id, idempotency_key, payload_hash, "
                "event_type, actor_json, payload_json, event_sequence) VALUES (:e,:c,:e,'ph',"
                "'recalculate.requested','{}'::jsonb,'{}'::jsonb,1)"
            ),
            {"e": ev, "c": case},
        )  # event_sequence NN (008)
        c.execute(
            text(
                "INSERT INTO runs (id, case_id, triggering_event_id, state, "
                "policy_bundle_hash) VALUES (:r,:c,:e,'QUEUED',:h)"
            ),
            {"r": run_id, "c": case, "e": ev, "h": bundle_hash},
        )
        c.execute(
            text(
                "INSERT INTO jobs (kind, payload_json, status, case_id, attempts) "
                "VALUES (:k, CAST(:p AS jsonb), :s, :c, 0)"
            ),
            {"k": kind, "p": f'{{"run_id":"{run_id}"}}', "s": status, "c": case},
        )


def test_verify_pinnable_backlog_flags_null_absent_corrupt(session_factory, engine, clean_db):
    with session_factory() as s:
        good = store.store_bundle(s, raw_x())
        s.commit()
    with engine.begin() as c:  # a corrupt bundle row
        c.execute(
            text(
                "INSERT INTO policy_bundles (bundle_hash, files_json) "
                'VALUES (\'corrupt\', \'{"scoring_rubric.json":"%%%"}\'::jsonb)'
            )
        )
    _queue_run_transition(engine, "c-ok", "r-ok", good)  # loads → OK
    _queue_run_transition(engine, "c-null", "r-null", None)  # NULL → flagged
    _queue_run_transition(engine, "c-abs", "r-abs", "0" * 64)  # absent → flagged
    _queue_run_transition(engine, "c-cor", "r-cor", "corrupt")  # corrupt → flagged
    _queue_run_transition(engine, "c-dead", "r-dead", "0" * 64, status="dead")  # dead-requeueable → flagged
    _queue_run_transition(engine, "c-done", "r-done", None, status="done")  # terminal → NOT flagged
    _queue_run_transition(engine, "c-oth", "r-oth", None, kind="outbox_delivery")  # non-pipeline → NOT flag
    assert set(verify_pinnable_backlog(session_factory)) == {"r-null", "r-abs", "r-cor", "r-dead"}


def test_seed_policy_bundle_stores_on_match_no_row_on_mismatch(session_factory, engine, clean_db):
    from kyc_tool.config import REPO_ROOT

    d = REPO_ROOT / "KYC_Tool_Build_Package" / "machine_readable"
    with pytest.raises(store.BundleCorrupt):
        seed_policy_bundle(session_factory, d, expect_hash="0" * 64)  # mismatch
    with session_factory() as s:
        assert s.execute(text("SELECT count(*) FROM policy_bundles")).scalar_one() == 0
    seed_policy_bundle(session_factory, d, expect_hash=bundle_x().bundle_hash)  # match
    with session_factory() as s:
        assert s.execute(text("SELECT count(*) FROM policy_bundles")).scalar_one() == 1


def test_readyz_503_when_process_bundle_absent(client, engine, clean_db):
    assert client.get("/readyz").status_code == 200  # app seeded at startup
    with engine.begin() as c:
        c.execute(text("TRUNCATE policy_bundles CASCADE"))
    assert client.get("/readyz").status_code == 503


def test_activation_recovery_requeues_then_scores_under_pinning(
    session_factory, policy, settings, tmp_path, engine, clean_db
):
    # §8.12: a final-attempt running job (crash during flag-on) is requeued after all
    # workers are confirmed stopped, THEN a flag-ON worker scores it under the pin —
    # not merely requeued. The run is pinned to a REAL seeded bundle so resolve succeeds.
    h = bundle_x().bundle_hash
    with session_factory() as s:
        store.store_bundle(s, raw_x())
        s.commit()
    _queue_run_transition(engine, "c-rec", "r-rec", h)  # QUEUED run pinned to the seeded bundle
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO checks (id, case_id, check_type, status, points_awarded, "
                "category, source) VALUES ('k-rec','c-rec','verified_email','pass',10,"
                "'account_access','seed')"
            )
        )  # a live check to score
        c.execute(
            text(
                "UPDATE jobs SET status='running', attempts=max_attempts, "  # crash mid-final-attempt
                "locked_by='dead-worker' WHERE case_id='c-rec'"
            )
        )
    assert requeue_interrupted(session_factory) == 1  # run AFTER all workers stopped
    with session_factory() as s:
        row = s.execute(
            text("SELECT status, attempts, max_attempts FROM jobs WHERE case_id='c-rec'")
        ).one()
        assert row.status == "queued" and row.attempts == row.max_attempts - 1  # forced-stop attempt returned
        assert s.execute(text("SELECT count(*) FROM jobs WHERE status='running'")).scalar_one() == 0
    cfg = settings.model_copy(update={"enforce_bundle_pinning": True})  # flag-ON restart resumes it
    pl = Pipeline(session_factory, policy, FsStore(tmp_path / "e"), cfg, adapters={})
    Worker(
        session_factory,
        {"run_transition": pl.handle_job},
        backoff_base_seconds=0,
        on_dead_letter=pl.on_dead_letter,
    ).run_until_idle()
    with session_factory() as s:
        assert (
            s.execute(text("SELECT engine_build_id FROM decisions WHERE case_id='c-rec'")).scalar_one()
            == "eng-1"
        )  # scored under pinning
        assert (
            s.execute(
                text("SELECT count(*) FROM jobs WHERE case_id='c-rec' AND status='dead'")
            ).scalar_one()
            == 0
        )  # not dead-lettered


def test_rollback_inflight_job_requeued_then_flag_off_decides_once(
    session_factory, policy, settings, tmp_path, engine, clean_db, post_event
):
    # §8.13: a final-attempt in-flight job during a flag-on→flag-off rollback is
    # requeued (confirmed-zero workers), and the flag-OFF restart on the PR6 image
    # decides it EXACTLY once — creation pin intact, resolved process-bundle + engine
    # provenance written (no post-epoch NULL).
    bx = bundle_x()
    with session_factory() as s:
        store.store_bundle(s, raw_x())
        s.commit()
    post_event(
        "c-rb",
        "email.verified",
        {"email": "o@acme.test", "domain": "acme.test", "verified_at": "2026-07-21T00:00:00Z"},
    )
    with engine.begin() as c:  # in-flight on its final attempt
        c.execute(
            text(
                "UPDATE jobs SET status='running', attempts=max_attempts, "
                "locked_by='dead-worker' WHERE case_id='c-rb'"
            )
        )
    assert requeue_interrupted(session_factory) == 1  # after all flag-on workers stopped
    off = settings.model_copy(update={"enforce_bundle_pinning": False})
    pl = Pipeline(session_factory, policy, FsStore(tmp_path / "e"), off, adapters={})  # flag-OFF restart
    Worker(
        session_factory,
        {"run_transition": pl.handle_job},
        backoff_base_seconds=0,
        on_dead_letter=pl.on_dead_letter,
    ).run_until_idle()
    with session_factory() as s:
        assert (
            s.execute(text("SELECT count(*) FROM decisions WHERE case_id='c-rb'")).scalar_one() == 1
        )  # decided EXACTLY once
        run = s.execute(
            text("SELECT policy_bundle_hash, engine_build_id FROM runs WHERE case_id='c-rb'")
        ).first()
        assert run.policy_bundle_hash == bx.bundle_hash and run.engine_build_id == "eng-1"  # pin intact
        dec = s.execute(
            text("SELECT policy_shas, engine_build_id FROM decisions WHERE case_id='c-rb'")
        ).first()
        assert dec.policy_shas == bx.shas and dec.engine_build_id == "eng-1"  # process-bundle provenance


def test_post_epoch_null_alert(session_factory, engine, clean_db):
    from kyc_tool.ops.activate_bundle_pinning_epoch import post_epoch_null_provenance

    # no epoch row yet → nothing to alert on (early-return branch)
    with session_factory() as s:
        assert post_epoch_null_provenance(s) == {"decisions": [], "checks": [], "runs": []}

    with session_factory() as s:
        h = store.store_bundle(s, raw_x())
        store.activate_epoch(s, expect_bundle_hash=h, expect_engine="eng-1")
        s.commit()
    with engine.begin() as c:
        c.execute(text("INSERT INTO cases (id) VALUES ('c-al')"))
        # a run the flagged decision references — exercises the RUNS surface (was vacuous)
        c.execute(
            text(
                "INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
                "actor_json, payload_json, event_sequence) VALUES ('e-flag','c-al','kf','ph',"
                "'email.verified','{}'::jsonb,'{}'::jsonb,1)"
            )
        )
        c.execute(
            text(
                "INSERT INTO runs (id, case_id, triggering_event_id, state) "
                "VALUES ('r-flag','c-al','e-flag','QUEUED')"
            )
        )
        # post-epoch decision missing engine_build_id, WITH run_id → flags decisions + runs
        c.execute(
            text(
                "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
                "buy_enablement, policy_shas, manual) VALUES ('d-al','c-al','r-flag','x',0,"
                "'{}'::jsonb,'buy_locked_org_id_required','{}'::jsonb,false)"
            )
        )  # decided_at=now()>epoch, engine NULL
        # post-epoch check missing policy_bundle_hash → exercises the CHECKS surface (was untested)
        c.execute(
            text(
                "INSERT INTO checks (id, case_id, check_type, status, points_awarded, category, "
                "source) VALUES ('k-al','c-al','verified_email','pass',10,'account_access','seed')"
            )
        )  # created_at=now()>epoch, policy_bundle_hash NULL
        # a legit queued run with NO decision → must NOT appear in runs
        c.execute(
            text(
                "INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
                "actor_json, payload_json, event_sequence) VALUES ('e-q','c-al','kq','ph',"
                "'email.verified','{}'::jsonb,'{}'::jsonb,2)"
            )
        )  # events.event_sequence NN since migration 008
        c.execute(
            text(
                "INSERT INTO runs (id, case_id, triggering_event_id, state) "
                "VALUES ('r-q','c-al','e-q','QUEUED')"
            )
        )
    with session_factory() as s:
        alert = post_epoch_null_provenance(s)
    assert "d-al" in alert["decisions"]  # decisions surface
    assert "k-al" in alert["checks"]  # checks surface (was untested)
    assert "r-flag" in alert["runs"]  # runs surface positive (was vacuous)
    assert "r-q" not in alert["runs"]  # a run with no post-epoch NULL-engine decision


# §8.15 activation identity gate — three runnable rejections (each pins a guard whose
# removal would otherwise leave the suite green):


def test_activate_cli_refuses_valid_but_wrong_bundle(
    session_factory, settings, tmp_path, clean_db, monkeypatch
):
    # Both X and Y are SEEDED (both loadable). The running process is X
    # (settings.policy_dir = normative). Activating a valid, seeded Y must be refused
    # by the CLI's LOCAL-bundle comparison — NOT by store-absence. Deleting that
    # comparison would let activate_epoch accept the historical Y, so this pins §8.15.
    from kyc_tool.policy.loader import read_policy_files
    from tests.integration._bundle_helpers import make_bundle_y

    ydir, by = make_bundle_y(tmp_path, threshold=101)
    with session_factory() as s:
        store.store_bundle(s, raw_x())
        store.store_bundle(s, read_policy_files(ydir))
        s.commit()
    monkeypatch.setattr(epoch_cli, "get_settings", lambda: settings)  # local bundle = X
    monkeypatch.setattr(
        sys, "argv", ["prog", "--expect-bundle-hash", by.bundle_hash, "--expect-engine", "eng-1"]
    )
    with pytest.raises((SystemExit, store.BundleCorrupt)):
        epoch_cli.main()
    with session_factory() as s:
        assert store.read_epoch(s) is None  # activation refused


def test_activate_cli_refuses_engine_mismatch(session_factory, settings, clean_db, monkeypatch):
    # Local X matches --expect-bundle-hash, but --expect-engine != running ENGINE_BUILD_ID.
    # Driving the CLI (not activate_epoch directly) catches a main() that ignores
    # --expect-engine and always passes ENGINE_BUILD_ID (which would write the epoch).
    with session_factory() as s:
        store.store_bundle(s, raw_x())
        s.commit()
    monkeypatch.setattr(epoch_cli, "get_settings", lambda: settings)
    monkeypatch.setattr(
        sys,
        "argv",
        ["prog", "--expect-bundle-hash", bundle_x().bundle_hash, "--expect-engine", "eng-999"],
    )
    with pytest.raises((SystemExit, store.BundleCorrupt)):
        epoch_cli.main()
    with session_factory() as s:
        assert store.read_epoch(s) is None  # activation refused


def test_epoch_fk_rejects_unknown_bundle(engine, clean_db):
    # the FK forbids an epoch row that points at a bundle_hash not in policy_bundles.
    with engine.begin() as c, pytest.raises(IntegrityError):
        c.execute(
            text(
                "INSERT INTO bundle_pinning_epoch (id, activated_at, bundle_hash, "
                "engine_build_id) VALUES (1, now(), :h, 'eng-1')"
            ),
            {"h": "0" * 64},
        )
