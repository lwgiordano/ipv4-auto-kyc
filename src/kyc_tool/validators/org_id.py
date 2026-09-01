"""ORG-ID (rir_rdap) validator — the highest-accuracy control proof.

Pass rule (03 §4): the RETURNED handle equals the SUBMITTED handle AND the RIR
org name matches the company AND the RIR address materially matches the
submission AND no broker/rejected entity conflict (the broker gate already
rejects blocked handles before this runs).

needs_review routing (adapter_catalog.json, five rules): parent/subsidiary
ambiguity · address missing/stale · related-but-different entity · resources
held by an ISP/technical provider · handle found only via broad name search.
Routed cases award nothing and carry the exact reason code.

"Materially matches" (v1, deterministic): full normalized equality, OR one
normalized address containing the other (same address at different
granularity). Never token-similarity scoring — the old shared-postal-token
shortcut is deliberately gone (remediation item 3): two unrelated addresses
sharing any digit-bearing token (e.g. "100") must not match. Fail-closed:
both addresses are REQUIRED; a submission or RDAP record without one routes to
review instead of silently passing on name alone.
"""

from kyc_tool.domain.models import CheckStatus
from kyc_tool.domain.reasons import ReasonCode
from kyc_tool.validators.base import CheckIntent
from kyc_tool.validators.normalize import canon_id, norm, norm_equal

SOURCE = "direct_rir_rdap"

NEEDS_REVIEW_FLAGS: tuple[tuple[str, ReasonCode], ...] = (
    ("parent_subsidiary_ambiguity", ReasonCode.ORG_ID_PARENT_SUBSIDIARY_AMBIGUITY),
    ("address_missing_or_stale", ReasonCode.ORG_ID_ADDRESS_MISSING_OR_STALE),
    ("related_entity_only", ReasonCode.ORG_ID_RELATED_ENTITY_ONLY),
    ("resources_held_by_provider", ReasonCode.ORG_ID_RESOURCES_HELD_BY_PROVIDER),
    ("via_broad_search", ReasonCode.ORG_ID_BROAD_NAME_SEARCH_ONLY),
)


def _address_materially_matches(rir_address: str, submitted: str) -> bool:
    if norm_equal(rir_address, submitted):
        return True
    a, b = norm(rir_address), norm(submitted)
    return bool(a and b and (a in b or b in a))


def org_id_intent(normalized: dict, case_snapshot: dict) -> CheckIntent:
    org = case_snapshot.get("org_id") or {}
    detail = {
        "org_handle": normalized.get("org_handle") or org.get("org_handle"),
        "rir": normalized.get("rir") or org.get("rir"),
        "entity_name": normalized.get("entity_name"),
    }

    if not normalized.get("found"):
        return CheckIntent(
            "org_id_match",
            CheckStatus.FAIL,
            reason_codes=(ReasonCode.ORG_ID_HANDLE_NOT_FOUND.value,),
            source=SOURCE,
            source_detail=detail,
        )

    # needs_review routing takes precedence over pass — these are exactly the
    # situations a human must look at (never silent points)
    review_reasons = tuple(
        reason.value for flag, reason in NEEDS_REVIEW_FLAGS if normalized.get(flag)
    )
    if review_reasons:
        return CheckIntent(
            "org_id_match",
            CheckStatus.NEEDS_REVIEW,
            reason_codes=review_reasons,
            source=SOURCE,
            source_detail=detail,
        )

    # fail-closed field requirements: every pass-rule input must exist on both
    # sides before a verdict is possible
    submitted_handle = canon_id(org.get("org_handle"))
    if not submitted_handle or not norm(case_snapshot.get("company_legal_name")) or not norm(
        case_snapshot.get("address")
    ):
        return CheckIntent(
            "org_id_match",
            CheckStatus.NEEDS_REVIEW,
            reason_codes=(ReasonCode.ORG_ID_SUBMISSION_INCOMPLETE.value,),
            source=SOURCE,
            source_detail=detail,
        )
    returned_handle = canon_id(normalized.get("org_handle"))
    if not returned_handle or not norm(normalized.get("entity_name")) or not norm(
        normalized.get("address")
    ):
        return CheckIntent(
            "org_id_match",
            CheckStatus.NEEDS_REVIEW,
            reason_codes=(ReasonCode.ORG_ID_EVIDENCE_INCOMPLETE.value,),
            source=SOURCE,
            source_detail=detail,
        )

    reasons: list[str] = []
    if returned_handle != submitted_handle:
        # the RIR answered for a DIFFERENT org than the one submitted — never
        # award control proof for someone else's handle
        reasons.append(ReasonCode.ORG_ID_HANDLE_MISMATCH.value)
    if not norm_equal(normalized.get("entity_name"), case_snapshot.get("company_legal_name")):
        reasons.append(ReasonCode.ORG_ID_NAME_MISMATCH.value)
    if not _address_materially_matches(
        normalized.get("address", ""), case_snapshot.get("address", "")
    ):
        reasons.append(ReasonCode.ORG_ID_ADDRESS_MISMATCH.value)

    if reasons:
        return CheckIntent(
            "org_id_match",
            CheckStatus.FAIL,
            reason_codes=tuple(reasons),
            source=SOURCE,
            source_detail=detail,
        )

    extra = ()
    if normalized.get("conflicting_entity"):
        # deterministic cross-entity contradiction (gate 5): keep the pass
        # verdict honest but stamp the conflict so the gate fails (G6)
        extra = (ReasonCode.HARD_CONFLICT.value,)
    return CheckIntent(
        "org_id_match",
        CheckStatus.PASS,
        reason_codes=extra,
        source=SOURCE,
        source_detail=detail,
    )
