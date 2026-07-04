"""Read endpoints + review-task completion (04 §2)."""

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy import select, text

from kyc_tool.checkstore import repo as checkstore
from kyc_tool.db.tables import Case, DecisionRow, ReviewTask, Run
from kyc_tool.events.ingest import ingest_event

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
    with request.app.state.session_factory() as session:
        case = session.get(Case, case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="case not found")
        live = checkstore.live_checks(session, case_id)
        latest = session.execute(
            select(DecisionRow)
            .where(DecisionRow.case_id == case_id)
            .order_by(DecisionRow.decided_at.desc())
            .limit(1)
        ).scalar_one_or_none()
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
    with request.app.state.session_factory() as session:
        if session.get(Case, case_id) is None:
            raise HTTPException(status_code=404, detail="case not found")
        checks = (
            checkstore.all_checks(session, case_id)
            if all
            else checkstore.live_checks(session, case_id)
        )
        return {"case_id": case_id, "checks": [_check_json(c) for c in checks]}


@router.get("/v1/runs/{run_id}")
def get_run(run_id: str, request: Request) -> dict:
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


@router.post("/v1/review-tasks/{task_id}/complete")
async def complete_review_task(task_id: str, request: Request) -> JSONResponse:
    """AUDIT:D4 — synthesizes the website.review_completed event so both
    completion paths share one idempotent, audited pipeline."""
    body = await request.json()
    result = body.get("result")
    reviewer_id = body.get("reviewer_id")
    reason_codes = body.get("reason_codes", [])
    if result not in ("pass", "fail") or not reviewer_id:
        raise HTTPException(status_code=422, detail="result (pass|fail) and reviewer_id required")

    with request.app.state.session_factory() as session:
        task = session.get(ReviewTask, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="review task not found")
        case_id = task.case_id
        task_type = task.task_type

    if task_type != "website":
        # poc_email_unavailable tasks close with audit only (AUDIT_FINDINGS §C2)
        raise HTTPException(status_code=422, detail="only website tasks complete via this endpoint")

    from datetime import UTC, datetime

    envelope = {
        "event_type": "website.review_completed",
        "occurred_at": datetime.now(UTC).isoformat(),
        "actor": {"type": "reviewer", "id": reviewer_id},
        "payload": {
            "task_id": task_id,
            "result": result,
            "reviewer_id": reviewer_id,
            "reason_codes": reason_codes,
        },
    }
    outcome = await run_in_threadpool(
        ingest_event,
        request.app.state.session_factory,
        request.app.state.policy,
        case_id=case_id,
        idempotency_key=f"review-task-complete-{task_id}",
        envelope=envelope,
    )
    return JSONResponse(status_code=outcome.status_code, content=outcome.body)
