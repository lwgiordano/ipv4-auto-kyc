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
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
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
    # PR 7b-core (migration 013): per-case decision-callback sequence counter,
    # incremented under the Case FOR UPDATE lock in _decide_txn (never max()+1).
    last_decision_sequence: Mapped[int] = mapped_column(
        BigInteger, server_default=text("0"), default=0
    )
    status: Mapped[str] = mapped_column(Text, default="kyc_pending")
    buy_status: Mapped[str] = mapped_column(Text, default="not_applicable")
    broker_status: Mapped[str] = mapped_column(Text, default="clear")
    current_score: Mapped[int] = mapped_column(Integer, default=0)
    latest_decision: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), onupdate=text("now()")
    )
    # PR 7b-core (migration 014): the atomic latest-decision authority. Maintained by a DB
    # trigger on decisions INSERT (same transaction, both decide paths), NEVER derived from
    # decided_at — now() is transaction-start time and can invert against the lock-serialized
    # commit order, which is exactly how the read API once paired a decision with the previous
    # decision's gates. Composite FK pins it to a decision of THIS case.
    latest_decision_row_id: Mapped[str | None] = mapped_column(Text)
    # Migration 018: the MANUAL pointer, maintained by its own INSERT trigger. Separate from the
    # pointer above because manual attribution is sticky — a later automatic decision moves the
    # verdict pointer but must never blank who approved the case (re-audit `cbb783b` F6). Sorting
    # `decisions` to find "the latest manual row" is not an option: `id` is a random UUID hex and
    # `decided_at` is transaction-start time. NULL means unresolved, never "no manual approval":
    # a legacy case with two or more manual rows cannot be ordered and is left explicitly unknown.
    latest_manual_decision_row_id: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        ForeignKeyConstraint(
            ["latest_decision_row_id", "id"],
            ["decisions.id", "decisions.case_id"],
            name="fk_cases_latest_decision",
            use_alter=True,  # cases↔decisions FKs are circular at the table level
        ),
        ForeignKeyConstraint(
            ["latest_manual_decision_row_id", "id"],
            ["decisions.id", "decisions.case_id"],
            name="fk_cases_latest_manual_decision",
            use_alter=True,
        ),
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

    # PR 7b-core (migration 013): named composite target for fk_decisions_run_case —
    # decisions(run_id, case_id) must cite a run under its OWN case.
    __table_args__ = (UniqueConstraint("id", "case_id", name="uq_runs_id_case_id"),)


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
    # PR 7b-core (migration 013): internal per-case ordinal for callback-emitting
    # (automatic) decisions; NULL for manual approvals. NOT on the wire.
    decision_sequence: Mapped[int | None] = mapped_column(BigInteger)
    manual: Mapped[bool] = mapped_column(Boolean, default=False)
    reviewer_id: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("run_id", name="uq_decisions_run_id"),
        UniqueConstraint("case_id", "decision_sequence", name="uq_decisions_case_decision_sequence"),
        UniqueConstraint("run_id", "case_id", "decision_sequence", name="uq_decisions_run_case_sequence"),
        # target of cases.fk_cases_latest_decision (migration 014): PG requires the referenced
        # column pair to carry its own unique constraint even though id alone is the PK.
        UniqueConstraint("id", "case_id", name="uq_decisions_id_case_id"),
        ForeignKeyConstraint(
            ["run_id", "case_id"], ["runs.id", "runs.case_id"], name="fk_decisions_run_case"
        ),
    )


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
    # NOT NULL + named FK (migration 013). The FK lives in ORM metadata too (re-audit F9):
    # Base.metadata is Alembic's comparison target, so a live-only FK would report drift.
    case_id: Mapped[str] = mapped_column(
        Text, ForeignKey("cases.id", name="fk_outbox_case_id"), index=True
    )
    run_id: Mapped[str | None] = mapped_column(Text)
    # PR 7b-core: FIFO stream this row is claimed under (decision | email), NOT NULL.
    ordering_stream: Mapped[str] = mapped_column(Text)
    # Internal per-case ordinal for decision_callback rows (NULL for poc_email).
    decision_sequence: Mapped[int | None] = mapped_column(BigInteger)
    payload_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(Text, default="pending")  # pending|delivered|dead|superseded
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # superseded terminal timestamp (never sent); mutually exclusive with delivered_at.
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Per-claim fence: the three move together (all-NULL or all-non-NULL).
    claim_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    claimed_by: Mapped[str | None] = mapped_column(Text)
    # PR 7b-core (013): the recorded digest of the bytes actually sent + which encoding was
    # used. Written in the delivery transaction; NULL for undelivered rows and for anything
    # delivered before 013 (jsonb normalizes key order, so those sent bytes are unrecoverable).
    callback_wire_sha256: Mapped[str | None] = mapped_column(Text)
    wire_version: Mapped[str | None] = mapped_column(Text)
    # PR 7b-core (migration 014): which witness regime this row was created under. 'legacy'
    # rows predate the pre-HTTP attempt authority, so an absent attempt proves nothing about
    # them; only an 'attempt_v1' row with no attempt is provably never-transmitted. The DB
    # default is 'legacy' — the fail-closed direction: a write path that forgets to stamp
    # degrades to over-caution (legacy_unwitnessed), never to a false proof of non-delivery.
    witness_generation: Mapped[str] = mapped_column(
        Text, server_default=text("'legacy'"), default="legacy"
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))

    __table_args__ = (
        # PR 7b-core (013): partial to the claimable set — decision_callback terminals are never
        # pruned, so a full index would grow without bound and enter every claim plan.
        Index("ix_outbox_claim", "next_attempt_at", postgresql_where=text("status = 'pending'")),
        Index(
            "ix_outbox_stream_claim", "case_id", "ordering_stream", "next_attempt_at",
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "uq_outbox_decision_callback_run", "run_id", unique=True,
            postgresql_where=text("kind='decision_callback'"),
        ),
        ForeignKeyConstraint(
            ["run_id", "case_id", "decision_sequence"],
            ["decisions.run_id", "decisions.case_id", "decisions.decision_sequence"],
            name="fk_outbox_decision_triple",
        ),
        # migration 014: the live alerting predicate is (pending, dead); 013's pending-only
        # partial indexes cannot serve it, and terminal history grows for the life of the system.
        Index(
            "ix_outbox_live_status", "status",
            postgresql_where=text("status IN ('pending','dead')"),
        ),
        CheckConstraint(
            "witness_generation IN ('legacy','attempt_v1')", name="ck_outbox_witness_generation"
        ),
        # a terminal digest is only writable on a delivered row — otherwise a raw digest on a
        # pending row reads as delivery_witnessed and licenses deleting its real attempts.
        CheckConstraint(
            "callback_wire_sha256 IS NULL OR status = 'delivered'",
            name="ck_outbox_wire_witness_delivered",
        ),
    )


class OutboxDeliveryAttempt(Base):
    """Immutable record that specific bytes were durably STAGED for the wire (PR 7b-core, 014).

    Written and COMMITTED before the HTTP send, under the claim that authorized it. Committed
    intent is exactly what it proves — no more: the process can die between this commit and the
    send, so a row here means the bytes MAY have reached the platform, and only the platform's
    accepted-request ledger settles it. What it rules out is the opposite lie: without it, the
    send-before-stamp residual (2xx received, terminal transaction faults, row stays `pending`)
    leaves a NULL terminal digest that reads as "never sent" precisely when the platform holds
    the bytes.

    Insert-only, enforced by a DB trigger (migration 014): UPDATE always refused; DELETE refused
    unless the parent row carries a terminal digest, because for any other row this is the sole
    evidence of staging. There is deliberately no "this attempt won" column: identical bytes are
    one event to the receiver, so a winner flag would be a derived fact free to contradict its
    own source.
    """

    __tablename__ = "outbox_delivery_attempts"

    attempt_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    outbox_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("outbox.id", name="fk_attempt_outbox", ondelete="CASCADE")
    )
    claim_token: Mapped[str] = mapped_column(UUID(as_uuid=False))
    wire_version: Mapped[str] = mapped_column(Text)  # legacy | sequenced
    request_sha256: Mapped[str] = mapped_column(Text)
    # migration 016: which regime ADMITTED this attempt. 'admission_v1' is stamped only by the
    # admission trigger (it overwrites whatever the INSERT carried); pre-authority rows are
    # 'legacy_unverified' and the witness taxonomy refuses to treat them as staged-intent proof.
    admission: Mapped[str] = mapped_column(
        Text, server_default=text("'legacy_unverified'"), default="legacy_unverified"
    )
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )

    __table_args__ = (
        Index("ix_attempt_outbox", "outbox_id", text("attempted_at DESC")),
        CheckConstraint("request_sha256 ~ '^[0-9a-f]{64}$'", name="ck_attempt_sha_shape"),
        CheckConstraint("wire_version IN ('legacy','sequenced')", name="ck_attempt_wire_vocab"),
        CheckConstraint(
            "admission IN ('legacy_unverified','admission_v1')",
            name="ck_attempt_admission_vocab",
        ),
    )


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
