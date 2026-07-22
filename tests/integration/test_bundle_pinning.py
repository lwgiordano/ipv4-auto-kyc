# test_bundle_pinning.py — attestation + corrupt-startup at the REAL worker seam.
# structlog uses the default PrintLoggerFactory (api/app.py:46 sets NO logger_factory),
# so events bypass stdlib `logging` and `caplog` sees ZERO records —
# `structlog.testing.capture_logs()` is the sink (event dict keyed by `event` + kwargs).
# Both proofs drive `pipeline_worker.build_worker()` (the actual startup path), not a
# test-authored seed_and_verify/attest ordering.
import pytest
import structlog
from sqlalchemy import text

from kyc_tool.policy_store import repo as store
from tests.integration._bundle_helpers import bundle_x


def test_pipeline_worker_startup_attests_and_refuses_corrupt(
        session_factory, settings, engine, clean_db, post_event, monkeypatch):
    from kyc_tool.workers import pipeline_worker
    # SUCCESS (both flag states): build_worker() seeds → verifies → attests, returns a Worker
    for flag in (False, True):
        cfg = settings.model_copy(update={"enforce_bundle_pinning": flag})
        monkeypatch.setattr(pipeline_worker, "get_settings", lambda cfg=cfg: cfg)
        with structlog.testing.capture_logs() as logs:
            worker = pipeline_worker.build_worker()
        assert worker is not None
        rec = next(r for r in logs if r.get("event") == "bundle_pinning_ready")
        assert (rec["flag"] is flag and rec["engine_build_id"] == "eng-1"
                and rec["bundle_hash"] == bundle_x().bundle_hash)   # §8.18 deployment witness
    # CORRUPT process-bundle row (the single seeded row) → build_worker RAISES before
    # returning a Worker; nothing attested AND the queued job stays WHOLLY unclaimed (§8.2).
    with engine.begin() as c:
        c.execute(text("UPDATE policy_bundles SET files_json = jsonb_set("
                       "files_json,'{scoring_rubric.json}','\"%%%\"')"))
    post_event("c-corrupt", "email.verified",
               {"email": "o@acme.test", "domain": "acme.test", "verified_at": "2026-07-21T00:00:00Z"})
    cfg = settings.model_copy(update={"enforce_bundle_pinning": False})
    monkeypatch.setattr(pipeline_worker, "get_settings", lambda cfg=cfg: cfg)
    with structlog.testing.capture_logs() as logs, pytest.raises(store.BundleCorrupt):
        pipeline_worker.build_worker()                             # aborts before returning a Worker
    assert not [r for r in logs if r.get("event") == "bundle_pinning_ready"]
    with session_factory() as s:
        job = s.execute(text("SELECT status, attempts, locked_by FROM jobs "
                             "WHERE case_id='c-corrupt'")).one()
        assert job.status == "queued" and job.attempts == 0 and job.locked_by is None  # unclaimed


# --- Task 7: resolve-at-entry refuses BEFORE any side effect ---------------


def test_flag_on_absent_bundle_dead_letters_zero_side_effects(
        session_factory, policy, settings, tmp_path, engine, clean_db, post_event):
    from sqlalchemy import text

    from kyc_tool.adapters.base import AdapterOutput, hash_inputs
    from kyc_tool.domain.models import AdapterStatus
    from kyc_tool.orchestration.pipeline import Pipeline
    from kyc_tool.queue.worker import Worker
    from kyc_tool.storage.object_store import FsStore

    calls: list[str] = []
    class _CountingPoc:                                   # adapter protocol (adapters/base.py)
        adapter_id = "rir_poc"
        def input_hash(self, snapshot, event):
            return hash_inputs(self.adapter_id, event)
        def run(self, snapshot, event) -> AdapterOutput:
            calls.append(self.adapter_id)                 # a real call would mint token+email
            return AdapterOutput(self.adapter_id, AdapterStatus.NOT_APPLICABLE)

    # ingest a run under the real policy (records its bundle_hash on the run)...
    post_event("case-absent", "poc.submitted", {"rir": "arin", "poc_handle": "POC-ACME"})
    # ...then point the run at a NOT-seeded hash + flag on
    with engine.begin() as c:
        c.execute(text("UPDATE runs SET policy_bundle_hash='deadbeef' WHERE case_id='case-absent'"))
        c.execute(text("TRUNCATE policy_bundles CASCADE"))
    pinned = settings.model_copy(update={"enforce_bundle_pinning": True})
    pl = Pipeline(session_factory, policy, FsStore(tmp_path/"e"), pinned,
                  adapters={"rir_poc": _CountingPoc()})
    Worker(session_factory, {"run_transition": pl.handle_job},
           backoff_base_seconds=0, on_dead_letter=pl.on_dead_letter).run_until_idle()
    with session_factory() as s:
        for tbl in ("review_tasks","poc_tokens","outbox","checks","decisions"):   # case_id-keyed tables
            n = s.execute(text(f"SELECT count(*) FROM {tbl} WHERE case_id='case-absent'")).scalar_one()
            assert n == 0, f"{tbl} had side effects on an absent-bundle run"
        ar = s.execute(text("SELECT count(*) FROM adapter_results ar JOIN runs r ON r.id=ar.run_id "
                            "WHERE r.case_id='case-absent'")).scalar_one()   # adapter_results is run_id-keyed
        assert ar == 0, "adapter_results had side effects on an absent-bundle run"
        dead = s.execute(text("SELECT count(*) FROM jobs WHERE status='dead'")).scalar_one()
    assert calls == [], "rir_poc was called before the absent-bundle refusal"  # §8.7 zero adapter calls
    assert dead >= 1


# --- Task 8: rubric-pinned decision-time scoring (every live check) --------


def test_mixed_era_reprices_score_gate_and_callback(
        session_factory, policy, settings, tmp_path, engine, clean_db,
        post_event, publisher, callback_capture):
    from sqlalchemy import text

    from kyc_tool.checkstore import repo as checkstore
    from kyc_tool.domain.models import CheckStatus
    from kyc_tool.orchestration.pipeline import Pipeline
    from kyc_tool.policy_store import repo as store
    from kyc_tool.queue.worker import Worker
    from kyc_tool.storage.object_store import FsStore
    from tests.integration._bundle_helpers import raw_x
    T = "verified_email"                                     # §8.8b example type
    assert policy.rubric.item(T).points == 10 and policy.rubric.item(T).category == "account_access"
    with session_factory() as s:
        store.store_bundle(s, raw_x())
        s.execute(text("INSERT INTO cases (id) VALUES ('case-mix')"))
        checkstore.write_check(s, case_id="case-mix", check_type=T, status=CheckStatus.PASS,
                               points_awarded=83, category="control_proof", source="seed")  # stale era
        s.commit()

    def _recalc(flag: bool):
        post_event("case-mix", "recalculate.requested", {})  # run pinned to app bundle X
        cfg = settings.model_copy(update={"enforce_bundle_pinning": flag})
        pl = Pipeline(session_factory, policy, FsStore(tmp_path / f"e{flag}"), cfg, adapters={})
        Worker(session_factory, {"run_transition": pl.handle_job}, backoff_base_seconds=0,
               on_dead_letter=pl.on_dead_letter).run_until_idle()
        with session_factory() as s:
            r = s.execute(text("SELECT score, gates_json FROM decisions WHERE case_id='case-mix' "
                               "ORDER BY decided_at DESC LIMIT 1")).one()
        return r.score, r.gates_json

    on_score, on_gates = _recalc(flag=True)
    assert on_score == 10 and on_gates["control_proof"] is False    # re-priced; gate flips off
    off_score, off_gates = _recalc(flag=False)
    assert off_score == 83 and off_gates["control_proof"] is True    # stamped; gate holds
    # callback checks-summary uses the SAME repriced views: a final flag-on run's body
    # (pipeline._callback_body, pipeline.py:510-519 → {"type","points": pts if PASS else 0}).
    post_event("case-mix", "recalculate.requested", {})
    cfg = settings.model_copy(update={"enforce_bundle_pinning": True})
    pl = Pipeline(session_factory, policy, FsStore(tmp_path / "ecb"), cfg, adapters={})
    Worker(session_factory, {"run_transition": pl.handle_job}, backoff_base_seconds=0,
           on_dead_letter=pl.on_dead_letter).run_until_idle()
    publisher.process_pending()
    checks = callback_capture.requests[-1]["body"]["checks"]
    assert any(c["type"] == T and c["points"] == 10 for c in checks)  # re-priced points in callback
