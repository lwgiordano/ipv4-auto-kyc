"""Request/response models for the platform↔tool contract (04 §2).

Every event type has a payload model; unknown fields are preserved (the spec
says payload essentials are a minimum). The callback body model exists so
tests can validate what the outbox delivers.
"""

import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, get_args

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from kyc_tool.domain.provenance import (
    LATEST_MANUAL_ROW,
    LATEST_ROW,
    LEGACY_ORDER,
    NO_DECISIONS,
    NO_MANUAL_DECISIONS,
    POINTER_DRIFT,
)
from kyc_tool.ui.salesforce_projection import SALESFORCE_FIELD_CONTRACT

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


class _PlatformEventBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    occurred_at: datetime
    actor: Actor


class KybRunRequestedEvent(_PlatformEventBase):
    event_type: Literal["kyb.run_requested"]
    payload: KybRunPayload


class EmailVerifiedEvent(_PlatformEventBase):
    event_type: Literal["email.verified"]
    payload: EmailVerifiedPayload


class OrgIdSubmittedEvent(_PlatformEventBase):
    event_type: Literal["org_id.submitted"]
    payload: OrgIdSubmittedPayload


class PocSubmittedEvent(_PlatformEventBase):
    event_type: Literal["poc.submitted"]
    payload: PocSubmittedPayload


class PocTokenVerifiedEvent(_PlatformEventBase):
    event_type: Literal["poc.token_verified"]
    payload: PocTokenVerifiedPayload


class DocumentUploadedEvent(_PlatformEventBase):
    event_type: Literal["document.uploaded"]
    payload: DocumentUploadedPayload


class WebsiteReviewCompletedEvent(_PlatformEventBase):
    event_type: Literal["website.review_completed"]
    payload: WebsiteReviewCompletedPayload


class ReviewerManualApproveEvent(_PlatformEventBase):
    event_type: Literal["reviewer.manual_approve"]
    payload: ReviewerManualApprovePayload


class RecalculateRequestedEvent(_PlatformEventBase):
    event_type: Literal["recalculate.requested"]
    payload: RecalculatePayload = Field(default_factory=RecalculatePayload)


PlatformEvent = Annotated[
    KybRunRequestedEvent
    | EmailVerifiedEvent
    | OrgIdSubmittedEvent
    | PocSubmittedEvent
    | PocTokenVerifiedEvent
    | DocumentUploadedEvent
    | WebsiteReviewCompletedEvent
    | ReviewerManualApproveEvent
    | RecalculateRequestedEvent,
    Field(discriminator="event_type"),
]


def _event_variants() -> tuple[type[BaseModel], ...]:
    union = get_args(PlatformEvent)[0]
    return get_args(union)


def _event_type(model: type[BaseModel]) -> str:
    values = get_args(model.model_fields["event_type"].annotation)
    if len(values) != 1 or type(values[0]) is not str:
        raise TypeError(f"{model.__name__}.event_type must be one string Literal")
    return values[0]


PLATFORM_EVENT_MODELS: tuple[type[BaseModel], ...] = _event_variants()

PAYLOAD_MODELS: dict[str, type[BaseModel]] = {
    _event_type(model): model.model_fields["payload"].annotation for model in PLATFORM_EVENT_MODELS
}


class EventEnvelope(RootModel[PlatformEvent]):
    @property
    def event_type(self) -> EventType:
        return self.root.event_type

    @property
    def occurred_at(self) -> datetime:
        return self.root.occurred_at

    @property
    def actor(self) -> Actor:
        return self.root.actor

    @property
    def payload(self) -> dict[str, Any]:
        return self.root.payload.model_dump(mode="json")

    def validated_payload(self) -> dict[str, Any]:
        return self.payload

    def normalized(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "occurred_at": self.occurred_at.isoformat(),
            "actor": self.actor.model_dump(mode="json"),
            "payload": self.validated_payload(),
        }


class QueuedEventResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    status: Literal["queued"]


class ManualApprovalEventResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str
    case_status: Literal["approved_manual"]
    buy_status: Literal["buy_enabled", "buy_locked_org_id_required"]
    recorded: Literal[True]


class EventIngestErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    error: str
    event_id: str | None = None
    task_id: str | None = None
    detail: str | None = None


class EventHttpErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    detail: str | list[dict[str, Any]]


class EventAcceptedResponse(RootModel[QueuedEventResponse | ManualApprovalEventResponse]):
    pass


class EventFailureResponse(RootModel[EventIngestErrorResponse | EventHttpErrorResponse]):
    pass


EVENT_RESPONSE_MODELS: dict[int, type[BaseModel]] = {
    200: EventAcceptedResponse,
    202: QueuedEventResponse,
    400: EventHttpErrorResponse,
    401: EventHttpErrorResponse,
    404: EventIngestErrorResponse,
    409: EventIngestErrorResponse,
    422: EventFailureResponse,
    503: EventFailureResponse,
}


_RFC3339_DATETIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\Z")


def _parse_rfc3339_datetime(value: str) -> datetime:
    """Parse a wire datetime only where the metadata declares one.

    The public field union intentionally contains StrictStr before datetime.  A JSON roundtrip
    therefore needs this narrow conversion before union validation, rather than a permissive
    scalar conversion that could change text, integer, or boolean field semantics.
    """
    if not _RFC3339_DATETIME.fullmatch(value):
        raise ValueError("datetime values must use an RFC3339 date-time representation")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _parse_aware_datetime(value: Any, field_name: str) -> datetime:
    """Accept only an aware datetime object or its RFC3339 JSON representation."""
    if type(value) is str:
        parsed = _parse_rfc3339_datetime(value)
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise ValueError(f"{field_name} must be an aware datetime or RFC3339 date-time string")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a UTC offset")
    return parsed.astimezone(UTC)


class KycCheckChildRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    Check_Type__c: StrictStr
    Status__c: Literal["pass", "fail", "needs_review"]
    Points__c: StrictInt
    Category__c: StrictStr
    Source__c: StrictStr
    Superseded__c: StrictBool
    Reason_Codes__c: StrictStr
    Created_At__c: datetime

    @model_validator(mode="before")
    @classmethod
    def parse_created_at_json_value(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "Created_At__c" not in value:
            return value
        candidate = dict(value)
        candidate["Created_At__c"] = _parse_aware_datetime(value["Created_At__c"], "Created_At__c")
        return candidate


def validate_projected_value(source_field: str, value: Any) -> Any:
    """Enforce the exact runtime shape specified by the canonical field contract."""
    contract = SALESFORCE_FIELD_CONTRACT.get(source_field)
    if contract is None:
        raise ValueError(f"unknown Salesforce source field: {source_field}")
    if value is None:
        if contract.nullable:
            return value
        raise ValueError(f"{source_field} is not nullable")

    value_type = contract.value_type
    if value_type == "enum":
        if type(value) is not str or value not in contract.allowed_values:
            raise ValueError(f"{source_field} must be one of its declared enum values")
    elif value_type == "integer":
        if type(value) is not int:
            raise ValueError(f"{source_field} must be an exact integer")
    elif value_type == "text":
        if type(value) is not str:
            raise ValueError(f"{source_field} must be text")
    elif value_type == "boolean":
        if type(value) is not bool:
            raise ValueError(f"{source_field} must be an exact boolean")
    elif value_type == "datetime":
        if not isinstance(value, datetime):
            raise ValueError(f"{source_field} must be a datetime")
    elif value_type == "check_records":
        if type(value) is not list or any(not isinstance(item, KycCheckChildRecord) for item in value):
            raise ValueError(f"{source_field} must be a list of KycCheckChildRecord values")
    else:  # pragma: no cover - static metadata has one closed set of value types
        raise ValueError(f"unsupported Salesforce field type: {value_type}")
    return value


class SalesforceProjectionField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_field: str
    source_identity: str
    value_type: Literal["enum", "integer", "text", "boolean", "datetime", "check_records"]
    nullable: bool
    value: StrictStr | StrictInt | StrictBool | datetime | list[KycCheckChildRecord] | None

    @model_validator(mode="before")
    @classmethod
    def parse_metadata_declared_datetime_value(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        contract = SALESFORCE_FIELD_CONTRACT.get(value.get("source_field"))
        if contract is None:
            return value
        candidate = dict(value)
        if contract.value_type == "datetime":
            if candidate.get("value") is not None:
                candidate["value"] = _parse_aware_datetime(candidate["value"], contract.source_identity)
        elif contract.value_type == "check_records" and type(candidate.get("value")) is not list:
            raise ValueError(f"{contract.source_identity} must be a list of check records")
        return candidate

    @model_validator(mode="after")
    def validate_contract_metadata_and_value(self) -> "SalesforceProjectionField":
        contract = SALESFORCE_FIELD_CONTRACT.get(self.source_field)
        if contract is None:
            raise ValueError(f"unknown Salesforce source field: {self.source_field}")
        if self.source_identity != contract.source_identity:
            raise ValueError(f"{self.source_field} has an incorrect source identity")
        if self.value_type != contract.value_type:
            raise ValueError(f"{self.source_field} has an incorrect value type")
        if self.nullable is not contract.nullable:
            raise ValueError(f"{self.source_field} has incorrect nullability")
        validate_projected_value(self.source_field, self.value)
        return self


class ToolDecisionAuthority(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provenance: Literal[LATEST_ROW, POINTER_DRIFT, LEGACY_ORDER, NO_DECISIONS]
    decision_row_id: str | None
    run_id: str | None
    decision_kind: Literal["automatic", "manual"] | None
    decision: Literal["approve", "approve_buy_locked", "manual_review_insufficient", "reject"] | None
    run_provenance: Literal["resolved", "unresolved", "not_applicable"]

    @model_validator(mode="after")
    def validate_authority_matrix(self) -> "ToolDecisionAuthority":
        if self.provenance != LATEST_ROW:
            if (
                any(
                    value is not None
                    for value in (self.decision_row_id, self.run_id, self.decision_kind, self.decision)
                )
                or self.run_provenance != "not_applicable"
            ):
                raise ValueError("unresolved decision authority must contain only null identity values")
            return self
        if self.decision_row_id is None or self.decision_kind is None or self.decision is None:
            raise ValueError("resolved decision authority requires its decision identity")
        if self.decision_kind == "automatic":
            if self.run_id is None or self.run_provenance not in {"resolved", "unresolved"}:
                raise ValueError("automatic decision authority requires a run and its provenance")
        elif self.run_id is not None or self.run_provenance != "not_applicable":
            raise ValueError("manual decision authority has no run")
        return self


class ManualApprovalAuthority(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provenance: Literal[LATEST_MANUAL_ROW, POINTER_DRIFT, LEGACY_ORDER, NO_MANUAL_DECISIONS]
    decision_row_id: str | None
    reviewer_id: str | None
    decided_at: datetime | None

    @field_validator("decided_at", mode="before")
    @classmethod
    def parse_decided_at(cls, value: Any) -> Any:
        if value is None:
            return value
        return _parse_aware_datetime(value, "decided_at")

    @model_validator(mode="after")
    def validate_sticky_manual_matrix(self) -> "ManualApprovalAuthority":
        values = (self.decision_row_id, self.reviewer_id, self.decided_at)
        if self.provenance == LATEST_MANUAL_ROW:
            if any(value is None for value in values):
                raise ValueError("resolved manual authority requires its complete attribution")
        elif any(value is not None for value in values):
            raise ValueError("unresolved manual authority must contain only null identity values")
        return self


class SalesforceProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    mapping_revision: StrictStr | None
    configuration_revision: StrictStr | None
    decision_authority: ToolDecisionAuthority
    manual_approval_authority: ManualApprovalAuthority
    fields: dict[str, SalesforceProjectionField]
    projection_timestamp: datetime

    @field_validator("mapping_revision", "configuration_revision", mode="before")
    @classmethod
    def validate_decimal_revision(cls, value: Any) -> Any:
        if value is None:
            return value
        if type(value) is not str or not value.isascii() or not value.isdecimal():
            raise ValueError("revision must be a decimal string")
        return value

    @field_validator("projection_timestamp", mode="before")
    @classmethod
    def parse_projection_timestamp(cls, value: Any) -> Any:
        return _parse_aware_datetime(value, "projection_timestamp")

    @model_validator(mode="after")
    def validate_projection_contract(self) -> "SalesforceProjectionResponse":
        if set(field.source_field for field in self.fields.values()) != set(SALESFORCE_FIELD_CONTRACT):
            raise ValueError(
                "projection fields must cover every canonical Salesforce source field exactly once"
            )
        if len({field.source_field for field in self.fields.values()}) != len(self.fields):
            raise ValueError("projection fields cannot duplicate a Salesforce source field")
        authority = self.decision_authority
        is_resolved_automatic = (
            authority.provenance == LATEST_ROW
            and authority.decision_kind == "automatic"
            and authority.run_provenance == "resolved"
        )
        if not is_resolved_automatic and self.configuration_revision is not None:
            raise ValueError("only a resolved automatic decision may carry a configuration revision")
        return self


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

    computed_decision: Literal["approve", "approve_buy_locked", "manual_review_insufficient", "reject"]
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
