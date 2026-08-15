# test_bundle_pinning.py — attestation + corrupt-startup at the REAL worker seam.
# structlog uses the default PrintLoggerFactory (api/app.py:47 sets NO logger_factory),
# so events bypass stdlib `logging` and `caplog` sees ZERO records —
# `structlog.testing.capture_logs()` is the sink (event dict keyed by `event` + kwargs).
# Both proofs drive `pipeline_worker.build_worker()` (the actual startup path), not a
# test-authored seed_and_verify/attest ordering.
import hashlib

import pytest
import structlog
from sqlalchemy import text

from kyc_tool.adapters.base import AdapterOutput, hash_inputs
from kyc_tool.checkstore import repo as checkstore
from kyc_tool.config import ProcessRole
from kyc_tool.domain.models import AdapterStatus, CheckStatus
from kyc_tool.orchestration.pipeline import Pipeline
from kyc_tool.policy.loader import POLICY_FILES, read_policy_files
from kyc_tool.policy_store import repo as store
from kyc_tool.queue.worker import Worker
from kyc_tool.storage.object_store import FsStore
from tests.integration._bundle_helpers import bundle_x, make_bundle_y, raw_x
from tests.integration.shared import ACME_KYB

_EMAIL_KYB = {**ACME_KYB, "email": {"email": "ops@acme.example", "domain": "acme.example"}}


class _OkEmailAdapter:                                   # writes a real verified_email check
    adapter_id = "email_verification"

    def input_hash(self, snapshot, event):
        return hash_inputs(self.adapter_id, snapshot.get("email"))

    def run(self, snapshot, event) -> AdapterOutput:
        e = snapshot.get("email")
        return AdapterOutput(self.adapter_id, AdapterStatus.OK, raw=b"{}",
                             normalized={"verified": True, "email": e["email"], "domain": e["domain"]})


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
           backoff_base_seconds=0, on_dead_letter=pl.on_dead_letter,
                  process_role=ProcessRole.PIPELINE_WORKER).run_until_idle()
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
               on_dead_letter=pl.on_dead_letter, process_role=ProcessRole.PIPELINE_WORKER).run_until_idle()
        with session_factory() as s:
            r = s.execute(text("SELECT score, gates_json FROM decisions WHERE case_id='case-mix' "
                               "ORDER BY decided_at DESC LIMIT 1")).one()
        return r.score, r.gates_json

    on_score, on_gates = _recalc(flag=True)
    assert on_score == 10 and on_gates["control_proof"] is False    # re-priced; gate flips off
    off_score, off_gates = _recalc(flag=False)
    assert off_score == 83 and off_gates["control_proof"] is True    # stamped; gate holds
    # callback checks-summary uses the SAME repriced views: a final flag-on run's body
    # (pipeline._callback_body, pipeline.py:570-597 → {"type","points": pts if PASS else 0}).
    post_event("case-mix", "recalculate.requested", {})
    cfg = settings.model_copy(update={"enforce_bundle_pinning": True})
    pl = Pipeline(session_factory, policy, FsStore(tmp_path / "ecb"), cfg, adapters={})
    Worker(session_factory, {"run_transition": pl.handle_job}, backoff_base_seconds=0,
           on_dead_letter=pl.on_dead_letter, process_role=ProcessRole.PIPELINE_WORKER).run_until_idle()
    publisher.process_pending()
    checks = callback_capture.requests[-1]["body"]["checks"]
    assert any(c["type"] == T and c["points"] == 10 for c in checks)  # re-priced points in callback


# --- Task 9: atomic bundle+engine provenance; immutable run pin ------------


@pytest.mark.parametrize("flag, resolved_is_x", [(True, True), (False, False)],
                         ids=["flag_on_pins_X", "flag_off_drifts_to_Y"])
def test_cross_bundle_provenance(session_factory, policy, settings, tmp_path, engine,
                                 clean_db, post_event, flag, resolved_is_x):
    bx = bundle_x()
    ydir, by = make_bundle_y(tmp_path, check_type="verified_email", points=1, category="supporting")
    with session_factory() as s:
        store.store_bundle(s, raw_x())                    # X from the normative dir
        store.store_bundle(s, read_policy_files(ydir))
        s.commit()
    # app ingests under X (records bx.bundle_hash on the run); worker process bundle = Y
    post_event("case-xy", "kyb.run_requested", _EMAIL_KYB)
    cfg = settings.model_copy(update={"enforce_bundle_pinning": flag})
    pl = Pipeline(session_factory, by, FsStore(tmp_path/"e"), cfg,
                  adapters={"email_verification": _OkEmailAdapter()})
    Worker(session_factory, {"run_transition": pl.handle_job}, backoff_base_seconds=0,
           on_dead_letter=pl.on_dead_letter, process_role=ProcessRole.PIPELINE_WORKER).run_until_idle()
    expected = bx if resolved_is_x else by
    with session_factory() as s:
        run = s.execute(text("SELECT policy_bundle_hash, engine_build_id FROM runs "
                             "WHERE case_id='case-xy'")).first()
        assert run.policy_bundle_hash == bx.bundle_hash      # creation pin ALWAYS X (immutable)
        assert run.engine_build_id == "eng-1"
        chk = s.execute(
            text("SELECT policy_bundle_hash FROM checks WHERE case_id='case-xy' "
                 "AND check_type='verified_email' AND superseded_by_check_id IS NULL")
        ).scalar_one()
        assert chk == expected.bundle_hash                   # stamped under the resolved bundle
        dec = s.execute(text("SELECT policy_shas, engine_build_id FROM decisions "
                             "WHERE case_id='case-xy' ORDER BY decided_at DESC LIMIT 1")).first()
        assert dec.policy_shas == expected.shas and dec.engine_build_id == "eng-1"
        # reconstruct the bundle hash from the recorded shas (loader.py:56-58 formula:
        # sha256 of "".join(f"{name}:{sha};") over POLICY_FILES) → equals the resolved bundle
        rebuilt = hashlib.sha256("".join(f"{n}:{dec.policy_shas[n]};"
                  for n in POLICY_FILES).encode()).hexdigest()
        assert rebuilt == expected.bundle_hash


def test_cross_bundle_decision_diverges_approve_x_manual_y(
        session_factory, policy, settings, tmp_path, engine, clean_db, post_event):
    ydir, by = make_bundle_y(tmp_path, threshold=101)     # Y differs ONLY in threshold
    with session_factory() as s:
        store.store_bundle(s, raw_x())
        store.store_bundle(s, read_policy_files(ydir))
        s.execute(text("INSERT INTO cases (id) VALUES ('case-div')"))   # broker_status defaults 'clear'
        for ct, cat in [("verified_company_email", "control_proof"),
                        ("official_registry_match", "legal_business_proof"),
                        ("org_id_match", "control_proof"),
                        ("business_document_verified", "legal_business_proof")]:
            checkstore.write_check(s, case_id="case-div", check_type=ct, status=CheckStatus.PASS,
                                   points_awarded=25, category=cat, source="seed")   # stamped sum = 100
        s.commit()

    def _decide(flag: bool) -> str:
        post_event("case-div", "recalculate.requested", {})   # run pinned to app bundle X
        cfg = settings.model_copy(update={"enforce_bundle_pinning": flag})
        pl = Pipeline(session_factory, by, FsStore(tmp_path / f"e{flag}"), cfg, adapters={})
        Worker(session_factory, {"run_transition": pl.handle_job}, backoff_base_seconds=0,
               on_dead_letter=pl.on_dead_letter, process_role=ProcessRole.PIPELINE_WORKER).run_until_idle()
        with session_factory() as s:
            return s.execute(text("SELECT decision FROM decisions WHERE case_id='case-div' "
                                  "ORDER BY decided_at DESC LIMIT 1")).scalar_one()

    assert _decide(flag=True) == "approve"                      # pinned X: 100>=100, all gates, org_id
    assert _decide(flag=False) == "manual_review_insufficient"  # process Y: 100<101 → score_met False


def test_manual_approve_stamps_engine_build_id(session_factory, clean_db, post_event):
    # manual-approve writes DecisionRow(run_id=None) synchronously in the API
    # (events/ingest.py:_handle_manual_approve) — Task 9 stamps ENGINE_BUILD_ID there.
    post_event("c-ma", "reviewer.manual_approve", {"reviewer_id": "rev-1"},
               actor={"type": "reviewer", "id": "rev-1"})
    with session_factory() as s:
        eid = s.execute(text("SELECT engine_build_id FROM decisions WHERE case_id='c-ma' "
                             "AND manual=true ORDER BY decided_at DESC LIMIT 1")).scalar_one()
    assert eid == "eng-1"


def test_broker_blocked_short_circuit_stamps_provenance(
        session_factory, policy, settings, tmp_path, engine, clean_db, post_event):
    bx = bundle_x()
    with engine.begin() as c:
        c.execute(text("INSERT INTO cases (id, broker_status) VALUES ('case-blk','blocked')"))
    with session_factory() as s:
        store.store_bundle(s, raw_x())
        s.commit()
    post_event("case-blk", "email.verified",
               {"email": "o@acme.test", "domain": "acme.test", "verified_at": "2026-07-21T00:00:00Z"})
    cfg = settings.model_copy(update={"enforce_bundle_pinning": True})
    pl = Pipeline(session_factory, policy, FsStore(tmp_path/"e"), cfg, adapters={})  # no broker_matcher
    Worker(session_factory, {"run_transition": pl.handle_job}, backoff_base_seconds=0,
           on_dead_letter=pl.on_dead_letter, process_role=ProcessRole.PIPELINE_WORKER).run_until_idle()
    with session_factory() as s:
        dec = s.execute(text("SELECT decision, policy_shas, engine_build_id FROM decisions "
                             "WHERE case_id='case-blk' ORDER BY decided_at DESC LIMIT 1")).first()
        assert dec.decision == "reject" and dec.engine_build_id == "eng-1" and dec.policy_shas == bx.shas
        reid = s.execute(text("SELECT engine_build_id FROM runs WHERE case_id='case-blk'")).scalar_one()
        assert reid == "eng-1"


def test_cascade_successor_carries_resolved_hash(
        session_factory, policy, settings, tmp_path, engine, clean_db, post_event):
    bx = bundle_x()
    with session_factory() as s:
        store.store_bundle(s, raw_x())
        s.execute(text("INSERT INTO cases (id) VALUES ('case-cas')"))
        # deliberately WRONG categories: a stale bundle-Y-era row must not leak its
        # category into the successor stamped under the resolved (pinned X) bundle
        checkstore.write_check(s, case_id="case-cas", check_type="org_id_match",
                               status=CheckStatus.PASS, points_awarded=25, category="wrong_org_cat",
                               source="direct_rir_rdap", source_detail={"org_handle": "ORG-A"})
        checkstore.write_check(s, case_id="case-cas", check_type="poc_verified",
                               status=CheckStatus.PASS, points_awarded=25, category="wrong_poc_cat",
                               source="seed",
                               source_detail={"poc_handle": "JD-1", "org_handle": "ORG-A"})
        s.commit()                                          # pre-pinning: policy_bundle_hash NULL
    post_event("case-cas", "org_id.submitted", {"rir": "arin", "org_handle": "ORG-B"})
    cfg = settings.model_copy(update={"enforce_bundle_pinning": True})
    pl = Pipeline(session_factory, policy, FsStore(tmp_path/"e"), cfg, adapters={})  # no rdap
    Worker(session_factory, {"run_transition": pl.handle_job}, backoff_base_seconds=0,
           on_dead_letter=pl.on_dead_letter, process_role=ProcessRole.PIPELINE_WORKER).run_until_idle()
    with session_factory() as s:
        live_rows = {
            r.check_type: r
            for r in s.execute(text(
                "SELECT check_type, status, policy_bundle_hash, category FROM checks "
                "WHERE case_id='case-cas' AND superseded_by_check_id IS NULL"
            ))
        }
        old_rows = {
            r.check_type: r
            for r in s.execute(text(
                "SELECT check_type, category, policy_bundle_hash FROM checks "
                "WHERE case_id='case-cas' AND superseded_by_check_id IS NOT NULL"
            ))
        }
    succ = live_rows["org_id_match"]
    assert succ.status == "needs_review"                # cascade successor (points removed)
    assert succ.policy_bundle_hash == bx.bundle_hash    # stamped under the resolved (pinned X) bundle
    assert succ.category == policy.rubric.item("org_id_match").category  # resolved, NOT "wrong_org_cat"
    poc_succ = live_rows["poc_verified"]
    assert poc_succ.status == "needs_review"
    assert poc_succ.policy_bundle_hash == bx.bundle_hash
    assert poc_succ.category == policy.rubric.item("poc_verified").category  # resolved, NOT "wrong_poc_cat"
    # the superseded (old) rows are immutable: still the seeded stale category, and
    # never stamped with a policy_bundle_hash (they were written pre-pinning)
    assert old_rows["org_id_match"].category == "wrong_org_cat"
    assert old_rows["org_id_match"].policy_bundle_hash is None
    assert old_rows["poc_verified"].category == "wrong_poc_cat"
    assert old_rows["poc_verified"].policy_bundle_hash is None
