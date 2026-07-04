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
from kyc_tool.db.audit import audit
from kyc_tool.db.session import uow
from kyc_tool.db.tables import Case, DecisionRow, Event, Run
from kyc_tool.domain import scoring
from kyc_tool.domain.models import BuyStatus, CaseStatus
from kyc_tool.policy.loader import PolicyBundle
from kyc_tool.queue import jobs

MANUAL_APPROVE = "reviewer.manual_approve"


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    status_code: int
    body: dict


def payload_hash(envelope: dict) -> str:
    return hashlib.sha256(
        json.dumps(envelope, sort_keys=True, default=str).encode()
    ).hexdigest()


def _apply_submission(case: Case, event_type: str, payload: dict) -> None:
    """Merge submitted evidence into the case snapshot (RESOLVE_INPUTS data)."""
    snapshot = dict(case.submitted_json or {})
    if event_type == "kyb.run_requested":
        snapshot.update(payload)
        case.company_name = payload.get("company_legal_name") or case.company_name
        case.jurisdiction = payload.get("jurisdiction") or case.jurisdiction
        case.platform_account_id = payload.get("platform_account_id") or case.platform_account_id
    elif event_type == "email.verified":
        snapshot["email"] = payload
    elif event_type == "org_id.submitted":
        snapshot["org_id"] = payload
    elif event_type == "poc.submitted":
        snapshot["poc"] = payload
    elif event_type == "document.uploaded":
        snapshot["documents"] = [*snapshot.get("documents", []), payload]
    case.submitted_json = snapshot


def ingest_event(
    session_factory: sessionmaker[Session],
    policy: PolicyBundle,
    *,
    case_id: str,
    idempotency_key: str,
    envelope: dict,
) -> IngestOutcome:
    digest = payload_hash(envelope)
    event_type = envelope["event_type"]
    payload = envelope.get("payload") or {}
    actor = envelope.get("actor") or {}

    with uow(session_factory) as session:
        # lazy case creation (AUDIT:C1)
        session.execute(
            pg_insert(Case).values(id=case_id).on_conflict_do_nothing(index_elements=["id"])
        )

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
            )
            .on_conflict_do_nothing(index_elements=["idempotency_key"])
            .returning(Event.id)
        ).scalar_one_or_none()

        if inserted is None:
            # replay (or key misuse) — the original row is committed by now
            existing = session.execute(
                select(Event).where(Event.idempotency_key == idempotency_key)
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

        event = session.get(Event, inserted)
        case = session.get(Case, case_id, with_for_update=True)
        _apply_submission(case, event_type, payload)
        audit(
            session,
            "event.received",
            case_id=case_id,
            actor=str(actor.get("id", "platform")),
            event_id=event.id,
            event_type=event_type,
            idempotency_key=idempotency_key,
        )

        if event_type == MANUAL_APPROVE:
            body = _handle_manual_approve(session, policy, case, event, actor)
            event.response_snapshot = body
            event.processed_at = datetime.now(UTC)
            return IngestOutcome(200, body)

        run = Run(case_id=case_id, triggering_event_id=event.id, policy_bundle_hash=policy.bundle_hash)
        session.add(run)
        session.flush()
        event.run_id = run.id
        jobs.enqueue(session, "run_transition", {"run_id": run.id}, case_id=case_id)
        audit(session, "run.created", case_id=case_id, run_id=run.id, event_id=event.id)

        body = {"run_id": run.id, "status": "queued"}
        event.response_snapshot = body
        return IngestOutcome(202, body)


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
            buy_enablement="enabled" if org_passed else "locked_org_id_required",
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
