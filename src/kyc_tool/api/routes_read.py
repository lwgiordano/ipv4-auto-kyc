"""Read endpoints (04 §2). Review completion is the keyed website.review_completed
event (PR 5a §4), no longer a dedicated endpoint."""

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import select, text

from kyc_tool.api.auth import require_read_access
from kyc_tool.checkstore import repo as checkstore
from kyc_tool.db.tables import Case, DecisionRow, ReviewTask, Run

router = APIRouter()


def _check_json(check) -> dict:
    return {
        "id": check.id,
        "type": check.check_type,
        "status": check.status,
        "points": check.points_awarded,
        "category": check.category,
        "source": check.source,
        "reason_codes": list(check.reason_codes or []),
        "superseded_by_check_id": check.superseded_by_check_id,
        "created_at": check.created_at.isoformat(),
    }


@router.get("/v1/cases/{case_id}")
def get_case(case_id: str, request: Request) -> dict:
    require_read_access(request.app.state.settings, request)
    with request.app.state.session_factory() as session:
        case = session.get(Case, case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="case not found")
        live = checkstore.live_checks(session, case_id)
        # The WHOLE decision tuple — value, gates, decision-time score — comes from the ONE row
        # `cases.latest_decision_row_id` points at (trigger-maintained, same transaction as every
        # decision insert), NEVER from decided_at ordering and never mixed with the projection:
        # decided_at is transaction-start time and inverts against commit order, and the
        # projection's `latest_decision` is not updated by the record-only manual-approve path —
        # serving projection-value + pointer-gates returned reject/bypassed-gates hybrids
        # (re-audit 4dfdf8a F4). `decision_provenance` makes the remaining ambiguity OBSERVABLE:
        # a NULL pointer with decisions present is a pre-014 manual-among-several history whose
        # write order has no durable record; it serves no decision tuple rather than a guess, is
        # counted in /v1/metrics, and heals on the case's next decision.
        latest = None
        if case.latest_decision_row_id:
            latest = session.execute(
                select(DecisionRow).where(
                    DecisionRow.id == case.latest_decision_row_id,
                    DecisionRow.case_id == case_id,
                )
            ).scalar_one_or_none()
        if latest is not None:
            provenance = "latest_decision_row"
        elif session.execute(
            select(DecisionRow.id).where(DecisionRow.case_id == case_id).limit(1)
        ).first():
            provenance = "unresolved_legacy_order"
        else:
            provenance = "no_decisions"
        return {
            "case_id": case.id,
            "status": case.status,
            "buy_status": case.buy_status,
            "broker_status": case.broker_status,
            "company_name": case.company_name,
            "jurisdiction": case.jurisdiction,
            # live case score (recomputed as checks land) — distinct from the decision-time score
            "score": case.current_score,
            "latest_decision": (latest.decision if latest else None),
            "decision_score": (latest.score if latest else None),
            "gates": (latest.gates_json if latest else {}),
            "decision_provenance": provenance,
            "live_checks": [_check_json(c) for c in live],
        }


@router.get("/v1/cases/{case_id}/checks")
def get_checks(case_id: str, request: Request, all: int = Query(default=0)) -> dict:
    require_read_access(request.app.state.settings, request)
    with request.app.state.session_factory() as session:
        if session.get(Case, case_id) is None:
            raise HTTPException(status_code=404, detail="case not found")
        checks = checkstore.all_checks(session, case_id) if all else checkstore.live_checks(session, case_id)
        return {"case_id": case_id, "checks": [_check_json(c) for c in checks]}


@router.get("/v1/runs/{run_id}")
def get_run(run_id: str, request: Request) -> dict:
    require_read_access(request.app.state.settings, request)
    with request.app.state.session_factory() as session:
        run = session.get(Run, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        adapters = session.execute(
            text(
                "SELECT adapter_id, status, latency_ms, fetched_at "
                "FROM adapter_results WHERE run_id=:r ORDER BY fetched_at"
            ),
            {"r": run_id},
        ).fetchall()
        return {
            "run_id": run.id,
            "case_id": run.case_id,
            "state": run.state,
            "partial": run.partial,
            "error": run.error,
            "adapters": [
                {
                    "adapter_id": a.adapter_id,
                    "status": a.status,
                    "latency_ms": a.latency_ms,
                    "fetched_at": a.fetched_at.isoformat(),
                }
                for a in adapters
            ],
        }


@router.get("/v1/review-tasks")
def list_review_tasks(request: Request, status: str = Query(default="open")) -> dict:
    require_read_access(request.app.state.settings, request)
    with request.app.state.session_factory() as session:
        tasks = session.execute(
            select(ReviewTask).where(ReviewTask.status == status).order_by(ReviewTask.created_at)
        ).scalars()
        return {
            "tasks": [
                {
                    "id": t.id,
                    "case_id": t.case_id,
                    "task_type": t.task_type,
                    "status": t.status,
                    "context": t.context_json,
                    "created_at": t.created_at.isoformat(),
                }
                for t in tasks
            ]
        }
