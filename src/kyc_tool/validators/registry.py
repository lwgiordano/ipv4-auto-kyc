"""Official-registry validator (Companies House / GLEIF).

Pass rule (03 §3): company active/current AND name + address + registration
number match the submission exactly (normalized for case/punctuation — never
fuzzy). One official_registry_match check regardless of which registry
produced the evidence; candidates from all registry adapters are judged and
the first exact match wins.

Fail-closed (remediation item 3): the pass rule requires every field on BOTH
sides. A submission missing name/address/number, or a candidate missing
name/address/number/status, can never PASS — missing data routes to
NEEDS_REVIEW; present-but-mismatched data FAILs with the specific code.
"""

from kyc_tool.domain.models import CheckStatus
from kyc_tool.domain.reasons import ReasonCode
from kyc_tool.validators.base import CheckIntent
from kyc_tool.validators.normalize import norm, norm_equal

ACTIVE_STATUSES = frozenset({"active", "open", "registered", "issued", "lapsed-active"})

REQUIRED_SUBMISSION_FIELDS = ("company_legal_name", "address", "registration_number")
REQUIRED_CANDIDATE_FIELDS = ("legal_name", "company_number", "address", "status")


def _judge_candidate(candidate: dict, submission: dict) -> tuple[str, list[str]]:
    """→ (verdict, reasons) where verdict is 'pass' | 'fail' | 'incomplete'.

    A candidate missing any pass-rule field is 'incomplete' — it can't be
    proven a match OR a mismatch, so it must not FAIL with a mismatch code."""
    if any(not norm(candidate.get(field)) for field in REQUIRED_CANDIDATE_FIELDS):
        return "incomplete", [ReasonCode.REGISTRY_EVIDENCE_INCOMPLETE.value]

    reasons: list[str] = []
    if norm(candidate.get("status")) not in ACTIVE_STATUSES:
        reasons.append(ReasonCode.REGISTRY_COMPANY_INACTIVE.value)
    if not norm_equal(candidate.get("legal_name"), submission.get("company_legal_name")):
        reasons.append(ReasonCode.REGISTRY_NAME_MISMATCH.value)
    if not norm_equal(candidate.get("company_number"), submission.get("registration_number")):
        reasons.append(ReasonCode.REGISTRY_NUMBER_MISMATCH.value)
    if not norm_equal(candidate.get("address"), submission.get("address")):
        reasons.append(ReasonCode.REGISTRY_ADDRESS_MISMATCH.value)
    return ("pass" if not reasons else "fail"), reasons


def registry_intent(adapter_outputs: dict[str, dict], case_snapshot: dict) -> CheckIntent | None:
    """Judge candidates from every registry adapter that ran."""
    sources = [
        (adapter_id, adapter_outputs[adapter_id])
        for adapter_id in ("companies_house", "gleif")
        if adapter_id in adapter_outputs
    ]
    if not sources:
        return None

    missing_submission = [
        field for field in REQUIRED_SUBMISSION_FIELDS if not norm(case_snapshot.get(field))
    ]
    if missing_submission:
        # the pass rule can't be evaluated without the submitted values —
        # ingestible, but never a PASS (fail-closed)
        return CheckIntent(
            "official_registry_match",
            CheckStatus.NEEDS_REVIEW,
            reason_codes=(ReasonCode.REGISTRY_SUBMISSION_INCOMPLETE.value,),
            source=sources[0][0],
            source_detail={"missing_submission_fields": missing_submission},
        )

    mismatch_reasons: set[str] = set()
    any_incomplete = False
    for adapter_id, output in sources:
        for candidate in output.get("candidates", []):
            verdict, reasons = _judge_candidate(candidate, case_snapshot)
            if verdict == "pass":
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
            if verdict == "incomplete":
                any_incomplete = True
            else:
                mismatch_reasons.update(reasons)

    if any_incomplete:
        # an incomplete candidate might be the true match — a human decides;
        # any mismatch codes from complete candidates ride along as context
        return CheckIntent(
            "official_registry_match",
            CheckStatus.NEEDS_REVIEW,
            reason_codes=tuple(
                sorted({ReasonCode.REGISTRY_EVIDENCE_INCOMPLETE.value, *mismatch_reasons})
            ),
            source=sources[0][0],
        )
    return CheckIntent(
        "official_registry_match",
        CheckStatus.FAIL,
        reason_codes=tuple(sorted(mismatch_reasons)) or (ReasonCode.REGISTRY_NO_MATCH.value,),
        source=sources[0][0],
    )
