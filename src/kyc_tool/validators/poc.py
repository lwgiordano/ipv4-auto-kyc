"""POC token validator.

Pass rule (03 §5): the POC is associated with the submitted ORG-ID/resource AND
the token sent to the RIR-listed email is verified. Token records are fetched by
orchestration and passed via extras (validators stay pure).

Binding + single-use (remediation item 5): a token proves exactly one
(case, token_id, digest, rir, poc_handle, ORG/resource) and exactly once. The
raw token is replaced by its digest at ingestion, so this validator matches on
the digest the event carried (`token_digest`) — it never sees or hashes a raw
token. A token minted for a different POC/RIR/ORG/resource, or already consumed,
can never pass.
"""

import hashlib
from datetime import UTC, datetime

from kyc_tool.domain.models import CheckStatus
from kyc_tool.domain.reasons import ReasonCode
from kyc_tool.validators.base import CheckIntent
from kyc_tool.validators.normalize import canon_id

SOURCE = "rir_poc_record_plus_token"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _lc(value) -> str:
    return (value or "").strip().lower()


def _fail(reason: ReasonCode) -> CheckIntent:
    return CheckIntent("poc_verified", CheckStatus.FAIL, reason_codes=(reason.value,), source=SOURCE)


def poc_token_intent(event_payload: dict, extras: dict, case_snapshot: dict) -> CheckIntent:
    token_id = event_payload.get("token_id")
    digest = event_payload.get("token_digest")  # ingestion replaced the raw token
    if not digest:
        # TODO(integration) AUDIT:C2 — the platform must forward the token
        return CheckIntent(
            "poc_verified",
            CheckStatus.NEEDS_REVIEW,
            reason_codes=(ReasonCode.POC_TOKEN_INVALID.value,),
            source=SOURCE,
        )

    poc = case_snapshot.get("poc") or {}
    cur_poc = canon_id(poc.get("poc_handle"))
    cur_rir = _lc(poc.get("rir"))
    cur_org = canon_id(poc.get("org_handle"))
    cur_resource = _lc(poc.get("resource"))
    if not cur_org and not cur_resource:
        # nothing for the POC to vouch for (fail-closed, item 3)
        return CheckIntent(
            "poc_verified",
            CheckStatus.NEEDS_REVIEW,
            reason_codes=(ReasonCode.POC_NO_ASSOCIATION_TARGET.value,),
            source=SOURCE,
        )

    tokens = extras.get("poc_tokens", [])
    match = next(
        (t for t in tokens if str(t.get("id")) == str(token_id) and t.get("token_hash") == digest),
        None,
    )
    if match is None:
        return _fail(ReasonCode.POC_TOKEN_INVALID)
    if match.get("consumed_at") is not None or match.get("verified_at") is not None:
        return _fail(ReasonCode.POC_TOKEN_CONSUMED)  # single-use

    expired_at = match.get("expired_at")
    if expired_at is not None and expired_at <= datetime.now(UTC):
        return _fail(ReasonCode.POC_TOKEN_EXPIRED)

    # the token must have been minted for the CURRENT identity — the COMPLETE
    # (rir, poc, org, resource) tuple, compared exactly. A blank dimension must
    # match a blank one: dropping the resource a token was minted for (leaving
    # only an unassociated org) is an identity change, not a free pass (D7).
    bound = (
        canon_id(match.get("poc_handle")) == cur_poc
        and _lc(match.get("rir")) == cur_rir
        and canon_id(match.get("org_handle")) == cur_org
        and _lc(match.get("resource")) == cur_resource
    )
    if not bound:
        return _fail(ReasonCode.POC_TOKEN_BINDING_MISMATCH)

    return CheckIntent(
        "poc_verified",
        CheckStatus.PASS,
        source=SOURCE,
        source_detail={
            "rir": poc.get("rir"),
            "org_handle": poc.get("org_handle"),
            "poc_handle": poc.get("poc_handle"),
            "resource": poc.get("resource"),
            "token_id": match.get("id"),
        },
    )
