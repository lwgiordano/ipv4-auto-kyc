"""Code ↔ normative-JSON alignment. These tests are what makes the JSONs
authoritative: if the spec files change shape, this fails before behavior
drifts silently."""

from kyc_tool.api.schemas import PAYLOAD_MODELS
from kyc_tool.config import REPO_ROOT
from kyc_tool.domain.models import BuyStatus, CaseStatus, Decision, RunState
from kyc_tool.policy.loader import load_policy

BUNDLE = load_policy(REPO_ROOT / "KYC_Tool_Build_Package" / "machine_readable")


def test_decision_enum_matches_policy():
    assert set(BUNDLE.decision_policy.decision_values) == {d.value for d in Decision}
    # priority order per decision_policy.json
    assert BUNDLE.decision_policy.decision_values == (
        "reject",
        "approve",
        "approve_buy_locked",
        "manual_review_insufficient",
    )


def test_manual_review_insufficient_auto_clears():
    rule = next(
        r for r in BUNDLE.decision_policy.decisions if r.decision == "manual_review_insufficient"
    )
    assert rule.model_extra.get("auto_clear") is True


def test_five_hard_gates_not_four():
    """AUDIT:A2 — the prose says four; the normative rubric defines five."""
    assert len(BUNDLE.rubric.hard_gates) == 5
    assert set(BUNDLE.rubric.hard_gates) == {
        "score_met",
        "legal_business_proof_required",
        "control_proof_required",
        "broker_status_allowed",
        "hard_conflict_must_be_false",
    }


def test_rubric_has_eight_check_types():
    assert len(BUNDLE.rubric.items) == 8
    assert set(BUNDLE.rubric.check_types) == {
        "verified_company_email",
        "official_registry_match",
        "org_id_match",
        "poc_verified",
        "business_document_verified",
        "linkedin_company_match",
        "website_verified",
        "verified_email",
    }


def test_adapter_catalog_has_nine_adapters():
    """AUDIT:A3 — the catalog (not the prose's 'eight') is canonical."""
    ids = BUNDLE.adapter_catalog.adapter_ids
    assert len(ids) == 9
    assert ids[0] == "broker_policy"  # order 1: the gate runs first
    website = next(e for e in BUNDLE.adapter_catalog.root if e.adapter_id == "website_manual_review")
    assert website.mode == "manual"  # no crawler in v1


def test_event_catalog_matches_api_schemas():
    assert set(BUNDLE.events.event_types) == set(PAYLOAD_MODELS)


def test_state_machine_matches_domain_enums():
    assert set(BUNDLE.state_machine.run_states) == {s.value for s in RunState}
    assert set(BUNDLE.state_machine.case_statuses) == {s.value for s in CaseStatus}
    assert set(BUNDLE.state_machine.buy_statuses) == {s.value for s in BuyStatus}


def test_broker_entities_split():
    blocked = {e.name for e in BUNDLE.broker_policy.entities if e.policy == "blocked"}
    allowed = {e.name for e in BUNDLE.broker_policy.entities if e.policy == "allowed"}
    assert blocked == {"Larus", "Brander", "InterLIR"}
    assert allowed == {"Silicon Desert", "IP Trading", "IPXO"}
    assert BUNDLE.broker_policy.matching_mode_v1 == "exact_identifier_only"


def test_allowed_broker_still_scoreable():
    assert set(BUNDLE.rubric.allowed_broker_statuses) == {"clear", "allowed_broker"}
