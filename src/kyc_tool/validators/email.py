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
    verified_company_email intents."""
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
    email_domain = domain_of(normalized.get("domain") or email)
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
    elif not submitted_domain or email_domain != submitted_domain:
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
