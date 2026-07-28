"""The executable Salesforce mapping (docs/SALESFORCE_MAPPING.md) — including
AUDIT:A5 and the derived per-field state rules."""

from kyc_tool.ui.salesforce_projection import FIELD_SOURCES, project_salesforce_fields


def _check(check_type, status, *, superseded=False, points=0, detail=None, reasons=None):
    return {
        "check_type": check_type,
        "status": status,
        "points_awarded": points,
        "category": "x",
        "source": "test",
        "reason_codes": reasons or [],
        "superseded_by_check_id": "successor" if superseded else None,
        "source_detail_json": detail or {},
        "created_at": "2026-01-01T00:00:00Z",
    }


BASE_CASE = {
    "id": "c1",
    "status": "account_approved",
    "buy_status": "buy_enabled",
    "broker_status": "clear",
    "current_score": 105,
    "latest_decision": "approve",
    "submitted_json": {},
}


def project(**overrides):
    kwargs = {
        "case": BASE_CASE,
        "checks": [],
        "open_task_types": [],
        "poc_token_outstanding": False,
        # the projection derives the non-manual action from the POINTED decision row's own
        # value (re-audit 0c46443 F6) — the fixture carries it like the real pointer row does
        "latest_decision": {"decision": "approve", "gates_json": {"no_hard_conflict": True},
                            "manual": False},
    }
    kwargs.update(overrides)
    return project_salesforce_fields(**kwargs)


def test_approve_maps_core_fields():
    fields = project()
    assert fields["KYC_Status__c"] == "Account Approved"
    assert fields["KYC_Score__c"] == 105
    assert fields["Buy_Enablement_Status__c"] == "Buy Enabled"
    assert fields["Platform_Action_Taken__c"] == "Approve Account"
    assert fields["Broker_Status__c"] == "Clear"
    assert fields["Hard_Conflict__c"] is False


def test_a5_approved_manual_maps_to_account_approved_plus_manual_action():
    fields = project(
        case={**BASE_CASE, "status": "approved_manual", "latest_decision": "manual_review_insufficient"},
        latest_decision={"gates_json": {}, "manual": True, "reviewer_id": "rev-1",
                         "decided_at": "2026-01-02T00:00:00Z"},
    )
    assert fields["KYC_Status__c"] == "Account Approved"  # AUDIT:A5
    assert fields["Platform_Action_Taken__c"] == "Manual Approve"
    assert fields["Manual_Approved_By__c"] == "rev-1"
    assert fields["Manual_Approved_At__c"] == "2026-01-02T00:00:00Z"


def test_hard_conflict_is_negated_gate():
    fields = project(latest_decision={"gates_json": {"no_hard_conflict": False}, "manual": False})
    assert fields["Hard_Conflict__c"] is True


def test_org_id_states():
    live = project(checks=[_check("org_id_match", "pass", detail={"org_handle": "ORG-A"})])
    assert (live["ORG_ID__c"], live["ORG_ID_Status__c"]) == ("ORG-A", "Pass")

    superseded_only = project(checks=[_check("org_id_match", "pass", superseded=True)])
    assert superseded_only["ORG_ID_Status__c"] == "Superseded"

    nothing = project(case={**BASE_CASE, "submitted_json": {"org_id": {"org_handle": "ORG-S"}}})
    assert (nothing["ORG_ID__c"], nothing["ORG_ID_Status__c"]) == ("ORG-S", "Pending")


def test_poc_token_sent_state():
    fields = project(
        case={**BASE_CASE, "submitted_json": {"poc": {"poc_handle": "JD1"}}},
        poc_token_outstanding=True,
    )
    assert fields["POC_Handle__c"] == "JD1"
    assert fields["POC_Verification_Status__c"] == "Token Sent"

    verified = project(checks=[_check("poc_verified", "pass", detail={"poc_handle": "JD1"})])
    assert verified["POC_Verification_Status__c"] == "Verified"


def test_document_uploaded_but_unprocessed():
    fields = project(case={**BASE_CASE, "submitted_json": {"documents": [{"object_ref": "fs://x"}]}})
    assert fields["Business_Document_Status__c"] == "Uploaded"
    assert project()["Business_Document_Status__c"] == "None"


def test_website_open_task_wins():
    fields = project(open_task_types=["website"])
    assert fields["Website_Review_Status__c"] == "Open"
    passed = project(checks=[_check("website_verified", "pass")])
    assert passed["Website_Review_Status__c"] == "Pass"


def test_reason_codes_union_live_only():
    fields = project(
        checks=[
            _check("org_id_match", "needs_review", reasons=["org_id_address_missing_or_stale"]),
            _check("verified_company_email", "fail", reasons=["email_domain_mismatch"]),
            _check("website_verified", "fail", superseded=True, reasons=["website_parked"]),
        ]
    )
    assert fields["Review_Reason_Codes__c"] == "email_domain_mismatch; org_id_address_missing_or_stale"


def test_child_records_cover_all_checks():
    fields = project(
        checks=[
            _check("verified_email", "pass", points=10),
            _check("verified_email", "pass", superseded=True, points=10),
        ]
    )
    records = fields["KYC_Check__c"]
    assert len(records) == 2
    assert {r["Superseded__c"] for r in records} == {True, False}


def test_every_projected_field_has_a_documented_source():
    fields = project()
    assert set(fields) == set(FIELD_SOURCES)
