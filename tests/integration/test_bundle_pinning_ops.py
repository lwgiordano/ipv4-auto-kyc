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
from tests.conftest import seed_automatic_decision
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

    # Truth table over (decision.engine_build_id, run.engine_build_id): the runs
    # surface must key off the RUN's OWN stamp, independent of the decision's
    # (AUDIT P2). Each group is its own case+event+run (FK) [+ decision].
    with engine.begin() as c:
        # (a) decision stamped eng-1 / run engine NULL ⇒ run flagged, decision NOT
        c.execute(text("INSERT INTO cases (id) VALUES ('c-a')"))
        c.execute(
            text(
                "INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
                "actor_json, payload_json, event_sequence) VALUES "
                "('e-a','c-a','k-a','ph','email.verified','{}'::jsonb,'{}'::jsonb,1)"
            )
        )  # event_sequence NN (008)
        c.execute(
            text(
                "INSERT INTO runs (id, case_id, triggering_event_id, state) "
                "VALUES ('r-a','c-a','e-a','QUEUED')"
            )
        )  # engine_build_id omitted ⇒ NULL
        seed_automatic_decision(c, case_id="c-a", run_id="r-a", decision_id="d-a", seq=1,
                                engine_build_id="eng-1")   # (a) decision stamped / run NULL

        # (b) decision engine NULL / run stamped eng-1 ⇒ decision flagged, run NOT
        c.execute(text("INSERT INTO cases (id) VALUES ('c-b')"))
        c.execute(
            text(
                "INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
                "actor_json, payload_json, event_sequence) VALUES "
                "('e-b','c-b','k-b','ph','email.verified','{}'::jsonb,'{}'::jsonb,1)"
            )
        )
        c.execute(
            text(
                "INSERT INTO runs (id, case_id, triggering_event_id, state, engine_build_id) "
                "VALUES ('r-b','c-b','e-b','QUEUED','eng-1')"
            )
        )
        seed_automatic_decision(c, case_id="c-b", run_id="r-b", decision_id="d-b", seq=1)
        # (b) decision engine NULL (helper default) / run stamped eng-1

        # (c) both stamped eng-1 ⇒ neither flagged
        c.execute(text("INSERT INTO cases (id) VALUES ('c-c')"))
        c.execute(
            text(
                "INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
                "actor_json, payload_json, event_sequence) VALUES "
                "('e-c','c-c','k-c','ph','email.verified','{}'::jsonb,'{}'::jsonb,1)"
            )
        )
        c.execute(
            text(
                "INSERT INTO runs (id, case_id, triggering_event_id, state, engine_build_id) "
                "VALUES ('r-c','c-c','e-c','QUEUED','eng-1')"
            )
        )
        seed_automatic_decision(c, case_id="c-c", run_id="r-c", decision_id="d-c", seq=1,
                                engine_build_id="eng-1")   # (c) both stamped

        # (d) queued run, NO decision ⇒ neither (the join with decisions excludes it)
        c.execute(text("INSERT INTO cases (id) VALUES ('c-d')"))
        c.execute(
            text(
                "INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
                "actor_json, payload_json, event_sequence) VALUES "
                "('e-d','c-d','k-d','ph','email.verified','{}'::jsonb,'{}'::jsonb,1)"
            )
        )
        c.execute(
            text(
                "INSERT INTO runs (id, case_id, triggering_event_id, state) "
                "VALUES ('r-d','c-d','e-d','QUEUED')"
            )
        )  # engine_build_id omitted ⇒ NULL; no decision references it

        # checks surface: NULL policy_bundle_hash flagged; non-null NOT flagged
        c.execute(
            text(
                "INSERT INTO checks (id, case_id, check_type, status, points_awarded, category, "
                "source) VALUES ('k-al','c-a','verified_email','pass',10,'account_access','seed')"
            )
        )  # created_at=now()>epoch, policy_bundle_hash NULL
        c.execute(
            text(
                "INSERT INTO checks (id, case_id, check_type, status, points_awarded, category, "
                "source, policy_bundle_hash) VALUES "
                "('k-ok','c-b','verified_email','pass',10,'account_access','seed','pinned-hash')"
            )
        )  # created_at=now()>epoch, policy_bundle_hash present ⇒ NOT flagged
        # (distinct case_id than k-al: uq_checks_live_per_type is unique per case_id+check_type)

    with session_factory() as s:
        alert = post_epoch_null_provenance(s)
    assert "r-a" in alert["runs"] and "r-b" not in alert["runs"]
    assert "r-c" not in alert["runs"] and "r-d" not in alert["runs"]
    assert "d-b" in alert["decisions"] and "d-a" not in alert["decisions"]
    assert "d-c" not in alert["decisions"]
    assert "k-al" in alert["checks"]  # NULL policy_bundle_hash flagged
    assert "k-ok" not in alert["checks"]  # non-null policy_bundle_hash NOT flagged


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


# --- P3 audit fix: create_app() must attest the SELECTED policy (injected or
# on-disk) as the single startup identity, never settings.policy_dir when it
# can differ from what the app actually serves ---


def test_create_app_attests_injected_policy_not_settings_dir(
        session_factory, settings, engine, clean_db, tmp_path, monkeypatch):
    import structlog
    from fastapi.testclient import TestClient

    from kyc_tool.api.app import create_app
    from tests.integration._bundle_helpers import make_bundle_y
    # settings points at X (normative); inject a DIFFERENT on-disk bundle Y
    ydir, by = make_bundle_y(tmp_path, threshold=101)
    assert by.bundle_hash != bundle_x().bundle_hash and by.policy_dir is not None
    # create_app() unconditionally calls structlog.configure() with a brand-new
    # processor list on every invocation (pre-existing, unrelated to this fix);
    # neutralize it here so it doesn't clobber capture_logs()'s own capture setup,
    # which relies on mutating the CURRENT processors list in place.
    monkeypatch.setattr(structlog, "configure", lambda *a, **k: None)
    with structlog.testing.capture_logs() as logs:
        app = create_app(settings, session_factory=session_factory, policy=by)
    # served identity == Y, attestation names Y (not X), and Y is now seeded/loadable
    assert app.state.policy.bundle_hash == by.bundle_hash
    rec = next(r for r in logs if r.get("event") == "bundle_pinning_ready")
    assert rec["bundle_hash"] == by.bundle_hash
    assert TestClient(app).get("/readyz").status_code == 200
    with session_factory() as s:
        assert store.load_bundle(s, by.bundle_hash) is not None   # Y (served) was seeded


def test_create_app_db_reconstructed_policy_requires_store_presence(
        session_factory, settings, engine, clean_db, monkeypatch):
    from kyc_tool.api.app import create_app
    from kyc_tool.policy.loader import build_bundle, read_policy_files
    recon = build_bundle(read_policy_files(settings.policy_dir), policy_dir=None)  # policy_dir None
    assert recon.policy_dir is None and recon.bundle_hash == bundle_x().bundle_hash
    # (a) absent from store -> boot fails
    with pytest.raises(RuntimeError):
        create_app(settings, session_factory=session_factory, policy=recon)
    # (b) present in store -> boots, attests, /readyz 200
    with session_factory() as s:
        store.store_bundle(s, read_policy_files(settings.policy_dir))
        s.commit()
    import structlog
    # neutralize create_app()'s internal structlog.configure() call (see comment in
    # test_create_app_attests_injected_policy_not_settings_dir above) so capture_logs()
    # actually sees the event.
    monkeypatch.setattr(structlog, "configure", lambda *a, **k: None)
    with structlog.testing.capture_logs() as logs:
        create_app(settings, session_factory=session_factory, policy=recon)
    rec = next(r for r in logs if r.get("event") == "bundle_pinning_ready")
    assert rec["bundle_hash"] == recon.bundle_hash


def test_create_app_db_reconstructed_policy_corrupt_row_fails_boot(
        session_factory, settings, engine, clean_db):
    from kyc_tool.api.app import create_app
    from kyc_tool.policy.loader import build_bundle, read_policy_files
    recon = build_bundle(read_policy_files(settings.policy_dir), policy_dir=None)
    with session_factory() as s:
        store.store_bundle(s, read_policy_files(settings.policy_dir))
        s.commit()
    with engine.begin() as c:  # corrupt the stored row so load_bundle raises BundleCorrupt
        c.execute(text("UPDATE policy_bundles SET files_json = jsonb_set("
                        "files_json,'{scoring_rubric.json}','\"%%%\"') WHERE bundle_hash=:h"),
                  {"h": recon.bundle_hash})
    with pytest.raises(store.BundleCorrupt):
        create_app(settings, session_factory=session_factory, policy=recon)
