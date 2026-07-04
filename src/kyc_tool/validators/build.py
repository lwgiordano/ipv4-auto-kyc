"""Intent builder — the single dispatch from (event, adapter outputs) to check
intents. Pure: everything it needs arrives in the ValidationContext.

Phase 3 adds: rir_rdap (ORG-ID) validation with needs_review routing, the
LinkedIn deterministic match, and registry↔RIR hard-conflict stamping.
"""

from kyc_tool.domain.models import CheckStatus
from kyc_tool.policy.loader import PolicyBundle
from kyc_tool.validators.base import CheckIntent, ValidationContext
from kyc_tool.validators.documents import document_intent
from kyc_tool.validators.email import email_intents
from kyc_tool.validators.poc import poc_token_intent
from kyc_tool.validators.registry import registry_intent
from kyc_tool.validators.website import website_intent


def build_intents(policy: PolicyBundle, ctx: ValidationContext) -> list[CheckIntent]:
    intents: list[CheckIntent] = []

    if "email_verification" in ctx.adapter_outputs:
        intents.extend(
            email_intents(ctx.adapter_outputs["email_verification"], ctx.case_snapshot)
        )

    registry = registry_intent(ctx.adapter_outputs, ctx.case_snapshot)
    if registry is not None:
        intents.append(registry)

    if "document_ocr" in ctx.adapter_outputs:
        intents.append(
            document_intent(
                ctx.adapter_outputs["document_ocr"], ctx.case_snapshot, ctx.live_checks
            )
        )

    if ctx.event_type == "website.review_completed":
        intents.append(website_intent(ctx.event_payload))

    if ctx.event_type == "poc.token_verified":
        intents.append(poc_token_intent(ctx.event_payload, ctx.extras, ctx.case_snapshot))

    # Never award a check that isn't in the rubric, and never let a non-pass
    # intent slip through with points (defense in depth; the store re-checks).
    valid_types = set(policy.rubric.check_types)
    for intent in intents:
        assert intent.check_type in valid_types, f"unknown check type: {intent.check_type}"
        assert isinstance(intent.status, CheckStatus)
    return intents
