"""Business-document validator.

Pass rule (03 §7): OCR-extracted name/address/number/jurisdiction match the
submission AND nothing conflicts with official registry evidence.
"""

from kyc_tool.domain.models import CheckStatus, CheckView
from kyc_tool.domain.reasons import ReasonCode
from kyc_tool.validators.base import CheckIntent
from kyc_tool.validators.normalize import norm_equal

SOURCE = "document_ocr_matching"


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
    if not norm_equal(extracted.get("name"), case_snapshot.get("company_legal_name")):
        reasons.append(ReasonCode.DOCUMENT_FIELDS_MISMATCH.value)
    submitted_number = case_snapshot.get("registration_number")
    if submitted_number and not norm_equal(extracted.get("number"), submitted_number):
        reasons.append(ReasonCode.DOCUMENT_FIELDS_MISMATCH.value)
    submitted_jurisdiction = case_snapshot.get("jurisdiction")
    if submitted_jurisdiction and not norm_equal(
        extracted.get("jurisdiction"), submitted_jurisdiction
    ):
        reasons.append(ReasonCode.DOCUMENT_FIELDS_MISMATCH.value)

    # conflict with live registry evidence (gate-5-relevant contradiction)
    registry = next(
        (c for c in live_checks if c.check_type == "official_registry_match"), None
    )
    if registry is not None and registry.status is CheckStatus.PASS:
        registry_number = (normalized.get("registry_detail") or {}).get("company_number")
        # the registry detail travels via extras when available; a mismatch on
        # the registration number is a hard contradiction
        if (
            registry_number
            and extracted.get("number")
            and not norm_equal(extracted.get("number"), registry_number)
        ):
            reasons.append(ReasonCode.DOCUMENT_REGISTRY_CONFLICT.value)

    if reasons:
        return CheckIntent(
            "business_document_verified",
            CheckStatus.FAIL,
            reason_codes=tuple(dict.fromkeys(reasons)),
            source=SOURCE,
            source_detail={"extracted": extracted},
        )
    return CheckIntent(
        "business_document_verified",
        CheckStatus.PASS,
        source=SOURCE,
        source_detail={"extracted": extracted},
    )
