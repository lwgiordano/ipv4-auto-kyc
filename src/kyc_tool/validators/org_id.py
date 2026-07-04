"""ORG-ID (rir_rdap) validator — the highest-accuracy control proof.

Pass rule (03 §4): exact handle exists AND RIR org name matches the company
AND the RIR address materially matches the submission AND no broker/rejected
entity conflict (the broker gate already rejects blocked handles before this
runs).

needs_review routing (adapter_catalog.json, five rules): parent/subsidiary
ambiguity · address missing/stale · related-but-different entity · resources
held by an ISP/technical provider · handle found only via broad name search.
Routed cases award nothing and carry the exact reason code.

"Materially matches" (v1, deterministic): full normalized equality, OR one
normalized address containing the other (same address at different
granularity), OR shared postal-style tokens (digit-bearing tokens of length
≥ 3 — covers UK outward/inward codes and numeric zips). Never
token-similarity scoring.
"""

import re

from kyc_tool.domain.models import CheckStatus
from kyc_tool.domain.reasons import ReasonCode
from kyc_tool.validators.base import CheckIntent
from kyc_tool.validators.normalize import norm, norm_equal

SOURCE = "direct_rir_rdap"

# tokens that look postal: word-chars with at least one digit, length >= 3
_POSTAL_TOKEN = re.compile(r"\b(?=\w*\d)\w{3,}\b")

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
    if a and b and (a in b or b in a):
        return True
    pa, pb = set(_POSTAL_TOKEN.findall(a)), set(_POSTAL_TOKEN.findall(b))
    return bool(pa and pb and pa & pb)


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

    reasons: list[str] = []
    if not norm_equal(normalized.get("entity_name"), case_snapshot.get("company_legal_name")):
        reasons.append(ReasonCode.ORG_ID_NAME_MISMATCH.value)
    submitted_address = case_snapshot.get("address")
    if submitted_address and not _address_materially_matches(
        normalized.get("address", ""), submitted_address
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
