"""POC token validator.

Pass rule (03 §5): POC associated with the submitted ORG-ID/resource AND the
token sent to the RIR-listed email is verified. Token records are fetched by
orchestration and passed via extras (validators stay pure).
"""

import hashlib
from datetime import UTC, datetime

from kyc_tool.domain.models import CheckStatus
from kyc_tool.domain.reasons import ReasonCode
from kyc_tool.validators.base import CheckIntent

SOURCE = "rir_poc_record_plus_token"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def poc_token_intent(event_payload: dict, extras: dict, case_snapshot: dict) -> CheckIntent:
    raw_token = event_payload.get("token")
    tokens = extras.get("poc_tokens", [])
    if not raw_token:
        # TODO(integration) AUDIT:C2 — the platform must forward the raw token
        return CheckIntent(
            "poc_verified",
            CheckStatus.NEEDS_REVIEW,
            reason_codes=(ReasonCode.POC_TOKEN_INVALID.value,),
            source=SOURCE,
        )

    digest = hash_token(raw_token)
    match = next((t for t in tokens if t.get("token_hash") == digest), None)
    if match is None:
        return CheckIntent(
            "poc_verified",
            CheckStatus.FAIL,
            reason_codes=(ReasonCode.POC_TOKEN_INVALID.value,),
            source=SOURCE,
        )

    expired_at = match.get("expired_at")
    now = datetime.now(UTC)
    if expired_at is not None and expired_at <= now:
        return CheckIntent(
            "poc_verified",
            CheckStatus.FAIL,
            reason_codes=(ReasonCode.POC_TOKEN_EXPIRED.value,),
            source=SOURCE,
        )

    poc = case_snapshot.get("poc") or {}
    return CheckIntent(
        "poc_verified",
        CheckStatus.PASS,
        source=SOURCE,
        source_detail={
            "org_handle": poc.get("org_handle"),
            "poc_handle": poc.get("poc_handle"),
            "token_id": match.get("id"),
        },
    )
