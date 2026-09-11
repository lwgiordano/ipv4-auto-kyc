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
    # Declared for contract clarity (was accepted via extra="allow"). Optional:
    # incomplete submissions stay ingestible, but the fail-closed validators
    # never PASS on absent evidence.
    registration_number: str | None = None
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
    # AUDIT:C2 — the platform echoes the raw token from the verification email.
    # Required: a token-less verification is rejected at ingestion (422), matching
    # the documented contract, not queued and later routed to review.
    token: str


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
    # Strict, unlike the INBOUND payload models above (re-gate-3 finding 3). The tolerance
    # asymmetry is the contract's own: we ignore unknown fields the platform sends us, and we
    # never emit a field the contract does not declare. Root-only strictness left every nested
    # outbound object permissive, so a future gate could be silently dropped here while leaking
    # through any path that skipped the encoder.
    model_config = ConfigDict(extra="forbid")

    score_met: bool
    legal_proof: bool
    control_proof: bool
    broker_ok: bool
    no_hard_conflict: bool


class CheckSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    status: str
    points: int
    source: str
    reason_codes: list[str] = Field(default_factory=list)


class EnforcementHeld(BaseModel):
    """The enforcement-hold block, typed (re-gate-3 finding 3). As a plain `dict` it accepted
    arbitrary keys — the one nested object that was not merely dropping unknowns but PUBLISHING
    them."""

    model_config = ConfigDict(extra="forbid")

    computed_decision: Literal[
        "approve", "approve_buy_locked", "manual_review_insufficient", "reject"
    ]
    reason: str


class DecisionCallback(BaseModel):
    """The authoritative decision body (04 §2).

    `extra="forbid"` (re-gate finding 3): permissive extras meant an unknown field was silently
    DROPPED at the encoder while the same field leaked through any path that skipped it. Refusing
    is the honest behaviour — a post-024 ordering key arriving before 024 exists is a defect to
    surface, not a value to quietly discard.
    """

    model_config = ConfigDict(extra="forbid")

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
    # Present while the temporary enforcement hold is active: a positive decision
    # downgraded to manual review carries the computed decision here. Modeled so
    # the authoritative body preserves it on validate instead of silently
    # dropping it (platform behavior depends on it).
    enforcement_held: EnforcementHeld | None = None


# The wire's ordering generation. `unsequenced` means the callback carries NO platform ordering
# key; it becomes `sequenced` only when migration 025 ships and `decision_sequence` joins the
# authoritative model. Declared here, beside the model, so the pending-025 gate has one authority
# to interrogate instead of two declarations that can drift (re-audit Wave 0 gate finding 6).


def encode_decision_callback(payload: dict) -> dict:
    """The ONE place a decision callback body is produced.

    Gate finding 6: `Pipeline._callback_body` built a plain dict and handed it straight to the
    outbox, so the authoritative model was a declaration nothing enforced. Adding
    `decision_sequence` to the emitter while leaving `DecisionCallback` untouched put the post-024
    ordering key on the wire with every check green — the "joint" 024 guard was watching a model
    the publisher did not use.

    Routing the body through validation makes the model load-bearing rather than descriptive: an
    unmodelled key is REFUSED here (`extra="forbid"`, re-gate finding 3 — the first version
    dropped it, which hid the divergence instead of surfacing it), so an emitter cannot publish a
    field the contract does not declare. `mode="json"` keeps `decided_at` an ISO string as the
    wire requires, and `exclude_none` keeps optional fields absent rather than explicitly null.
    """
    return DecisionCallback.model_validate(payload).model_dump(mode="json", exclude_none=True)
