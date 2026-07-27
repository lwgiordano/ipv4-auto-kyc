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
        # Gates come through cases.latest_decision_row_id — the trigger-maintained pointer set in
        # the same transaction as every decision insert — NEVER by decided_at: now() is
        # transaction-start time, so two case-locked decides can commit in one order while their
        # decided_at values sit in the other, and an ORDER BY decided_at read here once paired
        # the current decision with the PREVIOUS decision's gates (re-audit 1f8412e F4). A NULL
        # pointer with decisions present (an ambiguous pre-014 history) returns empty gates —
        # honest ignorance over a guess — and heals on the case's next decision.
        latest = (
            session.get(DecisionRow, case.latest_decision_row_id)
            if case.latest_decision_row_id else None
        )
        return {
            "case_id": case.id,
            "status": case.status,
            "buy_status": case.buy_status,
            "broker_status": case.broker_status,
            "company_name": case.company_name,
            "jurisdiction": case.jurisdiction,
            "score": case.current_score,
            "latest_decision": case.latest_decision,
            "gates": (latest.gates_json if latest else {}),
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
