"""Official-registry validator (Companies House / GLEIF).

Pass rule (03 §3): company active/current AND name + address + registration
number match the submission exactly (normalized for case/punctuation — never
fuzzy). One official_registry_match check regardless of which registry
produced the evidence; candidates from all registry adapters are judged and
the first exact match wins.
"""

from kyc_tool.domain.models import CheckStatus
from kyc_tool.domain.reasons import ReasonCode
from kyc_tool.validators.base import CheckIntent
from kyc_tool.validators.normalize import norm, norm_equal

ACTIVE_STATUSES = frozenset({"active", "open", "registered", "issued", "lapsed-active"})


def _candidate_matches(candidate: dict, submission: dict) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    status = norm(candidate.get("status"))
    if status not in ACTIVE_STATUSES:
        reasons.append(ReasonCode.REGISTRY_COMPANY_INACTIVE.value)

    if not norm_equal(candidate.get("legal_name"), submission.get("company_legal_name")):
        reasons.append(ReasonCode.REGISTRY_NAME_MISMATCH.value)

    submitted_number = submission.get("registration_number")
    if submitted_number and not norm_equal(candidate.get("company_number"), submitted_number):
        reasons.append(ReasonCode.REGISTRY_NUMBER_MISMATCH.value)

    submitted_address = submission.get("address")
    if submitted_address and not norm_equal(candidate.get("address"), submitted_address):
        reasons.append(ReasonCode.REGISTRY_ADDRESS_MISMATCH.value)

    return (not reasons, reasons)


def registry_intent(adapter_outputs: dict[str, dict], case_snapshot: dict) -> CheckIntent | None:
    """Judge candidates from every registry adapter that ran."""
    sources = [
        (adapter_id, adapter_outputs[adapter_id])
        for adapter_id in ("companies_house", "gleif")
        if adapter_id in adapter_outputs
    ]
    if not sources:
        return None

    all_reasons: set[str] = set()
    for adapter_id, output in sources:
        for candidate in output.get("candidates", []):
            matched, reasons = _candidate_matches(candidate, case_snapshot)
            if matched:
                return CheckIntent(
                    "official_registry_match",
                    CheckStatus.PASS,
                    source=adapter_id,
                    source_detail={
                        "registry": adapter_id,
                        "legal_name": candidate.get("legal_name"),
                        "company_number": candidate.get("company_number"),
                    },
                )
            all_reasons.update(reasons)

    return CheckIntent(
        "official_registry_match",
        CheckStatus.FAIL,
        reason_codes=tuple(sorted(all_reasons)) or (ReasonCode.REGISTRY_NO_MATCH.value,),
        source=sources[0][0],
    )
