"""Ops console routes — the JSON the console consumes plus the page itself.

Debug tooling, deliberately in the same trust domain as the existing read API
(which is unauthenticated). The composer and requeue endpoints MUTATE, so the
whole router is gated by KYC_UI_ENABLED — set it false in production or front
the port with network controls (see runbook).
"""

import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError
from sqlalchemy import text

from kyc_tool.api.auth import require_admin
from kyc_tool.api.routes_metrics import metrics as collect_metrics
from kyc_tool.api.schemas import PAYLOAD_MODELS, EventEnvelope
from kyc_tool.db.audit import audit
from kyc_tool.db.session import uow
from kyc_tool.events.ingest import ingest_event
from kyc_tool.ui import integrations as integrations_report
from kyc_tool.ui.salesforce_projection import FIELD_SOURCES, project_salesforce_fields

router = APIRouter()

_CONSOLE_HTML = (Path(__file__).parent / "console.html").read_text()

# Composer payload templates — starting points, not constraints.
EVENT_TEMPLATES = {
    "kyb.run_requested": {
        "company_legal_name": "Acme Networks Ltd",
        "address": "1 Main Street, London, EC1A 1AA",
        "registration_number": "12345678",
        "jurisdiction": "GB",
        "website": "https://acme.example",
        "contact": {"name": "Jane Doe", "title": "Director"},
    },
    "email.verified": {
        "email": "ops@acme.example",
        "domain": "acme.example",
        "verified_at": "2026-01-01T00:00:00Z",
    },
    "org_id.submitted": {"rir": "arin", "org_handle": "ORG-ACME-1"},
    "poc.submitted": {"rir": "arin", "poc_handle": "JD123-ARIN", "org_handle": "ORG-ACME-1"},
    "poc.token_verified": {
        "token_id": "tok-1",
        "verified_at": "2026-01-01T00:00:00Z",
        "token": "<paste token from the POC email>",
    },
    "document.uploaded": {"object_ref": "fs://uploads/doc.json", "doc_type": "registration_certificate"},
    "website.review_completed": {
        "task_id": "<open task id>",
        "result": "pass",
        "reviewer_id": "console",
        "reason_codes": [],
    },
    "reviewer.manual_approve": {"reviewer_id": "console", "note": "manual approval via ops console"},
    "recalculate.requested": {},
}


def _rows(result) -> list[dict]:
    return [dict(row._mapping) for row in result]


def _json_safe(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


@router.get("/ui", include_in_schema=False)
def console() -> HTMLResponse:
    return HTMLResponse(_CONSOLE_HTML)


@router.get("/ui/api/overview")
def overview(request: Request) -> dict:
    policy = request.app.state.policy
    settings = request.app.state.settings
    body = {
        "metrics": collect_metrics(request),
        "policy": {
            "bundle_hash": policy.bundle_hash,
            "shas": policy.shas,
            "rubric_version": policy.rubric.version,
            "threshold": policy.rubric.threshold,
        },
        "config": {
            "auth_disabled": settings.auth_disabled,
            "hmac_secret_set": bool(settings.platform_hmac_secret),
            "callback_url": settings.platform_callback_url,
            "object_store": settings.object_store,
            "policy_dir": str(settings.policy_dir),
        },
    }
    with request.app.state.session_factory() as session:
        body["dead_jobs"] = _rows(
            session.execute(
                text(
                    "SELECT id, kind, case_id, attempts, last_error FROM jobs "
                    "WHERE status='dead' ORDER BY id DESC LIMIT 10"
                )
            )
        )
        body["dead_outbox"] = _rows(
            session.execute(
                text(
                    "SELECT id, kind, case_id, run_id, attempts, last_error FROM outbox "
                    "WHERE status='dead' ORDER BY id DESC LIMIT 10"
                )
            )
        )
    return _json_safe(body)


@router.get("/ui/api/cases")
def list_cases(request: Request, q: str = Query(default=""), limit: int = Query(default=50)) -> dict:
    # The listed decision is the POINTED row's value (or NULL when unresolved/none) — the
    # cases.latest_decision projection column goes stale after a record-only manual approval
    # and must not be served as the verdict (re-audit 0c46443 F6).
    #
    # The pointed row's own `score` ships beside it as `decision_score`, and the live recomputed
    # `current_evidence_score` is a DISTINCT field (re-audit `cbb783b` F6): serving one Score
    # column that mixed a historical verdict with a later evidence total let the list render
    # "Approve / 40" for a case decided at 105. Two names, two meanings, never blended.
    sql = """
        SELECT c.id, c.company_name, c.jurisdiction, c.status, c.buy_status, c.broker_status,
               c.current_score AS current_evidence_score,
               d.decision AS latest_decision, d.score AS decision_score, c.updated_at
        FROM cases c
        LEFT JOIN decisions d ON d.id = c.latest_decision_row_id AND d.case_id = c.id
        {where}
        ORDER BY c.updated_at DESC LIMIT :limit
    """
    params: dict = {"limit": min(limit, 200)}
    where = ""
    if q:
        where = "WHERE c.id ILIKE :q OR c.company_name ILIKE :q"
        params["q"] = f"%{q}%"
    with request.app.state.session_factory() as session:
        cases = _rows(session.execute(text(sql.format(where=where)), params))
    return _json_safe({"cases": cases})


@router.get("/ui/api/cases/{case_id}/full")
def case_full(case_id: str, request: Request) -> dict:
    policy = request.app.state.policy
    with request.app.state.session_factory() as session:
        case = session.execute(
            text("SELECT * FROM cases WHERE id=:id"), {"id": case_id}
        ).mappings().first()
        if case is None:
            raise HTTPException(status_code=404, detail="case not found")
        case = dict(case)

        checks = _rows(
            session.execute(
                text("SELECT * FROM checks WHERE case_id=:id ORDER BY created_at, id"),
                {"id": case_id},
            )
        )
        runs = _rows(
            session.execute(
                text(
                    "SELECT id, state, partial, error, started_at, finished_at, "
                    "triggering_event_id, policy_bundle_hash FROM runs "
                    "WHERE case_id=:id ORDER BY started_at DESC LIMIT 25"
                ),
                {"id": case_id},
            )
        )
        adapter_results = _rows(
            session.execute(
                text(
                    "SELECT run_id, adapter_id, status, latency_ms, raw_ref, fetched_at "
                    "FROM adapter_results WHERE run_id = ANY(:run_ids) ORDER BY fetched_at"
                ),
                {"run_ids": [r["id"] for r in runs] or ["-"]},
            )
        )
        events = _rows(
            session.execute(
                text(
                    "SELECT id, event_type, received_at, processed_at, run_id "
                    "FROM events WHERE case_id=:id ORDER BY received_at DESC LIMIT 25"
                ),
                {"id": case_id},
            )
        )
        # DISPLAY order only — decided_at is transaction-start time and can invert against the
        # lock-serialized commit order, so this list must never select the authoritative row.
        decisions = _rows(
            session.execute(
                text(
                    "SELECT id, run_id, decision, score, gates_json, buy_enablement, manual, "
                    "reviewer_id, decided_at, published_at FROM decisions "
                    "WHERE case_id=:id ORDER BY decided_at DESC LIMIT 25"
                ),
                {"id": case_id},
            )
        )
        # The AUTHORITATIVE latest decision for the Salesforce projection: the trigger-maintained
        # pointer (migration 014), fetched separately — feeding decisions[0] here preserved the
        # exact inversion the pointer was introduced to eliminate (re-audit 4dfdf8a F4). A NULL
        # pointer (ambiguous pre-014 history) projects None, never a decided_at guess.
        pointer_decision = None
        if case.get("latest_decision_row_id"):
            row = session.execute(
                text(
                    "SELECT id, run_id, decision, score, gates_json, buy_enablement, manual, "
                    "reviewer_id, decided_at, published_at FROM decisions WHERE id=:d "
                    "AND case_id=:id"
                ),
                {"d": case["latest_decision_row_id"], "id": case_id},
            ).mappings().first()
            pointer_decision = dict(row) if row else None
        # Manual attribution is sticky (re-audit 15d875d F6): the verdict pointer moves to later
        # automatic decisions, but Manual_Approved_By/At must keep naming the manual act. It is
        # read from its OWN trigger-maintained pointer, never by sorting `decisions` — `id` is a
        # random UUID hex, so `ORDER BY id DESC` returned the lexically largest manual row rather
        # than the latest one (re-audit `cbb783b` F6), and `decided_at` inverts against commit
        # order. NULL here is honestly unresolved: either no manual approval, or a legacy history
        # with several that cannot be ordered.
        latest_manual_decision = None
        manual_decision_provenance = "no_manual_decisions"
        manual_pointer = case.get("latest_manual_decision_row_id")
        if manual_pointer:
            manual_row = session.execute(
                text(
                    "SELECT id, run_id, decision, score, manual, reviewer_id, decided_at "
                    "FROM decisions WHERE id=:d AND case_id=:id AND manual IS TRUE"
                ),
                {"d": manual_pointer, "id": case_id},
            ).mappings().first()
            latest_manual_decision = dict(manual_row) if manual_row else None
            # A pointer the hardened predicate rejects (other case, or not manual) is drift, and
            # the one thing it is NOT is proof that no manual approval happened. 022 forbids that
            # state at the database and validates the existing data, so reaching here means a row
            # written around the guard — report it as unresolved rather than silently answering
            # "no manual approval" for a case whose own pointer disagrees.
            manual_decision_provenance = (
                "latest_manual_row" if latest_manual_decision else "unresolved_pointer_drift"
            )
        if latest_manual_decision is None and not manual_pointer:
            manual_rows = session.execute(
                text("SELECT count(*) FROM decisions WHERE case_id=:id AND manual IS TRUE"),
                {"id": case_id},
            ).scalar_one()
            if manual_rows:
                manual_decision_provenance = "unresolved_legacy_order"
        tasks = _rows(
            session.execute(
                text(
                    "SELECT id, task_type, status, result, reviewer_id, reason_codes, "
                    "context_json, created_at, completed_at FROM review_tasks "
                    "WHERE case_id=:id ORDER BY created_at DESC"
                ),
                {"id": case_id},
            )
        )
        tokens = _rows(
            session.execute(
                text(
                    "SELECT id, poc_handle, rir_listed_email, sent_at, verified_at, expired_at "
                    "FROM poc_tokens WHERE case_id=:id ORDER BY sent_at DESC"
                ),
                {"id": case_id},
            )
        )
        audit = _rows(
            session.execute(
                text(
                    "SELECT id, at, actor, action, detail_json FROM audit_log "
                    "WHERE case_id=:id ORDER BY id DESC LIMIT 200"
                ),
                {"id": case_id},
            )
        )
        outbox = _rows(
            session.execute(
                text(
                    "SELECT id, kind, run_id, status, attempts, next_attempt_at, delivered_at, "
                    "last_error FROM outbox WHERE case_id=:id ORDER BY id DESC LIMIT 25"
                ),
                {"id": case_id},
            )
        )

    live = [c for c in checks if not c["superseded_by_check_id"]]
    live_by_type = {c["check_type"]: c for c in live}
    score_items = [
        {
            "check_type": item.check_type,
            "points": item.points,
            "category": item.category,
            "status": live_by_type.get(item.check_type, {}).get("status", "missing"),
            "awarded": live_by_type.get(item.check_type, {}).get("points_awarded", 0)
            if live_by_type.get(item.check_type, {}).get("status") == "pass"
            else 0,
        }
        for item in policy.rubric.items
    ]

    now = datetime.now(UTC)
    token_outstanding = any(
        t["verified_at"] is None and (t["expired_at"] is None or t["expired_at"] > now)
        for t in tokens
    )
    salesforce = project_salesforce_fields(
        case=case,
        checks=checks,
        open_task_types=[t["task_type"] for t in tasks if t["status"] == "open"],
        poc_token_outstanding=token_outstanding,
        latest_decision=pointer_decision,
        latest_manual_decision=latest_manual_decision,
    )

    return _json_safe(
        {
            "case": case,
            "checks": checks,
            # LIVE evidence, recomputed from today's checks — NOT the published decision. Named
            # so the consumer cannot mistake it for one (re-audit `cbb783b` F6); the decided
            # value travels with the pointed row below, as `pointer_decision.score`.
            "score": {
                "current_evidence_score": case["current_score"],
                "total": case["current_score"],  # retained: the rubric panel's own bar total
                "threshold": policy.rubric.threshold,
                "items": score_items,
                "is_published_decision": False,
            },
            "runs": runs,
            "adapter_results": adapter_results,
            "events": events,
            "decisions": decisions,
            # the authoritative row the Salesforce projection was built from (may be None for an
            # ambiguous pre-014 history) — surfaced so consumers can SEE which row is authority.
            # decision, score, gates and buy_enablement all come from THIS one row or from none.
            "pointer_decision": pointer_decision,
            # the sticky manual act (own pointer; None = no manual approval OR an unorderable
            # legacy multi-manual history — the projection treats both as unresolved)
            "latest_manual_decision": latest_manual_decision,
            "manual_decision_provenance": manual_decision_provenance,
            "review_tasks": tasks,
            "poc_tokens": tokens,
            "audit": list(reversed(audit)),
            "outbox": outbox,
            "salesforce": salesforce,
            "field_sources": FIELD_SOURCES,
        }
    )


@router.get("/ui/api/policy")
def policy_view(request: Request) -> dict:
    policy = request.app.state.policy
    with request.app.state.session_factory() as session:
        brokers = _rows(
            session.execute(
                text(
                    "SELECT name, policy, aliases, domains, email_domains, org_ids, "
                    "poc_handles, asns FROM broker_entities ORDER BY policy, name"
                )
            )
        )
    return _json_safe(
        {
            "bundle_hash": policy.bundle_hash,
            "threshold": policy.rubric.threshold,
            "rubric": [item.model_dump() for item in policy.rubric.items],
            "hard_gates": policy.rubric.hard_gates,
            "decisions": [rule.model_dump() for rule in policy.decision_policy.decisions],
            "events": list(policy.events.event_types),
            "adapters": [entry.model_dump() for entry in policy.adapter_catalog.root],
            "broker_entities": brokers,
            "field_sources": FIELD_SOURCES,
        }
    )


def _adapter_registry(request: Request) -> dict:
    """Build the worker adapter registry once per process and cache it. The
    integration report only introspects adapter types; rebuilding per request
    leaks an httpx client pool each time."""
    cached = getattr(request.app.state, "_adapter_registry", None)
    if cached is None:
        from kyc_tool.storage.object_store import make_object_store
        from kyc_tool.workers.pipeline_worker import build_adapters

        settings = request.app.state.settings
        store = make_object_store(
            settings.object_store, fs_root=settings.object_store_root, s3_bucket=settings.s3_bucket
        )
        cached = build_adapters(settings, store)
        request.app.state._adapter_registry = cached
    return cached


@router.get("/ui/api/integrations")
def integrations(request: Request) -> dict:
    adapters = _adapter_registry(request)
    with request.app.state.session_factory() as session:
        return _json_safe(
            integrations_report.integration_report(
                request.app.state.settings, session, adapters
            )
        )


@router.post("/ui/api/integrations/{adapter_id}/probe")
async def integration_probe(adapter_id: str, request: Request) -> dict:
    require_admin(request.app.state.settings, request.headers)
    return await run_in_threadpool(integrations_report.probe, adapter_id)


@router.get("/ui/api/event-templates")
def event_templates() -> dict:
    return {"templates": EVENT_TEMPLATES, "event_types": sorted(PAYLOAD_MODELS)}


@router.post("/ui/api/send-event")
async def send_event(request: Request) -> JSONResponse:
    """Composer: server-side ingestion (same trust domain as holding the HMAC
    secret — dev/debug tool; see module docstring)."""
    require_admin(request.app.state.settings, request.headers)
    body = await request.json()
    case_id = (body.get("case_id") or "").strip()
    if not case_id:
        raise HTTPException(status_code=422, detail="case_id required")
    idempotency_key = body.get("idempotency_key") or f"console-{uuid.uuid4().hex}"

    event_type = body.get("event_type")
    raw_payload = body.get("payload") or {}

    _SENSITIVE = {"website.review_completed", "reviewer.manual_approve"}
    settings = request.app.state.settings
    if settings.environment == "production" and event_type in _SENSITIVE:
        raise HTTPException(
            status_code=403, detail="composer cannot submit reviewer events in production"
        )

    if event_type in _SENSITIVE:
        rid = raw_payload.get("reviewer_id") or "ops-console"
        actor = {"type": "reviewer", "id": rid}
    else:
        actor = {"type": "system", "id": "ops-console"}

    try:
        envelope = EventEnvelope.model_validate(
            {
                "event_type": event_type,
                "occurred_at": datetime.now(UTC).isoformat(),
                "actor": actor,
                "payload": raw_payload,
            }
        )
        payload = envelope.validated_payload()
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_url=False)) from exc

    outcome = await run_in_threadpool(
        ingest_event,
        request.app.state.session_factory,
        request.app.state.policy,
        case_id=case_id,
        idempotency_key=idempotency_key,
        envelope={
            "event_type": envelope.event_type,
            "occurred_at": envelope.occurred_at.isoformat(),
            "actor": envelope.actor.model_dump(mode="json"),
            "payload": payload,
        },
    )
    with request.app.state.session_factory() as session:
        queued = session.execute(
            text("SELECT count(*) FROM jobs WHERE status='queued'")
        ).scalar_one()
    return JSONResponse(
        status_code=outcome.status_code,
        content={**outcome.body, "idempotency_key": idempotency_key, "jobs_queued": queued},
    )


@router.post("/ui/api/requeue/job/{job_id}")
def requeue_job(job_id: int, request: Request) -> dict:
    """Runbook §dead-letter as a button: requeue the job and reset its FAILED
    run to QUEUED — transitions are guarded, adapter fetches resume."""
    require_admin(request.app.state.settings, request.headers)
    with uow(request.app.state.session_factory) as session:
        row = session.execute(
            text("SELECT payload_json, status, case_id FROM jobs WHERE id=:id"), {"id": job_id}
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="job not found")
        if row.status != "dead":
            raise HTTPException(status_code=409, detail=f"job is {row.status}, not dead")
        session.execute(
            text(
                "UPDATE jobs SET status='queued', attempts=0, run_after=now(), locked_by=NULL, "
                "lease_expires_at=NULL, last_error=NULL, updated_at=now() WHERE id=:id"
            ),
            {"id": job_id},
        )
        run_id = (row.payload_json or {}).get("run_id")
        if run_id:
            session.execute(
                text(
                    "UPDATE runs SET state='QUEUED', error=NULL, finished_at=NULL "
                    "WHERE id=:run_id AND state='FAILED'"
                ),
                {"run_id": run_id},
            )
        audit(session, "job.requeued", case_id=row.case_id, run_id=run_id, job_id=job_id)
    return {"requeued": job_id, "run_reset": run_id}


@router.post("/ui/api/requeue/outbox/{outbox_id}")
def requeue_outbox(outbox_id: int, request: Request) -> dict:
    require_admin(request.app.state.settings, request.headers)
    with uow(request.app.state.session_factory) as session:
        row = session.execute(
            text("SELECT kind, status, case_id, run_id, payload_json FROM outbox WHERE id=:id"),
            {"id": outbox_id},
        ).first()
        if row is None or row.status != "dead":
            raise HTTPException(status_code=409, detail="outbox row not found or not dead")
        if (row.payload_json or {}).get("redacted"):
            # The body was scrubbed — by delivery for a POC token, or by retention past the
            # window — so there is nothing deliverable left, for EITHER kind. Requeueing would
            # produce a pending row that fails on every claim and blocks its stream (migration
            # 020 refuses the transition at the database too; this is the honest 409 above it).
            remedy = ("re-submit the POC instead" if row.kind == "poc_email"
                      else "re-emit the decision (recalculate.requested) instead")
            raise HTTPException(
                status_code=409,
                detail=f"dead {row.kind} is redacted (body scrubbed); {remedy}",
            )
        # The redaction predicate rides IN the UPDATE, not only in the check above: retention can
        # commit between that SELECT and this statement, and losing that race used to surface as
        # a 500 from the database guard rather than the honest 409 (the write is refused either
        # way — this makes the refusal legible).
        applied = session.execute(
            text(
                "UPDATE outbox SET status='pending', attempts=0, next_attempt_at=now(), "
                "last_error=NULL WHERE id=:id AND status='dead' "
                "AND payload_json <> '{\"redacted\": true}'::jsonb"
            ),
            {"id": outbox_id},
        ).rowcount
        if not applied:
            raise HTTPException(
                status_code=409,
                detail="outbox row changed while requeueing (redacted or no longer dead); retry",
            )
        audit(session, "outbox.requeued", case_id=row.case_id, run_id=row.run_id, outbox_id=outbox_id)
    return {"requeued": outbox_id}
