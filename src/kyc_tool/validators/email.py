"""Email verification validators (AUDIT:D5 — one verification event can
produce both the any-inbox and the company-email checks)."""

from kyc_tool.domain.models import CheckStatus
from kyc_tool.domain.reasons import ReasonCode
from kyc_tool.validators.base import CheckIntent
from kyc_tool.validators.normalize import domain_of

# Free/disposable providers can never be "company email" (+25). Deliberately a
# short, exact list — extending it is a policy decision, not a heuristic.
FREE_EMAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "yahoo.co.uk",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "msn.com",
        "aol.com",
        "icloud.com",
        "me.com",
        "proton.me",
        "protonmail.com",
        "gmx.com",
        "gmx.de",
        "mail.com",
        "yandex.com",
        "yandex.ru",
        "zoho.com",
        "mailinator.com",
        "guerrillamail.com",
        "10minutemail.com",
        "tempmail.com",
        "yopmail.com",
    }
)

SOURCE = "platform_email_verification"


def email_intents(normalized: dict, case_snapshot: dict) -> list[CheckIntent]:
    """(normalized email evidence, submission) → verified_email and possibly
    verified_company_email intents.

    Fail-closed (remediation item 3): the domain is derived from the email
    ADDRESS, never trusted from the payload's separate `domain` field — that
    field once let `attacker@gmail.com` + `domain=company.example` pass the
    company check. A payload whose declared domain contradicts its own address
    is rejected outright (no check passes)."""
    if not normalized.get("verified"):
        return [
            CheckIntent(
                "verified_email",
                CheckStatus.FAIL,
                reason_codes=(ReasonCode.EMAIL_NOT_VERIFIED.value,),
                source=SOURCE,
            )
        ]

    email = normalized.get("email", "")
    email_domain = domain_of(email)  # authoritative: from the address itself
    if not email_domain:
        # a "verified" event without a usable address is incomplete evidence
        return [
            CheckIntent(
                "verified_email",
                CheckStatus.NEEDS_REVIEW,
                reason_codes=(ReasonCode.EMAIL_EVIDENCE_INCOMPLETE.value,),
                source=SOURCE,
            )
        ]
    declared_domain = domain_of(normalized.get("domain"))
    if declared_domain and declared_domain != email_domain:
        # self-contradictory event: neither check may pass on it
        detail = {"email_domain": email_domain, "declared_domain": declared_domain}
        return [
            CheckIntent(
                "verified_email",
                CheckStatus.FAIL,
                reason_codes=(ReasonCode.EMAIL_PAYLOAD_DOMAIN_CONFLICT.value,),
                source=SOURCE,
                source_detail=detail,
            ),
            CheckIntent(
                "verified_company_email",
                CheckStatus.FAIL,
                reason_codes=(ReasonCode.EMAIL_PAYLOAD_DOMAIN_CONFLICT.value,),
                source=SOURCE,
                source_detail=detail,
            ),
        ]

    detail = {"email_domain": email_domain}
    intents = [
        CheckIntent("verified_email", CheckStatus.PASS, source=SOURCE, source_detail=detail)
    ]

    submitted_domain = domain_of(
        case_snapshot.get("website") or case_snapshot.get("company_domain")
    )
    if email_domain in FREE_EMAIL_DOMAINS:
        intents.append(
            CheckIntent(
                "verified_company_email",
                CheckStatus.FAIL,
                reason_codes=(ReasonCode.EMAIL_FREE_OR_DISPOSABLE_DOMAIN.value,),
                source=SOURCE,
                source_detail=detail,
            )
        )
    elif not submitted_domain:
        # nothing submitted to prove "company email" against — never a silent
        # mismatch-FAIL, never a pass: a human (or a later submission) resolves
        intents.append(
            CheckIntent(
                "verified_company_email",
                CheckStatus.NEEDS_REVIEW,
                reason_codes=(ReasonCode.EMAIL_SUBMISSION_INCOMPLETE.value,),
                source=SOURCE,
                source_detail=detail,
            )
        )
    elif email_domain != submitted_domain:
        intents.append(
            CheckIntent(
                "verified_company_email",
                CheckStatus.FAIL,
                reason_codes=(ReasonCode.EMAIL_DOMAIN_MISMATCH.value,),
                source=SOURCE,
                source_detail={**detail, "submitted_domain": submitted_domain},
            )
        )
    else:
        intents.append(
            CheckIntent(
                "verified_company_email",
                CheckStatus.PASS,
                source=SOURCE,
                source_detail={**detail, "submitted_domain": submitted_domain},
            )
        )
    return intents
