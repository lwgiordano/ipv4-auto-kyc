"""Always-mounted, narrowly-scoped ops endpoints (re-audit `f2929f8..6a4cd87` F3).

The RUNBOOK's dead-letter recovery must exist in the SECURE production configuration — the /ui
console is optional (and off in production guidance), so its requeue buttons cannot be the only
path. These two endpoints wrap the same shared transactions the console uses, behind the operator
bearer token, and in production they fail CLOSED when no token is configured (dev/test keeps the
console's local-trust model)."""

from fastapi import APIRouter, HTTPException, Request

from kyc_tool.api.auth import require_admin
from kyc_tool.ops.requeue_service import requeue_dead_job, requeue_dead_outbox

router = APIRouter()


def _require_ops_admin(request: Request) -> None:
    settings = request.app.state.settings
    if settings.environment == "production" and not settings.ui_admin_token:
        # Fail closed BEFORE require_admin's empty-token dev allowance can apply: production boot
        # requires the token (production_config_violations), so reaching here means a bypassed boot.
        raise HTTPException(status_code=401, detail="ops admin token not configured")
    require_admin(settings, request.headers)


@router.post("/v1/ops/requeue/job/{job_id}")
def ops_requeue_job(job_id: int, request: Request) -> dict:
    _require_ops_admin(request)  # auth BEFORE any DB access
    return requeue_dead_job(request.app.state.session_factory, job_id)


@router.post("/v1/ops/requeue/outbox/{outbox_id}")
def ops_requeue_outbox(outbox_id: int, request: Request) -> dict:
    _require_ops_admin(request)
    return requeue_dead_outbox(request.app.state.session_factory, outbox_id)
