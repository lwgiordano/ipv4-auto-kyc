"""Event ingestion — TXN-1. The ONLY way work enters the tool.

Guarantees (04 §2, AUDIT:C1/C3):
- Idempotency: INSERT … ON CONFLICT DO NOTHING on idempotency_key; a replay
  returns the stored response_snapshot verbatim (200). Same key with a
  different payload_hash is a client error (409).
- Case is created lazily on first event (AUDIT:C1 — no registration event).
- reviewer.manual_approve is handled inline: audit + approved_manual + buy
  recomputation, NO run, NO decision callback (AUDIT:C3).
- Everything else creates a run and enqueues one run_transition job in the
  same transaction (the queue is the outbox for work).
"""

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from kyc_tool.checkstore import repo as checkstore
from kyc_tool.config import get_settings
from kyc_tool.db.audit import audit
from kyc_tool.db.session import uow
from kyc_tool.db.tables import Case, DecisionRow, Event, ReviewTask, Run
from kyc_tool.domain import scoring
from kyc_tool.domain.decision import buy_enablement_for
from kyc_tool.domain.models import BuyStatus, CaseStatus
from kyc_tool.events.review_guard import reviewer_actor_reason
from kyc_tool.policy.loader import PolicyBundle
from kyc_tool.queue import jobs
from kyc_tool.validators.poc import hash_token

MANUAL_APPROVE = "reviewer.manual_approve"


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    status_code: int
    body: dict


class _FloorReject(Exception):  # noqa: N818 — internal control-flow signal, not an *Error
    """Raised inside the ingest txn to reject an event and roll the txn back, so
    a rejected completion leaves no orphan event row (PR 5a §4)."""

    def __init__(self, outcome: IngestOutcome) -> None:
        self.outcome = outcome


def _validate_website_review_completed(
    session: Session, case_id: str, actor: dict, payload: dict
) -> IngestOutcome | None:
    """PR 5a §4 floor: a keyed `website.review_completed` must reference a real,
    open, same-case website task. Runs AFTER replay resolution and BEFORE the
    run/check. PR 5b adds the reviewer-actor trust floor: the signed envelope's
    actor must be a consistent, nonblank reviewer matching payload.reviewer_id."""
    if reviewer_actor_reason(actor, payload) is not None:
        return IngestOutcome(422, {"error": "invalid reviewer actor", "task_id": payload.get("task_id")})
    task_id = payload.get("task_id")
    task = session.get(ReviewTask, task_id) if task_id else None
    if task is None:
        return IngestOutcome(404, {"error": "review task not found", "task_id": task_id})
    if task.task_type != "website":
        return IngestOutcome(422, {"error": "not a website review task", "task_id": task_id})
    if task.case_id != case_id:
        return IngestOutcome(409, {"error": "task belongs to a different case", "task_id": task_id})
    if task.status != "open":
        return IngestOutcome(409, {"error": "review task not open", "task_id": task_id})
    return None


def payload_hash(envelope: dict) -> str:
    return hashlib.sha256(json.dumps(envelope, sort_keys=True, default=str).encode()).hexdigest()


def _scrub_secrets(event_type: str, payload: dict) -> dict:
    """Replace at-rest secrets with a digest BEFORE the event is persisted.

    The idempotency digest is taken from the ORIGINAL envelope (see
    ingest_event), so scrubbing never affects replay/409 detection. A
    poc.token_verified carries a raw single-use token only in transit; the
    events table keeps its digest — what the validator matches on — never the
    token itself (item 5).
    """
    if event_type == "poc.token_verified" and payload.get("token"):
        scrubbed = dict(payload)
        scrubbed["token_digest"] = hash_token(scrubbed.pop("token"))
        return scrubbed
    return payload


def _apply_event_to_snapshot(case: Case, event_type: str, payload: dict) -> dict:
    """Return a NEW cumulative snapshot with this event's evidence merged in.

    Pure w.r.t. the case: it copies `case.submitted_json` and never mutates it,
    so the caller can pin the result on the run (frozen inputs) independently of
    the case's evolving snapshot.
    """
    snapshot = dict(case.submitted_json or {})
    if event_type == "kyb.run_requested":
        snapshot.update(payload)
    elif event_type == "email.verified":
        snapshot["email"] = payload
    elif event_type == "org_id.submitted":
        snapshot["org_id"] = payload
    elif event_type == "poc.submitted":
        snapshot["poc"] = payload
    elif event_type == "document.uploaded":
        snapshot["documents"] = [*snapshot.get("documents", []), payload]
    return snapshot


def _update_case_metadata(case: Case, event_type: str, payload: dict) -> None:
    """Denormalized case-level display fields (not run inputs)."""
    if event_type == "kyb.run_requested":
        case.company_name = payload.get("company_legal_name") or case.company_name
        case.jurisdiction = payload.get("jurisdiction") or case.jurisdiction
        case.platform_account_id = payload.get("platform_account_id") or case.platform_account_id


def ingest_event(
    session_factory: sessionmaker[Session],
    policy: PolicyBundle,
    *,
    case_id: str,
    idempotency_key: str,
    envelope: dict,
) -> IngestOutcome:
    digest = payload_hash(envelope)  # from the ORIGINAL envelope — before scrubbing
    event_type = envelope["event_type"]
    payload = _scrub_secrets(event_type, envelope.get("payload") or {})
    actor = envelope.get("actor") or {}

    try:
        with uow(session_factory) as session:
            # lazy case creation (AUDIT:C1)
            session.execute(pg_insert(Case).values(id=case_id).on_conflict_do_nothing(index_elements=["id"]))
            # Lock the case BEFORE allocating a sequence — this serializes concurrent
            # same-case ingestion (incl. reviewer.manual_approve), so the per-case
            # event_sequence is race-free.
            case = session.get(Case, case_id, with_for_update=True)
            next_sequence = case.event_sequence + 1

            inserted = session.execute(
                pg_insert(Event)
                .values(
                    id=uuid.uuid4().hex,
                    case_id=case_id,
                    idempotency_key=idempotency_key,
                    payload_hash=digest,
                    event_type=event_type,
                    actor_json=actor,
                    payload_json=payload,
                    event_sequence=next_sequence,
                    sequence_backfilled=False,  # assigned live
                )
                .on_conflict_do_nothing(index_elements=["case_id", "idempotency_key"])
                .returning(Event.id)
            ).scalar_one_or_none()

            if inserted is None:
                # replay (or key misuse) WITHIN this case — the sequence was NOT
                # consumed. D3: the same key in another case never lands here.
                existing = session.execute(
                    select(Event).where(
                        Event.case_id == case_id,
                        Event.idempotency_key == idempotency_key,
                    )
                ).scalar_one()
                if existing.payload_hash != digest:
                    return IngestOutcome(
                        409,
                        {
                            "error": "idempotency key reuse with different payload",
                            "event_id": existing.id,
                        },
                    )
                return IngestOutcome(200, dict(existing.response_snapshot or {}))

            # genuine new event — commit the sequence and pin the frozen snapshot
            case.event_sequence = next_sequence
            event = session.get(Event, inserted)
            new_snapshot = _apply_event_to_snapshot(case, event_type, payload)
            case.submitted_json = new_snapshot
            _update_case_metadata(case, event_type, payload)
            audit(
                session,
                "event.received",
                case_id=case_id,
                actor=str(actor.get("id", "platform")),
                event_id=event.id,
                event_type=event_type,
                event_sequence=next_sequence,
                idempotency_key=idempotency_key,
            )

            if event_type == MANUAL_APPROVE:
                if reviewer_actor_reason(actor, payload) is not None:
                    raise _FloorReject(IngestOutcome(422, {"error": "invalid reviewer actor"}))
                body = _handle_manual_approve(session, policy, case, event, actor)
                event.response_snapshot = body
                event.processed_at = datetime.now(UTC)
                return IngestOutcome(200, body)

            # PR 5a §4 floor: reject an invalid review completion BEFORE the
            # run/check; _FloorReject rolls the whole txn back (no orphan row).
            if event_type == "website.review_completed":
                reject = _validate_website_review_completed(session, case_id, actor, payload)
                if reject is not None:
                    raise _FloorReject(reject)

            run = Run(
                case_id=case_id,
                triggering_event_id=event.id,
                policy_bundle_hash=policy.bundle_hash,
                input_snapshot_json=dict(new_snapshot),  # freeze the run's inputs
            )
            session.add(run)
            session.flush()
            event.run_id = run.id
            jobs.enqueue(
                session,
                "run_transition",
                {"run_id": run.id},
                case_id=case_id,
                max_attempts=get_settings().job_max_attempts,
            )
            audit(session, "run.created", case_id=case_id, run_id=run.id, event_id=event.id)

            body = {"run_id": run.id, "status": "queued"}
            event.response_snapshot = body
            return IngestOutcome(202, body)
    except _FloorReject as fr:
        return fr.outcome


def _handle_manual_approve(
    session: Session, policy: PolicyBundle, case: Case, event: Event, actor: dict
) -> dict:
    """AUDIT:C3 — record-only. The platform already enforced the approval;
    score and gates are bypassed; buy enablement still requires ORG-ID."""
    views = [checkstore.as_view(c) for c in checkstore.live_checks(session, case.id)]
    org_passed = scoring.org_id_check_passed(views)

    case.status = CaseStatus.APPROVED_MANUAL.value
    case.buy_status = (
        BuyStatus.BUY_ENABLED.value if org_passed else BuyStatus.BUY_LOCKED_ORG_ID_REQUIRED.value
    )
    reviewer_id = str(actor.get("id", "unknown"))
    session.add(
        DecisionRow(
            case_id=case.id,
            run_id=None,
            decision="approve",
            score=case.current_score,
            gates_json={"bypassed": True},
            buy_enablement=buy_enablement_for(org_passed).value,
            policy_shas=policy.shas,
            manual=True,
            reviewer_id=reviewer_id,
        )
    )
    audit(
        session,
        "reviewer.manual_approve",
        case_id=case.id,
        actor=reviewer_id,
        event_id=event.id,
        note=(event.payload_json or {}).get("note"),
        buy_enablement=case.buy_status,
    )
    return {
        "case_id": case.id,
        "case_status": case.status,
        "buy_status": case.buy_status,
        "recorded": True,
    }
