"""Operational metrics (JSON). Prometheus exposition is a deployment TODO —
these counters are the dashboard's source: runs, decision distribution,
queue/outbox health (dead-letters alert!), review-queue depth, adapter latency.
"""

import json
from dataclasses import fields

from fastapi import APIRouter, Request
from sqlalchemy import text

from kyc_tool.api import hmac_witness
from kyc_tool.api.auth import require_read_access
from kyc_tool.domain.models import BuyEnablement, Decision, Gates

router = APIRouter()


def _grouped(session, sql: str) -> dict:
    return {row[0]: row[1] for row in session.execute(text(sql))}


# Module-level constant so the EXPLAIN regression test pins the plan of the EXACT statement the
# endpoint runs — a test that EXPLAINs its own copy of the SQL proves nothing about this one.
LIVE_OUTBOX_SQL = (
    "SELECT status, count(*) FROM outbox WHERE status IN ('pending','dead') GROUP BY status"
)

# The "all five hard gates pass" JSONB predicate, built from the Gates dataclass fields so it is a
# single source of truth with the domain model: add/rename a gate and this predicate follows
# automatically. JSONB @> requires every key present AND true, so an empty or partial gates_json
# can never read as vacuously all-pass. AUDIT:A2 — five gates, not the prose's four.
_ALL_GATES_PASS_JSON = json.dumps({f.name: True for f in fields(Gates)}, sort_keys=True)

# Reconstruct the engine's COMPUTED decision from IMMUTABLE stamped facts — the fix for the shadow
# gauge's core defect (re-audit `b39b82a..b53daf4` F1). The production default is
# enforce_positive_decisions=false (M2 frozen): the pipeline HOLDS a computed positive as
# manual_review_insufficient but preserves score/gates/buy_enablement UNCHANGED
# (domain.decision.hold_positive_for_manual_review). Reading decisions.decision therefore counts the
# emitted hold, not the engine's real decision, and the "would auto-approve" population reads ~zero
# in production. A manual=false row is a computed positive iff all five gates pass; buy_enablement
# splits approve vs approve_buy_locked. A stored reject is authoritative — broker BLOCKED
# short-circuits decide() before the gate test and evaluate_gates sets broker_ok=false for a blocked
# broker, so a real reject is never all-pass; the `decision <> :d_reject` guard also refuses to
# reinterpret a pathological all-pass reject row. This deliberately does NOT read audit_log (which
# retention deletes); it uses only the immutable decision row. Label strings are parameterised from
# the domain enums so they cannot drift from what the engine writes.
_COMPUTED_DECISION_CASE = (
    "CASE WHEN decision <> :d_reject AND gates_json @> CAST(:all_pass AS jsonb) "
    "          AND buy_enablement = :buy_enabled THEN :d_approve "
    "     WHEN decision <> :d_reject AND gates_json @> CAST(:all_pass AS jsonb) THEN :d_buy_locked "
    "     ELSE decision END"
)
_COMPUTED_PARAMS = {
    "all_pass": _ALL_GATES_PASS_JSON,
    "buy_enabled": BuyEnablement.ENABLED.value,
    "d_approve": Decision.APPROVE.value,
    "d_buy_locked": Decision.APPROVE_BUY_LOCKED.value,
    "d_reject": Decision.REJECT.value,
}

_AUTOMATION_READINESS_SQL = text(
    f"""
    WITH latest_auto AS (
        SELECT DISTINCT ON (case_id) case_id, ({_COMPUTED_DECISION_CASE}) AS computed_decision
        FROM decisions WHERE manual = false
        ORDER BY case_id, decision_sequence DESC
    )
    SELECT
        count(*) FILTER (WHERE computed_decision IN (:d_approve, :d_buy_locked))
            AS cases_would_auto_approve,
        count(*) FILTER (WHERE computed_decision NOT IN (:d_approve, :d_buy_locked))
            AS cases_engine_held,
        count(*) FILTER (WHERE computed_decision NOT IN (:d_approve, :d_buy_locked) AND EXISTS (
            SELECT 1 FROM decisions m
            WHERE m.case_id = latest_auto.case_id AND m.manual = true))
            AS cases_engine_nonpositive_with_manual_history
    FROM latest_auto
    """
)

# The engine's COMPUTED decision mix (same reconstruction), not the enforcement-held labels.
_AUTOMATIC_DECISIONS_BY_TYPE_SQL = text(
    f"SELECT ({_COMPUTED_DECISION_CASE}) AS computed_decision, count(*) "
    f"FROM decisions WHERE manual = false GROUP BY computed_decision"
)


def _automation_readiness(session) -> dict:
    """Read-only, NON-GATING shadow-mode gauge: the engine ALREADY renders these decisions — this
    only MEASURES them, it enforces nothing, and it CANNOT by itself authorise enforcement (M2). It
    is the "measure before you enforce" surface for a staged rollout. Every count below is the
    engine's COMPUTED decision, reconstructed from immutable stamped facts (see
    `_COMPUTED_DECISION_CASE`); with the enforcement kill switch off (the production default) the
    stored label is a hold, so counting the stored label would measure the overlay, not the engine.

    - `automatic_decisions_by_type`: the mix of decisions the engine COMPUTED (manual=false). A
      sudden shift in the approve share is an early fraud/regression signal.
    - `manual_approvals_total`: human `manual_approve` decisions (manual=true).
    - `cases_would_auto_approve`: cases whose LATEST computed automatic decision is approve-class —
      the population that would auto-approve WITHOUT a human under enforcement. This is the set to
      SAMPLE and human-review to measure the residual false-approve rate.
    - `cases_engine_held`: cases whose latest computed automatic decision is NOT approve-class
      (genuine insufficient-evidence holds AND automatic rejects) — the conservative direction.
    - `cases_engine_nonpositive_with_manual_history`: of those non-positive cases, how many carry
      ANY manual approval anywhere in their history. This is a WEAK, NON-GATING signal, deliberately
      named for exactly what it measures: it has no "after" ordering and is NOT bound to the reviewed
      decision/run, so it is NOT a proven engine-vs-human disagreement. A real reviewed-outcome
      binding (approve AND reject) is future review/activation-unit work.

    HONEST LIMIT: the dangerous direction — the engine auto-approving a case a human would REJECT —
    is NOT computable from stored data. This system records human APPROVALS, not rejections, and
    under enforcement no human reviews an auto-approve. Measuring the residual false-approve rate
    requires the shadow PROCESS (humans review a sample of `cases_would_auto_approve` while
    enforcement stays OFF); this gauge exposes that population and deliberately does not claim the
    false-approve rate on its own. It also mixes engine builds/policy eras and carries no window or
    denominator (versioned-contract hardening is deferred to the rollout observation unit), so it is
    an operator signal, not a rollout decision record.
    """
    counts = session.execute(_AUTOMATION_READINESS_SQL, _COMPUTED_PARAMS).one()
    return {
        "automatic_decisions_by_type": {
            row[0]: row[1]
            for row in session.execute(_AUTOMATIC_DECISIONS_BY_TYPE_SQL, _COMPUTED_PARAMS)
        },
        "manual_approvals_total": session.execute(
            text("SELECT count(*) FROM decisions WHERE manual = true")
        ).scalar_one(),
        "cases_would_auto_approve": counts.cases_would_auto_approve,
        "cases_engine_held": counts.cases_engine_held,
        "cases_engine_nonpositive_with_manual_history": (
            counts.cases_engine_nonpositive_with_manual_history
        ),
    }


@router.get("/v1/metrics")
def metrics(request: Request) -> dict:
    """Auth-gated HTTP metrics surface (re-audit `b39b82a..b53daf4` F4). Read auth
    (`require_read_access` — off in dev, forced on in production) is checked BEFORE any DB access, so
    an unauthenticated caller cannot even open a session against this business-sensitive endpoint.
    This route (and ONLY this route) carries the automation-readiness block — it is the authenticated
    surface for rollout observers."""
    require_read_access(request.app.state.settings, request)
    return collect_metrics(request, include_readiness=True)


def collect_metrics(request: Request, *, include_readiness: bool = False) -> dict:
    """Build the metrics payload. INTERNAL — performs NO auth of its own.

    HONEST TRUST NOTE (re-audit `d3c0852..23e005e` F1): the `/ui/api/overview` route reuses this and
    is CURRENTLY UNAUTHENTICATED (the UI read-GET auth sweep is PR 1.1). So the automation-readiness
    block — sensitive rollout intelligence, and an expensive query (re-audit F5) — is NOT built here
    by default; only the read-auth-gated `/v1/metrics` route requests it (`include_readiness=True`).
    That keeps the sensitive/expensive gauge off the unauthenticated, 5-second-polled UI path until
    PR 1.1 gates every UI read GET. The remaining operational counters below are the same ones the
    console has always shown."""
    with request.app.state.session_factory() as session:
        adapter_latency = [
            {
                "adapter_id": row.adapter_id,
                "calls": row.calls,
                "error_rate": float(row.error_rate or 0),
                "avg_ms": float(row.avg_ms or 0),
                "p95_ms": float(row.p95_ms or 0),
            }
            for row in session.execute(
                text(
                    """
                    SELECT adapter_id,
                           count(*) AS calls,
                           avg((status = 'upstream_error')::int) AS error_rate,
                           avg(latency_ms) AS avg_ms,
                           percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_ms
                    FROM adapter_results GROUP BY adapter_id ORDER BY adapter_id
                    """
                )
            )
        ]
        decision_latency = session.execute(
            text(
                """
                SELECT avg(EXTRACT(EPOCH FROM (d.decided_at - e.received_at))) AS avg_s,
                       percentile_cont(0.95) WITHIN GROUP
                           (ORDER BY EXTRACT(EPOCH FROM (d.decided_at - e.received_at))) AS p95_s
                FROM decisions d
                JOIN runs r ON r.id = d.run_id
                JOIN events e ON e.id = r.triggering_event_id
                """
            )
        ).one()
        # PR 7b-core: decision_callback rows are never pruned — retention redacts the body past
        # the window but keeps the row itself, because the row is the durable ordering authority
        # a later unit (7b-activation) reconciles the platform against. So `outbox` grows without
        # bound for the life of the system.
        #
        # Live statuses (pending, dead) stay EXACT: they are what an operator acts on, and they
        # stay small. The predicate is served by migration 014's ix_outbox_live_status — 013's
        # claim indexes are partial to status='pending' ALONE, and Postgres cannot use a
        # pending-only partial index for an IN ('pending','dead') query, so without 014's index
        # this exact count would seq-scan the ever-growing terminal history (re-audit 1f8412e
        # F7). The EXPLAIN regression test pins the plan, not just the values.
        # `outbox_by_status` and `outbox_alerting` used to each run this identical query — two
        # round-trips for one result — so it is computed once here and shared; both keys stay
        # published (each is part of the metrics surface).
        #
        # Terminal history (delivered, superseded) is NOT exactly counted. It has exactly one
        # consumer in this repo (a test asserting the key is present) and nothing alerts on it or
        # consumes it in the reconciliation design — it is a growth gauge, not a control signal.
        # An exact counter would need a trigger-maintained second source of truth that every
        # status transition anywhere in the codebase must keep honest forever, to serve a number
        # nobody acts on. So it is instead *estimated* in bounded time from the planner's own
        # statistics — pg_class.reltuples for the whole relation, minus the exact live count above
        # — rather than scanned. reltuples is -1 on a relation that has never been ANALYZEd (or
        # since its last TRUNCATE), and is in general only an estimate that can legitimately sit
        # below the true live count, so both the raw statistic and the final subtraction are
        # floored at 0 — the estimate can never be reported as negative. This endpoint's cost is
        # therefore independent of how much terminal history retention has accumulated.
        outbox_live_by_status = _grouped(session, LIVE_OUTBOX_SQL)
        # Addressed by OID via to_regclass, not by `relname = 'outbox'`: relname ignores schema, so
        # a same-named table in any other schema on the search path could supply the statistic for
        # a relation this session never queries. to_regclass resolves the SAME name the ORM does,
        # and yields NULL (no row, estimate 0) rather than raising if the table is absent.
        outbox_reltuples = session.execute(
            text("SELECT reltuples::bigint FROM pg_class WHERE oid = to_regclass(:relname)"),
            {"relname": "outbox"},
        ).scalar()
        outbox_total_estimate = max(outbox_reltuples, 0) if outbox_reltuples is not None else 0
        outbox_terminal_total_estimate = max(
            outbox_total_estimate - sum(outbox_live_by_status.values()), 0
        )
        return {
            "runs_by_state": _grouped(session, "SELECT state, count(*) FROM runs GROUP BY state"),
            "decisions_by_type": _grouped(
                session, "SELECT decision, count(*) FROM decisions GROUP BY decision"
            ),
            "jobs_by_status": _grouped(session, "SELECT status, count(*) FROM jobs GROUP BY status"),
            # pre-014 histories whose decision write-order has no durable record serve NO
            # decision tuple (decision_provenance=unresolved_legacy_order) — this counts them so
            # the condition is observable instead of silently indistinguishable from empty gates.
            "cases_with_unresolved_decision_order": session.execute(
                text("SELECT count(*) FROM cases c WHERE c.latest_decision_row_id IS NULL "
                     "AND EXISTS (SELECT 1 FROM decisions d WHERE d.case_id = c.id)")
            ).scalar_one(),
            "outbox_by_status": outbox_live_by_status,
            "outbox_terminal_total_estimate": outbox_terminal_total_estimate,
            # superseded is a governed terminal (best-effort local suppression, zero sends) — it is
            # counted in outbox_terminal_total_estimate and EXCLUDED from the alert set below.
            "outbox_alerting": outbox_live_by_status,
            "review_tasks_open_by_type": _grouped(
                session,
                "SELECT task_type, count(*) FROM review_tasks WHERE status='open' GROUP BY task_type",
            ),
            "adapter_latency": adapter_latency,
            # Shadow-mode automation-readiness: measure the engine's decisions vs. human approvals
            # WITHOUT enforcing, to inform a staged rollout. Read-only; enforces nothing. Included
            # ONLY for the read-auth-gated /v1/metrics route (include_readiness), never on the
            # currently-unauthenticated /ui overview path (re-audit F1) — and it is the expensive
            # query, so keeping it off the 5s-polled UI path also addresses F5.
            **({"automation_readiness": _automation_readiness(session)} if include_readiness else {}),
            "event_to_decision_seconds": {
                "avg": float(decision_latency.avg_s or 0),
                "p95": float(decision_latency.p95_s or 0),
            },
            # HMAC dual-accept witness (PR 5a §6) — DB-backed so it aggregates
            # across replicas; the v1_accepted witness gates the inbound sunset.
            "hmac": {
                "v1_accepted": session.execute(
                    text("SELECT accepted_count FROM hmac_v1_observation WHERE id = 1")
                ).scalar()
                or 0,
                **{
                    row[0]: row[1]
                    for row in session.execute(text("SELECT key, count FROM hmac_signature_stats"))
                },
                "observation": hmac_witness.observation_state(session),
            },
        }
