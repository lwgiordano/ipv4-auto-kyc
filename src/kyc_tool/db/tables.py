"""SQLAlchemy schema — mirrors 04_API_AND_DATA_MODEL.md §4 plus the queue and
outbox tables the architecture adds. Enum-ish columns are TEXT on purpose:
values are governed by the policy JSONs / domain enums, and adding a value must
never require a migration.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _uuid() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class Case(Base):
    __tablename__ = "cases"

    # The platform addresses cases by its own id (path param); we adopt it as PK.
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    platform_account_id: Mapped[str | None] = mapped_column(Text)
    company_name: Mapped[str | None] = mapped_column(Text)
    jurisdiction: Mapped[str | None] = mapped_column(Text)
    submitted_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    # Per-case event counter (last assigned event_sequence). Incremented under a
    # FOR UPDATE lock in ingest so sequence allocation is race-free.
    event_sequence: Mapped[int] = mapped_column(BigInteger, server_default=text("0"), default=0)
    status: Mapped[str] = mapped_column(Text, default="kyc_pending")
    buy_status: Mapped[str] = mapped_column(Text, default="not_applicable")
    broker_status: Mapped[str] = mapped_column(Text, default="clear")
    current_score: Mapped[int] = mapped_column(Integer, default=0)
    latest_decision: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), onupdate=text("now()")
    )


class Event(Base):
    __tablename__ = "events"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_uuid)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    # D3 (PR 5a): idempotency is per-case, not global — the same key in another
    # case is an independent event, never a replay of the first.
    idempotency_key: Mapped[str] = mapped_column(Text)
    payload_hash: Mapped[str] = mapped_column(Text)
    # Gap-free per-case ordinal (D1: this is the wire's `event_sequence`).
    # sequence_backfilled marks rows whose sequence was RECONSTRUCTED by the 008
    # migration (arrival order) rather than assigned live at ingest.
    event_sequence: Mapped[int | None] = mapped_column(BigInteger)
    sequence_backfilled: Mapped[bool] = mapped_column(Boolean, server_default=text("false"), default=False)
    event_type: Mapped[str] = mapped_column(Text)
    actor_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    payload_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    response_snapshot: Mapped[dict | None] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    run_id: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (UniqueConstraint("case_id", "idempotency_key", name="uq_events_case_idempotency"),)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_uuid)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    triggering_event_id: Mapped[str] = mapped_column(ForeignKey("events.id"))
    # Frozen inputs the run evaluates — pinned at run creation, never mutated by
    # later events/runs/side effects. NULLABLE only for pre-008 historical runs.
    input_snapshot_json: Mapped[dict | None] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(Text, default="QUEUED")
    partial: Mapped[bool] = mapped_column(Boolean, default=False)
    policy_bundle_hash: Mapped[str | None] = mapped_column(Text)
    # PR 6 (migration 011): which engine build resolved/scored this run. Paired
    # with policy_bundle_hash above for full pinning provenance.
    engine_build_id: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class Job(Base):
    """Hand-rolled durable queue. Claimed with FOR UPDATE SKIP LOCKED + lease;
    per-case serialization comes from the claim predicate (no second running
    run_transition for the same case)."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(Text)  # run_transition | (reserved: adapter_call)
    case_id: Mapped[str | None] = mapped_column(Text, index=True)
    payload_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(Text, default="queued")  # queued|running|done|dead
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    locked_by: Mapped[str | None] = mapped_column(Text)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), onupdate=text("now()")
    )

    __table_args__ = (Index("ix_jobs_claim", "status", "run_after"),)


class AdapterResult(Base):
    __tablename__ = "adapter_results"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    adapter_id: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)  # ok | upstream_error | not_applicable
    raw_ref: Mapped[str | None] = mapped_column(Text)  # object-storage pointer
    normalized_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    input_hash: Mapped[str] = mapped_column(Text, default="")
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    latency_ms: Mapped[int | None] = mapped_column(Integer)

    # Resumable RUN_ADAPTERS: a retried transition skips already-recorded work.
    __table_args__ = (UniqueConstraint("run_id", "adapter_id", "input_hash", name="uq_adapter_run_input"),)


class Check(Base):
    """Immutable evidence checks. NEVER update a row except to stamp
    superseded_by_check_id inside the same transaction that inserts the
    successor. Live check = superseded_by_check_id IS NULL."""

    __tablename__ = "checks"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_uuid)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    check_type: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)  # pass | fail | needs_review
    points_awarded: Mapped[int] = mapped_column(Integer, default=0)
    category: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    source_detail_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    reason_codes: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    created_by_run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"))
    # Deferrable: the supersession stamp is written BEFORE its successor row
    # exists (same txn), so the live partial-unique index never sees two live
    # rows; the FK validates at commit.
    superseded_by_check_id: Mapped[str | None] = mapped_column(
        ForeignKey("checks.id", deferrable=True, initially="DEFERRED")
    )
    # PR 6 (migration 011): which policy bundle's rubric produced this check.
    policy_bundle_hash: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))

    # AUDIT:D3 — at most one live check per (case, type); enforced in migration 003
    # via a partial unique index (WHERE superseded_by_check_id IS NULL).


class ReviewTask(Base):
    __tablename__ = "review_tasks"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_uuid)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    task_type: Mapped[str] = mapped_column(Text)  # website | poc_email_unavailable
    context_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(Text, default="open")  # open | done
    result: Mapped[str | None] = mapped_column(Text)  # pass | fail
    reviewer_id: Mapped[str | None] = mapped_column(Text)
    reason_codes: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PocToken(Base):
    __tablename__ = "poc_tokens"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_uuid)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    poc_handle: Mapped[str] = mapped_column(Text)
    # Identity binding (migration 009): a token proves exactly one
    # (case, rir, poc_handle, org_handle/resource) — nullable for pre-009 tokens,
    # which then fail the tightened validator (fail-closed).
    rir: Mapped[str | None] = mapped_column(Text)
    org_handle: Mapped[str | None] = mapped_column(Text)
    resource: Mapped[str | None] = mapped_column(Text)
    rir_listed_email: Mapped[str] = mapped_column(Text)
    token_hash: Mapped[str] = mapped_column(Text)  # sha256; raw token never stored
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # single-use
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DecisionRow(Base):
    __tablename__ = "decisions"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_uuid)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"))
    decision: Mapped[str] = mapped_column(Text)
    score: Mapped[int] = mapped_column(Integer)
    gates_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    buy_enablement: Mapped[str] = mapped_column(Text)
    policy_shas: Mapped[dict] = mapped_column(JSONB, default=dict)  # audit provenance
    # PR 6 (migration 011): which engine build resolved/scored this decision.
    engine_build_id: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    manual: Mapped[bool] = mapped_column(Boolean, default=False)
    reviewer_id: Mapped[str | None] = mapped_column(Text)


class AuditLog(Base):
    """Append-only. No update/delete path exists in code; retention jobs are
    the only sanctioned remover."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    case_id: Mapped[str | None] = mapped_column(Text, index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    actor: Mapped[str] = mapped_column(Text, default="system")
    action: Mapped[str] = mapped_column(Text)
    detail_json: Mapped[dict] = mapped_column(JSONB, default=dict)


class BrokerEntity(Base):
    __tablename__ = "broker_entities"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(Text, unique=True)
    policy: Mapped[str] = mapped_column(Text)  # blocked | allowed
    aliases: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    domains: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    email_domains: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    org_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    poc_handles: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    asns: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    effective_date: Mapped[datetime | None] = mapped_column(Date)
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)


class Outbox(Base):
    """Transactional outbox: written in the decide txn, delivered by the
    publisher worker at-least-once (AUDIT:A6 — no exactly-once claims)."""

    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(Text)  # decision_callback | poc_email
    case_id: Mapped[str | None] = mapped_column(Text, index=True)
    run_id: Mapped[str | None] = mapped_column(Text)
    payload_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(Text, default="pending")  # pending|delivered|dead
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))

    __table_args__ = (Index("ix_outbox_claim", "status", "next_attempt_at"),)


class HmacV1Observation(Base):
    """Durable, cross-replica v1 acceptance witness (PR 5a §6). Single row
    (id=1), seeded INACTIVE by migration 010; the observation clock starts only
    at the post-cutover activation command. Updated fail-closed on every
    accepted inbound v1 request, so real v1 traffic is never silently invisible.
    """

    __tablename__ = "hmac_v1_observation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    observation_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_count: Mapped[int] = mapped_column(BigInteger, server_default=text("0"), default=0)
    last_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class HmacSignatureStat(Base):
    """Diagnostic v2/rejected signature counters (best-effort availability; the
    v1 witness above is the fail-closed one that gates the sunset)."""

    __tablename__ = "hmac_signature_stats"

    key: Mapped[str] = mapped_column(Text, primary_key=True)  # v2_accepted | rejected
    count: Mapped[int] = mapped_column(BigInteger, server_default=text("0"), default=0)
    last_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PolicyBundleRow(Base):
    """Durable policy-bundle store, keyed by content hash (PR 6, migration
    011). Rows are immutable once referenced by the pinning epoch or by a
    check/run/decision provenance column — downgrade refuses once any is used."""

    __tablename__ = "policy_bundles"

    bundle_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    files_json: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


class BundlePinningEpochRow(Base):
    """Single-row (id=1) activation epoch: the policy bundle + engine build
    currently pinned for new runs (PR 6, migration 011). Written only by the
    activation command, never by request-serving code paths."""

    __tablename__ = "bundle_pinning_epoch"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, server_default=text("1"))
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    bundle_hash: Mapped[str] = mapped_column(ForeignKey("policy_bundles.bundle_hash"))
    engine_build_id: Mapped[str] = mapped_column(Text)
