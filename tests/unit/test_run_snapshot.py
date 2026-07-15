"""PR 2 pure pieces (offline): the snapshot builder never mutates the case, and
the pipeline reads a run's FROZEN snapshot with a pre-008 fallback. The DB-level
behaviour (sequence allocation, cross-run isolation, concurrency) is in
tests/integration/test_run_snapshots.py."""

from types import SimpleNamespace

from kyc_tool.config import Settings
from kyc_tool.domain.models import BuyEnablement, Decision, DecisionResult, Gates
from kyc_tool.events.ingest import _apply_event_to_snapshot, _update_case_metadata
from kyc_tool.orchestration.pipeline import Pipeline


def _case(submitted=None):
    return SimpleNamespace(
        submitted_json=submitted, company_name=None, jurisdiction=None, platform_account_id=None
    )


def test_apply_event_returns_new_dict_and_never_mutates_case():
    case = _case({"existing": 1})
    out = _apply_event_to_snapshot(case, "email.verified", {"email": "a@b.com"})
    assert out == {"existing": 1, "email": {"email": "a@b.com"}}
    # the case snapshot is untouched — the caller pins `out` on the run separately
    assert case.submitted_json == {"existing": 1}
    # and the result is a fresh object, safe to mutate
    out["mutated"] = True
    assert "mutated" not in case.submitted_json


def test_apply_event_merges_each_evidence_type():
    assert _apply_event_to_snapshot(_case(), "kyb.run_requested", {"company_legal_name": "X"}) == {
        "company_legal_name": "X"
    }
    assert _apply_event_to_snapshot(_case(), "org_id.submitted", {"h": 1}) == {"org_id": {"h": 1}}
    assert _apply_event_to_snapshot(_case(), "poc.submitted", {"p": 1}) == {"poc": {"p": 1}}
    # documents accumulate
    case = _case({"documents": [{"a": 1}]})
    assert _apply_event_to_snapshot(case, "document.uploaded", {"b": 2})["documents"] == [
        {"a": 1},
        {"b": 2},
    ]


def test_apply_event_ignores_non_evidence_events():
    # e.g. reviewer.manual_approve carries no submission — snapshot is a copy
    case = _case({"k": "v"})
    out = _apply_event_to_snapshot(case, "reviewer.manual_approve", {})
    assert out == {"k": "v"}
    assert out is not case.submitted_json


def test_update_case_metadata_only_on_kyb():
    case = _case()
    _update_case_metadata(case, "kyb.run_requested", {"company_legal_name": "Acme", "jurisdiction": "GB"})
    assert case.company_name == "Acme"
    assert case.jurisdiction == "GB"
    case2 = _case()
    _update_case_metadata(case2, "email.verified", {"email": "x"})
    assert case2.company_name is None


def test_run_snapshot_prefers_frozen_then_falls_back():
    frozen = Pipeline._run_snapshot(SimpleNamespace(input_snapshot_json={"x": 1}), _case({"y": 2}))
    assert frozen == {"x": 1}  # the run's frozen inputs win
    # pre-008 run (NULL snapshot) → case snapshot fallback
    assert Pipeline._run_snapshot(SimpleNamespace(input_snapshot_json=None), _case({"y": 2})) == {"y": 2}
    # nothing recorded anywhere → empty dict, never None
    assert Pipeline._run_snapshot(SimpleNamespace(input_snapshot_json=None), _case(None)) == {}


def test_callback_event_sequence_gated_by_flag():
    # _callback_body touches no DB, so a bare Pipeline is enough to test the M3 gate
    def pipeline(flag):
        return Pipeline(None, None, None, Settings(callback_include_event_sequence=flag))

    result = DecisionResult(
        Decision.APPROVE, 120, Gates(True, True, True, True, True), BuyEnablement.ENABLED
    )
    case = SimpleNamespace(id="c1")
    run = SimpleNamespace(id="r1")
    event = SimpleNamespace(id="e1", event_sequence=7)

    off = pipeline(False)._callback_body(case, run, event, result, [])
    assert "event_sequence" not in off  # not shipped until the platform accepts it
    on = pipeline(True)._callback_body(case, run, event, result, [])
    assert on["event_sequence"] == 7


def test_seed_in_run_context_only_floqer_discovery():
    base = {"a": 1}
    # Floqer with discovery seeds context in a fresh dict (base untouched)
    out = Pipeline._seed_in_run_context(
        base, "floqer_company_enrichment", {"discovered": True, "company_domain": "acme.example"}
    )
    assert out["floqer_context"]["company_domain"] == "acme.example"
    assert base == {"a": 1}
    # Floqer with no discovery, or any other adapter, leaves the snapshot as-is
    assert Pipeline._seed_in_run_context(base, "floqer_company_enrichment", {"discovered": False}) == base
    assert Pipeline._seed_in_run_context(base, "email_verification", {"discovered": True}) == base


def test_decision_callback_model_preserves_event_sequence():
    # Codex P2: the authoritative model must keep the gated field, not drop it
    from kyc_tool.api.schemas import DecisionCallback

    body = {
        "case_id": "c1",
        "run_id": "r1",
        "event_id": "e1",
        "decision": "approve",
        "score": 120,
        "gates": {
            "score_met": True,
            "legal_proof": True,
            "control_proof": True,
            "broker_ok": True,
            "no_hard_conflict": True,
        },
        "buy_enablement": "enabled",
        "checks": [],
        "decided_at": "2026-01-01T00:00:00Z",
    }
    assert DecisionCallback.model_validate(body).event_sequence is None  # optional / flag off
    assert DecisionCallback.model_validate({**body, "event_sequence": 7}).event_sequence == 7
