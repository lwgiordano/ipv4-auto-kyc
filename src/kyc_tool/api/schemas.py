"""Request/response models for the platform↔tool contract (04 §2).

Every event type has a payload model; unknown fields are preserved (the spec
says payload essentials are a minimum). The callback body model exists so
tests can validate what the outbox delivers.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

EventType = Literal[
    "kyb.run_requested",
    "email.verified",
    "org_id.submitted",
    "poc.submitted",
    "poc.token_verified",
    "document.uploaded",
    "website.review_completed",
    "reviewer.manual_approve",
    "recalculate.requested",
]

Rir = Literal["arin", "ripe", "apnic", "lacnic", "afrinic"]


class Actor(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["user", "reviewer", "system"]
    id: str


class KybRunPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    company_legal_name: str
    address: str | None = None
    jurisdiction: str | None = None
    website: str | None = None
    contact: dict | None = None
    platform_account_id: str | None = None


class EmailVerifiedPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    email: str
    domain: str
    verified_at: datetime


class OrgIdSubmittedPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    rir: Rir
    org_handle: str


class PocSubmittedPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    rir: Rir
    poc_handle: str
    org_handle: str | None = None
    resource: str | None = None


class PocTokenVerifiedPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    token_id: str
    verified_at: datetime
    # TODO(integration) AUDIT:C2 — platform forwards the raw token for hash check
    token: str | None = None


class DocumentUploadedPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    object_ref: str
    doc_type: str


class WebsiteReviewCompletedPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    task_id: str
    result: Literal["pass", "fail"]
    reviewer_id: str
    reason_codes: list[str] = Field(default_factory=list)


class ReviewerManualApprovePayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    reviewer_id: str
    note: str | None = None


class RecalculatePayload(BaseModel):
    model_config = ConfigDict(extra="allow")


PAYLOAD_MODELS: dict[str, type[BaseModel]] = {
    "kyb.run_requested": KybRunPayload,
    "email.verified": EmailVerifiedPayload,
    "org_id.submitted": OrgIdSubmittedPayload,
    "poc.submitted": PocSubmittedPayload,
    "poc.token_verified": PocTokenVerifiedPayload,
    "document.uploaded": DocumentUploadedPayload,
    "website.review_completed": WebsiteReviewCompletedPayload,
    "reviewer.manual_approve": ReviewerManualApprovePayload,
    "recalculate.requested": RecalculatePayload,
}


class EventEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_type: EventType
    occurred_at: datetime
    actor: Actor
    payload: dict = Field(default_factory=dict)

    def validated_payload(self) -> dict:
        model = PAYLOAD_MODELS[self.event_type]
        return model.model_validate(self.payload).model_dump(mode="json")


class GatesBody(BaseModel):
    score_met: bool
    legal_proof: bool
    control_proof: bool
    broker_ok: bool
    no_hard_conflict: bool


class CheckSummary(BaseModel):
    type: str
    status: str
    points: int
    source: str
    reason_codes: list[str] = Field(default_factory=list)


class DecisionCallback(BaseModel):
    """The authoritative decision body (04 §2)."""

    case_id: str
    run_id: str
    event_id: str
    decision: Literal["approve", "approve_buy_locked", "manual_review_insufficient", "reject"]
    score: int
    gates: GatesBody
    buy_enablement: Literal["enabled", "locked_org_id_required"]
    checks: list[CheckSummary]
    decided_at: datetime
    # D1: per-case ordinal of the triggering event. Present only when the M3
    # cutover flag (callback_include_event_sequence) is on; optional here so the
    # authoritative model preserves it on validate instead of dropping it.
    event_sequence: int | None = None
