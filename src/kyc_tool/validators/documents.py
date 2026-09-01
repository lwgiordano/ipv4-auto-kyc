"""Business-document validator.

Pass rule (03 §7): OCR-extracted name/address/number/jurisdiction match the
submission AND nothing conflicts with official registry evidence.

Fail-closed (remediation item 3): all four fields are required on BOTH sides —
a document that only shows a matching name can never PASS. Precedence: a
DETECTED contradiction (field mismatch, or a registration number conflicting
with live registry evidence) FAILs outright even when other fields are missing;
missing-only routes to NEEDS_REVIEW (submission vs evidence incompleteness get
distinct codes). The registry conflict additionally carries HARD_CONFLICT so
gate 5 fails (remediation item 4).
"""

from kyc_tool.domain.models import CheckStatus, CheckView
from kyc_tool.domain.reasons import ReasonCode
from kyc_tool.validators.base import CheckIntent
from kyc_tool.validators.normalize import norm, norm_equal

SOURCE = "document_ocr_matching"

# (extracted field, submission field) — the four pass-rule comparisons
_COMPARED_FIELDS = (
    ("name", "company_legal_name"),
    ("address", "address"),
    ("number", "registration_number"),
    ("jurisdiction", "jurisdiction"),
)


def document_intent(
    normalized: dict,
    case_snapshot: dict,
    live_checks: tuple[CheckView, ...],
) -> CheckIntent:
    extracted = normalized.get("extracted", {})
    if not extracted:
        return CheckIntent(
            "business_document_verified",
            CheckStatus.FAIL,
            reason_codes=(ReasonCode.DOCUMENT_UNREADABLE.value,),
            source=SOURCE,
        )

    reasons: list[str] = []
    missing_extracted: list[str] = []
    missing_submitted: list[str] = []
    for doc_field, submission_field in _COMPARED_FIELDS:
        doc_value = extracted.get(doc_field)
        submitted_value = case_snapshot.get(submission_field)
        if not norm(doc_value):
            missing_extracted.append(doc_field)
        elif not norm(submitted_value):
            missing_submitted.append(submission_field)
        elif not norm_equal(doc_value, submitted_value):
            reasons.append(ReasonCode.DOCUMENT_FIELDS_MISMATCH.value)

    # conflict with live registry evidence (gate-5 contradiction) — checked
    # whenever both numbers exist, independent of any missing fields
    registry = next(
        (c for c in live_checks if c.check_type == "official_registry_match"), None
    )
    if registry is not None and registry.status is CheckStatus.PASS:
        registry_number = (registry.source_detail or {}).get("company_number")
        if (
            registry_number
            and extracted.get("number")
            and not norm_equal(extracted.get("number"), registry_number)
        ):
            # the document contradicts the authoritative registry record: fail
            # the check AND fail gate 5 (HARD_CONFLICT is the gate's trigger;
            # the specific code preserves the diagnosis)
            reasons.append(ReasonCode.DOCUMENT_REGISTRY_CONFLICT.value)
            reasons.append(ReasonCode.HARD_CONFLICT.value)

    if reasons:
        return CheckIntent(
            "business_document_verified",
            CheckStatus.FAIL,
            reason_codes=tuple(dict.fromkeys(reasons)),
            source=SOURCE,
            source_detail={"extracted": extracted},
        )
    if missing_submitted:
        return CheckIntent(
            "business_document_verified",
            CheckStatus.NEEDS_REVIEW,
            reason_codes=(ReasonCode.DOCUMENT_SUBMISSION_INCOMPLETE.value,),
            source=SOURCE,
            source_detail={"extracted": extracted, "missing_submitted": missing_submitted},
        )
    if missing_extracted:
        return CheckIntent(
            "business_document_verified",
            CheckStatus.NEEDS_REVIEW,
            reason_codes=(ReasonCode.DOCUMENT_EVIDENCE_INCOMPLETE.value,),
            source=SOURCE,
            source_detail={"extracted": extracted, "missing_extracted": missing_extracted},
        )
    return CheckIntent(
        "business_document_verified",
        CheckStatus.PASS,
        source=SOURCE,
        source_detail={"extracted": extracted},
    )
