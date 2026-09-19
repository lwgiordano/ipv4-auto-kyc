"""Executable form of docs/SALESFORCE_MAPPING.md.

Pure functions over plain values: given a case's current state, compute exactly
what the PLATFORM should mirror into each Salesforce field. The tool still
never touches Salesforce (05, non-negotiable #2) — this is a read-only
projection for the ops console's Field Map view and for the platform team to
verify their sync against. Encodes AUDIT:A5 (approved_manual → "Account
Approved" + "Manual Approve").
"""

from dataclasses import dataclass
from types import MappingProxyType

KYC_STATUS_MAP = {
    "registered": "Registered",
    "email_verification_pending": "Email Verification Pending",
    "email_verified": "Email Verified",
    "enrichment_running": "Enrichment Running",
    "kyc_pending": "KYC Pending",
    "manual_review_insufficient": "Manual Review - Insufficient Score",
    "account_approved": "Account Approved",
    "approved_manual": "Account Approved",  # AUDIT:A5
    "rejected": "Rejected",
}

BUY_STATUS_MAP = {
    "not_applicable": "Not Applicable",
    "buy_locked_org_id_required": "Buy Locked - ORG-ID Required",
    "org_id_validation_pending": "ORG-ID Validation Pending",
    "org_id_failed": "ORG-ID Failed",
    "buy_enabled": "Buy Enabled",
    "buy_suspended": "Buy Suspended",
}

ACTION_MAP = {
    "approve": "Approve Account",
    "approve_buy_locked": "Approve Account - Buy Locked",
    "reject": "Reject",
    # manual_review_insufficient is a holding state — no enforcement action
}

BROKER_MAP = {"clear": "Clear", "allowed_broker": "Allowed Broker", "blocked": "Blocked"}


def _live(checks: list[dict]) -> list[dict]:
    return [c for c in checks if not c.get("superseded_by_check_id")]


def _of_type(checks: list[dict], check_type: str) -> list[dict]:
    return [c for c in checks if c.get("check_type") == check_type]


def _single_check_status(
    checks: list[dict], check_type: str, labels: dict[str, str], pending: str = "Pending"
) -> str:
    """Live check wins; a history with no live row reads 'Superseded'."""
    of_type = _of_type(checks, check_type)
    live = _live(of_type)
    if live:
        return labels.get(live[0].get("status", ""), pending)
    if of_type:
        return "Superseded"
    return pending


def project_salesforce_fields(
    *,
    case: dict,
    checks: list[dict],
    open_task_types: list[str],
    poc_token_outstanding: bool,
    latest_decision: dict | None,
    latest_manual_decision: dict | None = None,
    mappings: dict[str, str] | None = None,
) -> dict:
    live = _live(checks)
    submitted = case.get("submitted_json") or {}
    gates = (latest_decision or {}).get("gates_json") or {}

    # ORG-ID
    org_live = _live(_of_type(checks, "org_id_match"))
    org_handle = (
        (org_live[0].get("source_detail_json") or {}).get("org_handle")
        if org_live
        else (submitted.get("org_id") or {}).get("org_handle")
    )
    org_status = _single_check_status(
        checks, "org_id_match", {"pass": "Pass", "fail": "Fail", "needs_review": "Pending"}
    )

    # POC
    poc_live = _live(_of_type(checks, "poc_verified"))
    poc_handle = (
        (poc_live[0].get("source_detail_json") or {}).get("poc_handle")
        if poc_live
        else (submitted.get("poc") or {}).get("poc_handle")
    )
    poc_history = _of_type(checks, "poc_verified")
    if poc_live:
        poc_status = {"pass": "Verified", "fail": "Failed", "needs_review": "Pending"}.get(
            poc_live[0].get("status", ""), "Pending"
        )
    elif poc_history:
        poc_status = "Superseded"
    elif poc_token_outstanding:
        poc_status = "Token Sent"
    else:
        poc_status = "Pending"

    # Business document
    doc_live = _live(_of_type(checks, "business_document_verified"))
    if doc_live:
        doc_status = {"pass": "Verified", "fail": "Failed"}.get(doc_live[0].get("status", ""), "Uploaded")
    elif submitted.get("documents"):
        doc_status = "Uploaded"
    else:
        doc_status = "None"

    # Website
    website_live = _live(_of_type(checks, "website_verified"))
    if "website" in open_task_types:
        website_status = "Open"
    elif website_live:
        website_status = {"pass": "Pass", "fail": "Fail"}.get(website_live[0].get("status", ""), "Open")
    else:
        website_status = None

    # Platform action: manual approval is sticky and platform-initiated. The non-manual action
    # derives from the POINTED decision row (the caller passes it), never from the
    # cases.latest_decision column — that projection goes stale after a record-only manual
    # approval, and when the pointer is unresolved (ambiguous pre-014 order) the honest action
    # is NONE, not a guess (re-audit 0c46443 F6).
    if case.get("status") == "approved_manual":
        action = "Manual Approve"
    else:
        action = ACTION_MAP.get((latest_decision or {}).get("decision") or "")

    reason_codes = sorted({code for c in live for code in (c.get("reason_codes") or [])})

    # Manual attribution is STICKY: a later automatic decision moves the pointer but must not
    # blank Manual_Approved_By/At while the case remains approved_manual. The caller passes the
    # latest manual row as its own argument (re-audit 15d875d F6); falling back to the pointed
    # row keeps old callers correct when the pointed row IS the manual one.
    manual = (
        latest_manual_decision
        if (latest_manual_decision or {}).get("manual")
        else (latest_decision if (latest_decision or {}).get("manual") else None)
    )

    fields = {
        "KYC_Status__c": KYC_STATUS_MAP.get(case.get("status", "")),
        # the POINTED decision's score — what was actually decided — never the live recomputed
        # case score, which can drift after the decision; None when the pointer is unresolved
        # (ambiguous pre-014 order): an honest blank, not a guess (re-audit 15d875d F6)
        "KYC_Score__c": (latest_decision or {}).get("score"),
        "Buy_Enablement_Status__c": BUY_STATUS_MAP.get(case.get("buy_status", "")),
        "Platform_Action_Taken__c": action,
        "ORG_ID__c": org_handle,
        "ORG_ID_Status__c": org_status,
        "POC_Handle__c": poc_handle,
        "POC_Verification_Status__c": poc_status,
        "Business_Document_Status__c": doc_status,
        "Website_Review_Status__c": website_status,
        "Broker_Status__c": BROKER_MAP.get(case.get("broker_status", "")),
        # AUDIT:D-SF-NULL — ONLY an authoritative decision that actually evaluated the gate
        # may speak: absent tuple (drift / unresolved order) and bypassed gates (manual
        # approval) both project NULL. The old default fabricated `False` — a definite "no
        # hard conflict" — out of the gate never having been evaluated (re-audit `45cc215`
        # F9). NULLABLE refines `salesforce_sync_fields.json`'s `boolean` (package frozen).
        "Hard_Conflict__c": (not gates["no_hard_conflict"] if "no_hard_conflict" in gates else None),
        "Review_Reason_Codes__c": "; ".join(reason_codes) if reason_codes else None,
        "Manual_Approved_By__c": (manual or {}).get("reviewer_id"),
        "Manual_Approved_At__c": (manual or {}).get("decided_at"),
        "KYC_Check__c": [
            {
                "Check_Type__c": c.get("check_type"),
                "Status__c": c.get("status"),
                "Points__c": c.get("points_awarded", 0),
                "Category__c": c.get("category"),
                "Source__c": c.get("source"),
                "Superseded__c": bool(c.get("superseded_by_check_id")),
                "Reason_Codes__c": "; ".join(c.get("reason_codes") or []),
                "Created_At__c": c.get("created_at"),
            }
            for c in checks
        ],
    }
    return {mappings[key] if mappings is not None else key: value for key, value in fields.items()}


# Static "source of truth" notes per field, for the Field Map view (mirrors the
# mapping doc so the UI documents itself).
FIELD_SOURCES = {
    "KYC_Status__c": "case status (approved_manual → 'Account Approved', AUDIT:A5)",
    "KYC_Score__c": "pointed decision row's score (blank while pre-014 order is unresolved)",
    "Buy_Enablement_Status__c": "case buy_status (tool asserts enabled/locked; platform may show transients)",
    "Platform_Action_Taken__c": "latest decision; 'Manual Approve' when case is approved_manual",
    "ORG_ID__c": "live org_id_match check detail, else submitted org handle",
    "ORG_ID_Status__c": "live org_id_match status; history without live row → Superseded",
    "POC_Handle__c": "live poc_verified check detail, else submitted poc handle",
    "POC_Verification_Status__c": "live poc_verified status; outstanding token → Token Sent",
    "Business_Document_Status__c": "live business_document_verified; uploaded-but-unprocessed → Uploaded",
    "Website_Review_Status__c": "open website task → Open; else live website_verified status",
    "Broker_Status__c": "case broker_status (exact-match gate)",
    "Hard_Conflict__c": "NOT gates.no_hard_conflict from the latest decision; NULL when no "
    "authoritative decision evaluated the gate (unresolved pointer, manual bypass)",
    "Review_Reason_Codes__c": "union of live checks' reason codes",
    "Manual_Approved_By__c": "latest MANUAL decision row's reviewer (sticky across later autos)",
    "Manual_Approved_At__c": "latest MANUAL decision row's timestamp (sticky across later autos)",
    "KYC_Check__c": "one child record per check row (live + superseded)",
}


@dataclass(frozen=True)
class SalesforceFieldContract:
    """Machine-readable public metadata for one canonical projector source field."""

    source_identity: str
    value_type: str
    nullable: bool
    allowed_values: frozenset[str] | None = None


# This must remain pure UI-owned metadata. API response validation imports it, but this module
# never imports the API models: the UI continues to own the canonical projector vocabulary.
SALESFORCE_FIELD_CONTRACT = MappingProxyType(
    {
        "KYC_Status__c": SalesforceFieldContract(
            "case.status",
            "enum",
            True,
            frozenset(
                {
                    "Registered",
                    "Email Verification Pending",
                    "Email Verified",
                    "Enrichment Running",
                    "KYC Pending",
                    "Manual Review - Insufficient Score",
                    "Account Approved",
                    "Rejected",
                }
            ),
        ),
        "KYC_Score__c": SalesforceFieldContract("decision.score", "integer", True),
        "Buy_Enablement_Status__c": SalesforceFieldContract(
            "case.buy_status",
            "enum",
            True,
            frozenset(
                {
                    "Not Applicable",
                    "Buy Locked - ORG-ID Required",
                    "ORG-ID Validation Pending",
                    "ORG-ID Failed",
                    "Buy Enabled",
                    "Buy Suspended",
                }
            ),
        ),
        "Platform_Action_Taken__c": SalesforceFieldContract(
            "tool.decision_or_manual_approval",
            "enum",
            True,
            frozenset(
                {
                    "Approve Account",
                    "Approve Account - Buy Locked",
                    "Reject",
                    "Manual Approve",
                }
            ),
        ),
        "ORG_ID__c": SalesforceFieldContract("org_id_match.handle_or_submission", "text", True),
        "ORG_ID_Status__c": SalesforceFieldContract(
            "org_id_match.status", "enum", False, frozenset({"Pending", "Pass", "Fail", "Superseded"})
        ),
        "POC_Handle__c": SalesforceFieldContract("poc_verified.handle_or_submission", "text", True),
        "POC_Verification_Status__c": SalesforceFieldContract(
            "poc_verified.status_or_token",
            "enum",
            False,
            frozenset({"Pending", "Token Sent", "Verified", "Failed", "Superseded"}),
        ),
        "Business_Document_Status__c": SalesforceFieldContract(
            "business_document_verified.status_or_submission",
            "enum",
            False,
            frozenset({"None", "Uploaded", "Verified", "Failed"}),
        ),
        "Website_Review_Status__c": SalesforceFieldContract(
            "website.review_task_or_check", "enum", True, frozenset({"Open", "Pass", "Fail"})
        ),
        "Broker_Status__c": SalesforceFieldContract(
            "case.broker_status", "enum", True, frozenset({"Clear", "Allowed Broker", "Blocked"})
        ),
        "Hard_Conflict__c": SalesforceFieldContract("decision.gates_json.no_hard_conflict", "boolean", True),
        "Review_Reason_Codes__c": SalesforceFieldContract("checks.live.reason_codes", "text", True),
        "Manual_Approved_By__c": SalesforceFieldContract("manual_decision.reviewer_id", "text", True),
        "Manual_Approved_At__c": SalesforceFieldContract("manual_decision.decided_at", "datetime", True),
        "KYC_Check__c": SalesforceFieldContract("checks.all", "check_records", False),
    }
)
