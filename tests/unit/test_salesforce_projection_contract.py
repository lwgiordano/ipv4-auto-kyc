from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from kyc_tool.api.schemas import SalesforceProjectionResponse
from kyc_tool.ui.salesforce_projection import FIELD_SOURCES, SALESFORCE_FIELD_CONTRACT


def valid_response_dict() -> dict:
    timestamp = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
    return {
        "case_id": "platform-case-123",
        "mapping_revision": "24",
        "configuration_revision": "21",
        "decision_authority": {
            "provenance": "latest_decision_row",
            "decision_row_id": "decision-123",
            "run_id": "run-123",
            "decision_kind": "automatic",
            "decision": "approve",
            "run_provenance": "resolved",
        },
        "manual_approval_authority": {
            "provenance": "no_manual_decisions",
            "decision_row_id": None,
            "reviewer_id": None,
            "decided_at": None,
        },
        "fields": {
            "KYC_Status__c": {
                "source_field": "KYC_Status__c",
                "source_identity": "case.status",
                "value_type": "enum",
                "nullable": True,
                "value": "Manual Review - Insufficient Score",
            },
            "KYC_Score__c": {
                "source_field": "KYC_Score__c",
                "source_identity": "decision.score",
                "value_type": "integer",
                "nullable": True,
                "value": 100,
            },
            "Buy_Enablement_Status__c": {
                "source_field": "Buy_Enablement_Status__c",
                "source_identity": "case.buy_status",
                "value_type": "enum",
                "nullable": True,
                "value": "Buy Enabled",
            },
            "Platform_Action_Taken__c": {
                "source_field": "Platform_Action_Taken__c",
                "source_identity": "tool.decision_or_manual_approval",
                "value_type": "enum",
                "nullable": True,
                "value": "Approve Account - Buy Locked",
            },
            "ORG_ID__c": {
                "source_field": "ORG_ID__c",
                "source_identity": "org_id_match.handle_or_submission",
                "value_type": "text",
                "nullable": True,
                "value": "ORG-1",
            },
            "ORG_ID_Status__c": {
                "source_field": "ORG_ID_Status__c",
                "source_identity": "org_id_match.status",
                "value_type": "enum",
                "nullable": False,
                "value": "Pass",
            },
            "POC_Handle__c": {
                "source_field": "POC_Handle__c",
                "source_identity": "poc_verified.handle_or_submission",
                "value_type": "text",
                "nullable": True,
                "value": "POC-1",
            },
            "POC_Verification_Status__c": {
                "source_field": "POC_Verification_Status__c",
                "source_identity": "poc_verified.status_or_token",
                "value_type": "enum",
                "nullable": False,
                "value": "Verified",
            },
            "Business_Document_Status__c": {
                "source_field": "Business_Document_Status__c",
                "source_identity": "business_document_verified.status_or_submission",
                "value_type": "enum",
                "nullable": False,
                "value": "Verified",
            },
            "Website_Review_Status__c": {
                "source_field": "Website_Review_Status__c",
                "source_identity": "website.review_task_or_check",
                "value_type": "enum",
                "nullable": True,
                "value": "Pass",
            },
            "Broker_Status__c": {
                "source_field": "Broker_Status__c",
                "source_identity": "case.broker_status",
                "value_type": "enum",
                "nullable": True,
                "value": "Clear",
            },
            "Hard_Conflict__c": {
                "source_field": "Hard_Conflict__c",
                "source_identity": "decision.gates_json.no_hard_conflict",
                "value_type": "boolean",
                "nullable": True,
                "value": None,
            },
            "Review_Reason_Codes__c": {
                "source_field": "Review_Reason_Codes__c",
                "source_identity": "checks.live.reason_codes",
                "value_type": "text",
                "nullable": True,
                "value": "reason-one; reason-two",
            },
            "Manual_Approved_By__c": {
                "source_field": "Manual_Approved_By__c",
                "source_identity": "manual_decision.reviewer_id",
                "value_type": "text",
                "nullable": True,
                "value": "reviewer-1",
            },
            "Manual_Approved_At__c": {
                "source_field": "Manual_Approved_At__c",
                "source_identity": "manual_decision.decided_at",
                "value_type": "datetime",
                "nullable": True,
                "value": timestamp,
            },
            "KYC_Check__c": {
                "source_field": "KYC_Check__c",
                "source_identity": "checks.all",
                "value_type": "check_records",
                "nullable": False,
                "value": [
                    {
                        "Check_Type__c": "org_id_match",
                        "Status__c": "pass",
                        "Points__c": 10,
                        "Category__c": "identity",
                        "Source__c": "registry",
                        "Superseded__c": False,
                        "Reason_Codes__c": "",
                        "Created_At__c": timestamp,
                    }
                ],
            },
        },
        "projection_timestamp": timestamp,
    }


def test_public_field_contract_covers_the_existing_projector_keys():
    assert set(SALESFORCE_FIELD_CONTRACT) == set(FIELD_SOURCES)
    assert SALESFORCE_FIELD_CONTRACT["KYC_Score__c"].source_identity == "decision.score"
    assert SALESFORCE_FIELD_CONTRACT["Hard_Conflict__c"].nullable is True
    assert SALESFORCE_FIELD_CONTRACT["KYC_Check__c"].value_type == "check_records"


def test_public_response_rejects_unknown_field_metadata_and_bad_domain():
    candidate = valid_response_dict()
    candidate["fields"]["KYC_Status__c"]["value"] = "Approved Somehow"
    with pytest.raises(ValidationError):
        SalesforceProjectionResponse.model_validate(candidate)

    candidate = valid_response_dict()
    candidate["fields"]["KYC_Status__c"]["unknown_metadata"] = "not allowed"
    with pytest.raises(ValidationError):
        SalesforceProjectionResponse.model_validate(candidate)


def test_public_response_keeps_scalar_domains_exact():
    candidate = valid_response_dict()
    candidate["fields"]["KYC_Score__c"]["value"] = True
    with pytest.raises(ValidationError):
        SalesforceProjectionResponse.model_validate(candidate)

    candidate = valid_response_dict()
    candidate["fields"]["Hard_Conflict__c"]["value"] = None
    assert SalesforceProjectionResponse.model_validate(candidate).fields["Hard_Conflict__c"].value is None


def test_datetime_values_round_trip_from_their_rfc3339_json_representation():
    response = SalesforceProjectionResponse.model_validate(valid_response_dict())
    restored = SalesforceProjectionResponse.model_validate_json(response.model_dump_json())
    assert restored.fields["Manual_Approved_At__c"].value == datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize("value", [0, True, "2026-09-12"])
def test_datetime_fields_reject_non_rfc3339_scalar_representations(value):
    candidate = valid_response_dict()
    candidate["fields"]["Manual_Approved_At__c"]["value"] = value
    with pytest.raises(ValidationError):
        SalesforceProjectionResponse.model_validate(candidate)


def test_check_record_datetime_rejects_numeric_epoch_coercion():
    candidate = valid_response_dict()
    candidate["fields"]["KYC_Check__c"]["value"][0]["Created_At__c"] = 0
    with pytest.raises(ValidationError):
        SalesforceProjectionResponse.model_validate(candidate)


@pytest.mark.parametrize("revision", ["current", "-1", "1.5", True])
def test_revisions_must_be_decimal_strings_or_null(revision):
    candidate = valid_response_dict()
    candidate["mapping_revision"] = revision
    with pytest.raises(ValidationError):
        SalesforceProjectionResponse.model_validate(candidate)

    candidate = valid_response_dict()
    candidate["configuration_revision"] = revision
    with pytest.raises(ValidationError):
        SalesforceProjectionResponse.model_validate(candidate)


def test_decimal_revisions_accept_values_and_null():
    candidate = valid_response_dict()
    candidate["mapping_revision"] = "00024"
    candidate["configuration_revision"] = "00021"
    response = SalesforceProjectionResponse.model_validate(candidate)
    assert response.mapping_revision == "00024"
    assert response.configuration_revision == "00021"

    candidate = valid_response_dict()
    candidate["mapping_revision"] = None
    candidate["configuration_revision"] = None
    response = SalesforceProjectionResponse.model_validate(candidate)
    assert response.mapping_revision is None
    assert response.configuration_revision is None


@pytest.mark.parametrize("value", [0, True, "2026-09-12", datetime(2026, 9, 12, 12, 0)])
def test_projection_timestamp_rejects_non_rfc3339_or_naive_values(value):
    candidate = valid_response_dict()
    candidate["projection_timestamp"] = value
    with pytest.raises(ValidationError):
        SalesforceProjectionResponse.model_validate(candidate)


def test_projection_timestamp_normalizes_an_aware_offset_to_utc():
    candidate = valid_response_dict()
    candidate["projection_timestamp"] = datetime(
        2026, 9, 12, 17, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))
    )
    response = SalesforceProjectionResponse.model_validate(candidate)
    assert response.projection_timestamp == datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
    assert response.model_dump(mode="json")["projection_timestamp"] == "2026-09-12T12:00:00Z"


def test_all_timestamp_boundaries_normalize_aware_values_to_utc():
    offset_time = datetime(2026, 9, 12, 17, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    expected = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
    candidate = valid_response_dict()
    candidate["projection_timestamp"] = offset_time
    candidate["fields"]["Manual_Approved_At__c"]["value"] = offset_time
    candidate["fields"]["KYC_Check__c"]["value"][0]["Created_At__c"] = offset_time
    candidate["manual_approval_authority"] = {
        "provenance": "latest_manual_row",
        "decision_row_id": "manual-decision-1",
        "reviewer_id": "reviewer-1",
        "decided_at": offset_time,
    }
    response = SalesforceProjectionResponse.model_validate(candidate)
    assert response.projection_timestamp == expected
    assert response.fields["Manual_Approved_At__c"].value == expected
    assert response.fields["KYC_Check__c"].value[0].Created_At__c == expected
    assert response.manual_approval_authority.decided_at == expected


@pytest.mark.parametrize("value", [0, True, "2026-09-12", datetime(2026, 9, 12, 12, 0)])
def test_manual_authority_timestamp_rejects_non_rfc3339_or_naive_values(value):
    candidate = valid_response_dict()
    candidate["manual_approval_authority"] = {
        "provenance": "latest_manual_row",
        "decision_row_id": "manual-decision-1",
        "reviewer_id": "reviewer-1",
        "decided_at": value,
    }
    with pytest.raises(ValidationError):
        SalesforceProjectionResponse.model_validate(candidate)


def test_field_validator_rejects_raw_date_and_tuple_check_values_before_coercion():
    candidate = valid_response_dict()
    candidate["fields"]["Manual_Approved_At__c"]["value"] = date(2026, 9, 12)
    with pytest.raises(ValidationError):
        SalesforceProjectionResponse.model_validate(candidate)

    candidate = valid_response_dict()
    candidate["fields"]["KYC_Check__c"]["value"] = tuple(candidate["fields"]["KYC_Check__c"]["value"])
    with pytest.raises(ValidationError):
        SalesforceProjectionResponse.model_validate(candidate)


def test_nullable_datetime_field_allows_null_without_datetime_coercion():
    candidate = valid_response_dict()
    candidate["fields"]["Manual_Approved_At__c"]["value"] = None
    assert (
        SalesforceProjectionResponse.model_validate(candidate).fields["Manual_Approved_At__c"].value is None
    )


@pytest.mark.parametrize(
    ("authority", "configuration_revision"),
    [
        (
            {
                "provenance": "latest_decision_row",
                "decision_row_id": "automatic-resolved",
                "run_id": "run-1",
                "decision_kind": "automatic",
                "decision": "approve",
                "run_provenance": "resolved",
            },
            "21",
        ),
        (
            {
                "provenance": "latest_decision_row",
                "decision_row_id": "automatic-unresolved",
                "run_id": "run-2",
                "decision_kind": "automatic",
                "decision": "reject",
                "run_provenance": "unresolved",
            },
            None,
        ),
        (
            {
                "provenance": "latest_decision_row",
                "decision_row_id": "manual-row",
                "run_id": None,
                "decision_kind": "manual",
                "decision": "manual_review_insufficient",
                "run_provenance": "not_applicable",
            },
            None,
        ),
        *[
            (
                {
                    "provenance": provenance,
                    "decision_row_id": None,
                    "run_id": None,
                    "decision_kind": None,
                    "decision": None,
                    "run_provenance": "not_applicable",
                },
                None,
            )
            for provenance in ("unresolved_pointer_drift", "unresolved_legacy_order", "no_decisions")
        ],
    ],
)
def test_decision_authority_matrix_accepts_every_honest_state(authority, configuration_revision):
    candidate = valid_response_dict()
    candidate["decision_authority"] = authority
    candidate["configuration_revision"] = configuration_revision
    assert (
        SalesforceProjectionResponse.model_validate(candidate).decision_authority.provenance
        == authority["provenance"]
    )


@pytest.mark.parametrize(
    "authority",
    [
        {
            "provenance": "latest_manual_row",
            "decision_row_id": "manual-1",
            "reviewer_id": "reviewer-1",
            "decided_at": "2026-09-12T12:00:00Z",
        },
        *[
            {
                "provenance": provenance,
                "decision_row_id": None,
                "reviewer_id": None,
                "decided_at": None,
            }
            for provenance in ("unresolved_pointer_drift", "unresolved_legacy_order", "no_manual_decisions")
        ],
    ],
)
def test_manual_authority_matrix_accepts_every_sticky_state(authority):
    candidate = valid_response_dict()
    candidate["manual_approval_authority"] = authority
    assert (
        SalesforceProjectionResponse.model_validate(candidate).manual_approval_authority.provenance
        == authority["provenance"]
    )


@pytest.mark.parametrize(
    "authority",
    [
        {
            "provenance": "latest_decision_row",
            "decision_row_id": "automatic-missing-run",
            "run_id": None,
            "decision_kind": "automatic",
            "decision": "approve",
            "run_provenance": "resolved",
        },
        {
            "provenance": "latest_decision_row",
            "decision_row_id": "manual-carrying-run",
            "run_id": "run-that-must-not-exist",
            "decision_kind": "manual",
            "decision": "manual_review_insufficient",
            "run_provenance": "not_applicable",
        },
        {
            "provenance": "unresolved_pointer_drift",
            "decision_row_id": "invented-row",
            "run_id": None,
            "decision_kind": "manual",
            "decision": "reject",
            "run_provenance": "not_applicable",
        },
    ],
)
def test_decision_authority_rejects_malformed_provenance_combinations(authority):
    candidate = valid_response_dict()
    candidate["decision_authority"] = authority
    candidate["configuration_revision"] = None
    with pytest.raises(ValidationError):
        SalesforceProjectionResponse.model_validate(candidate)


@pytest.mark.parametrize(
    "authority",
    [
        {
            "provenance": "latest_manual_row",
            "decision_row_id": "manual-incomplete",
            "reviewer_id": None,
            "decided_at": "2026-09-12T12:00:00Z",
        },
        {
            "provenance": "unresolved_legacy_order",
            "decision_row_id": "invented-manual-row",
            "reviewer_id": "reviewer-1",
            "decided_at": "2026-09-12T12:00:00Z",
        },
    ],
)
def test_manual_authority_rejects_malformed_provenance_combinations(authority):
    candidate = valid_response_dict()
    candidate["manual_approval_authority"] = authority
    with pytest.raises(ValidationError):
        SalesforceProjectionResponse.model_validate(candidate)


@pytest.mark.parametrize(
    "authority",
    [
        {
            "provenance": "latest_decision_row",
            "decision_row_id": "automatic-unresolved",
            "run_id": "run-2",
            "decision_kind": "automatic",
            "decision": "reject",
            "run_provenance": "unresolved",
        },
        {
            "provenance": "latest_decision_row",
            "decision_row_id": "manual-row",
            "run_id": None,
            "decision_kind": "manual",
            "decision": "manual_review_insufficient",
            "run_provenance": "not_applicable",
        },
        *[
            {
                "provenance": provenance,
                "decision_row_id": None,
                "run_id": None,
                "decision_kind": None,
                "decision": None,
                "run_provenance": "not_applicable",
            }
            for provenance in ("unresolved_pointer_drift", "unresolved_legacy_order", "no_decisions")
        ],
    ],
)
def test_only_resolved_automatic_decisions_may_carry_configuration_revision(authority):
    candidate = valid_response_dict()
    candidate["decision_authority"] = authority
    candidate["configuration_revision"] = "21"
    with pytest.raises(ValidationError):
        SalesforceProjectionResponse.model_validate(candidate)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "unknown"])
def test_response_requires_exact_unique_canonical_source_coverage(mutation):
    candidate = valid_response_dict()
    if mutation == "missing":
        candidate["fields"].pop("KYC_Score__c")
    elif mutation == "duplicate":
        candidate["fields"]["KYC_Score__c"]["source_field"] = "KYC_Status__c"
    else:
        candidate["fields"]["unexpected_destination"] = {
            "source_field": "Unknown__c",
            "source_identity": "unknown",
            "value_type": "text",
            "nullable": True,
            "value": "unexpected",
        }
    with pytest.raises(ValidationError):
        SalesforceProjectionResponse.model_validate(candidate)
