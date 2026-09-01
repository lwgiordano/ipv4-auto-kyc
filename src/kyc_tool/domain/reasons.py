"""Reason codes attached to checks and review tasks.

Deterministic validation must be explainable: every non-pass outcome carries at
least one code. Codes are stable strings (they reach Salesforce's
Review_Reason_Codes__c and the audit trail).
"""

from enum import StrEnum


class ReasonCode(StrEnum):
    # email
    EMAIL_NOT_VERIFIED = "email_not_verified"
    EMAIL_FREE_OR_DISPOSABLE_DOMAIN = "email_free_or_disposable_domain"
    EMAIL_DOMAIN_MISMATCH = "email_domain_mismatch"
    EMAIL_PAYLOAD_DOMAIN_CONFLICT = "email_payload_domain_conflict"
    EMAIL_EVIDENCE_INCOMPLETE = "email_evidence_incomplete"
    EMAIL_SUBMISSION_INCOMPLETE = "email_submission_incomplete"

    # registry
    REGISTRY_COMPANY_INACTIVE = "registry_company_inactive"
    REGISTRY_NAME_MISMATCH = "registry_name_mismatch"
    REGISTRY_ADDRESS_MISMATCH = "registry_address_mismatch"
    REGISTRY_NUMBER_MISMATCH = "registry_number_mismatch"
    REGISTRY_NO_MATCH = "registry_no_match"
    REGISTRY_SUBMISSION_INCOMPLETE = "registry_submission_incomplete"
    REGISTRY_EVIDENCE_INCOMPLETE = "registry_evidence_incomplete"

    # ORG-ID (rir_rdap) — needs_review routing per adapter_catalog.json
    ORG_ID_HANDLE_NOT_FOUND = "org_id_handle_not_found"
    ORG_ID_NAME_MISMATCH = "org_id_name_mismatch"
    ORG_ID_ADDRESS_MISMATCH = "org_id_address_mismatch"
    ORG_ID_BROKER_CONFLICT = "org_id_broker_conflict"
    ORG_ID_PARENT_SUBSIDIARY_AMBIGUITY = "org_id_parent_subsidiary_ambiguity"
    ORG_ID_ADDRESS_MISSING_OR_STALE = "org_id_address_missing_or_stale"
    ORG_ID_RELATED_ENTITY_ONLY = "org_id_related_entity_only"
    ORG_ID_RESOURCES_HELD_BY_PROVIDER = "org_id_resources_held_by_provider"
    ORG_ID_BROAD_NAME_SEARCH_ONLY = "org_id_broad_name_search_only"
    ORG_ID_HANDLE_MISMATCH = "org_id_handle_mismatch"
    ORG_ID_SUBMISSION_INCOMPLETE = "org_id_submission_incomplete"
    ORG_ID_EVIDENCE_INCOMPLETE = "org_id_evidence_incomplete"

    # POC
    POC_NOT_ASSOCIATED = "poc_not_associated"
    POC_EMAIL_HIDDEN = "poc_email_hidden"
    POC_TOKEN_EXPIRED = "poc_token_expired"
    POC_TOKEN_INVALID = "poc_token_invalid"
    POC_NO_ASSOCIATION_TARGET = "poc_no_association_target"
    POC_TOKEN_BINDING_MISMATCH = "poc_token_binding_mismatch"
    POC_TOKEN_CONSUMED = "poc_token_consumed"

    # identity revalidation (supersession placeholders)
    ORG_ID_REVALIDATION_PENDING = "org_id_revalidation_pending"

    # document
    DOCUMENT_FIELDS_MISMATCH = "document_fields_mismatch"
    DOCUMENT_REGISTRY_CONFLICT = "document_registry_conflict"
    DOCUMENT_UNREADABLE = "document_unreadable"
    DOCUMENT_SUBMISSION_INCOMPLETE = "document_submission_incomplete"
    DOCUMENT_EVIDENCE_INCOMPLETE = "document_evidence_incomplete"

    # linkedin
    LINKEDIN_FIELDS_MISMATCH = "linkedin_fields_mismatch"
    LINKEDIN_NO_DATA = "linkedin_no_data"

    # website (human review)
    WEBSITE_DOES_NOT_RESOLVE = "website_does_not_resolve"
    WEBSITE_NO_HTTPS = "website_no_https"
    WEBSITE_CONTENT_UNRELATED = "website_content_unrelated"
    WEBSITE_PARKED = "website_parked"
    WEBSITE_FRAUD_INDICATORS = "website_fraud_indicators"

    # cross-check conflicts (gate 5)
    HARD_CONFLICT = "hard_conflict"

    # broker
    BLOCKED_BROKER_EXACT_MATCH = "blocked_broker_exact_match"

    # operational
    UPSTREAM_ERROR = "upstream_error"
