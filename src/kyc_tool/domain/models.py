"""Pure domain types. Frozen; no I/O, no ORM.

Enum string values are the wire/database representations — they come from the
normative spec (state_machine.json, decision_policy.json, adapter contract).
"""

from dataclasses import dataclass, field
from enum import StrEnum


class Decision(StrEnum):
    APPROVE = "approve"
    APPROVE_BUY_LOCKED = "approve_buy_locked"
    MANUAL_REVIEW_INSUFFICIENT = "manual_review_insufficient"
    REJECT = "reject"


class BuyEnablement(StrEnum):
    ENABLED = "enabled"
    LOCKED_ORG_ID_REQUIRED = "locked_org_id_required"


class BrokerStatus(StrEnum):
    CLEAR = "clear"
    ALLOWED_BROKER = "allowed_broker"
    BLOCKED = "blocked"


class CheckStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    NEEDS_REVIEW = "needs_review"


class RunState(StrEnum):
    QUEUED = "QUEUED"
    RESOLVE_INPUTS = "RESOLVE_INPUTS"
    BROKER_GATE = "BROKER_GATE"
    RUN_ADAPTERS = "RUN_ADAPTERS"
    VALIDATE = "VALIDATE"
    WRITE_CHECKS = "WRITE_CHECKS"
    SCORE = "SCORE"
    DECIDE = "DECIDE"
    PUBLISH_DECISION = "PUBLISH_DECISION"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


class CaseStatus(StrEnum):
    REGISTERED = "registered"
    EMAIL_VERIFICATION_PENDING = "email_verification_pending"
    EMAIL_VERIFIED = "email_verified"
    ENRICHMENT_RUNNING = "enrichment_running"
    KYC_PENDING = "kyc_pending"
    MANUAL_REVIEW_INSUFFICIENT = "manual_review_insufficient"
    ACCOUNT_APPROVED = "account_approved"
    APPROVED_MANUAL = "approved_manual"
    REJECTED = "rejected"


class BuyStatus(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    BUY_LOCKED_ORG_ID_REQUIRED = "buy_locked_org_id_required"
    ORG_ID_VALIDATION_PENDING = "org_id_validation_pending"
    ORG_ID_FAILED = "org_id_failed"
    BUY_ENABLED = "buy_enabled"
    BUY_SUSPENDED = "buy_suspended"


class AdapterStatus(StrEnum):
    OK = "ok"
    UPSTREAM_ERROR = "upstream_error"
    NOT_APPLICABLE = "not_applicable"


class RejectReason(StrEnum):
    """v1: only the exact-broker match rejects (AUDIT:B3 — 'hard_block' is
    undefined in the spec; the enum stays extensible so defining it later is
    additive)."""

    BLOCKED_BROKER_EXACT_MATCH = "blocked_broker_exact_match"


@dataclass(frozen=True, slots=True)
class CheckView:
    """A live check as the scoring/decision layer sees it."""

    check_type: str
    status: CheckStatus
    points_awarded: int
    category: str
    source: str
    reason_codes: tuple[str, ...] = ()
    source_detail: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Gates:
    """The five hard gates (AUDIT:A2 — five, not the prose's 'four')."""

    score_met: bool
    legal_proof: bool
    control_proof: bool
    broker_ok: bool
    no_hard_conflict: bool

    @property
    def all_pass(self) -> bool:
        return (
            self.score_met
            and self.legal_proof
            and self.control_proof
            and self.broker_ok
            and self.no_hard_conflict
        )

    def as_dict(self) -> dict[str, bool]:
        return {
            "score_met": self.score_met,
            "legal_proof": self.legal_proof,
            "control_proof": self.control_proof,
            "broker_ok": self.broker_ok,
            "no_hard_conflict": self.no_hard_conflict,
        }


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    score: int
    by_check: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DecisionResult:
    decision: Decision
    score: int
    gates: Gates
    buy_enablement: BuyEnablement
    reject_reason: RejectReason | None = None
