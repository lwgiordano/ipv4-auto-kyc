"""Operational metrics (JSON). Prometheus exposition is a deployment TODO —
these counters are the dashboard's source: runs, decision distribution,
queue/outbox health (dead-letters alert!), review-queue depth, adapter latency.
"""

from fastapi import APIRouter, Request
from sqlalchemy import text

from kyc_tool.api import hmac_witness

router = APIRouter()


def _grouped(session, sql: str) -> dict:
    return {row[0]: row[1] for row in session.execute(text(sql))}


# Module-level constant so the EXPLAIN regression test pins the plan of the EXACT statement the
# endpoint runs — a test that EXPLAINs its own copy of the SQL proves nothing about this one.
LIVE_OUTBOX_SQL = (
    "SELECT status, count(*) FROM outbox WHERE status IN ('pending','dead') GROUP BY status"
)

# Shadow-mode automation-readiness gauge. 'approve' and 'approve_buy_locked' are the approve-class
# outcomes (domain.decision.POSITIVE_DECISIONS) — the ones that, under enforcement, would let a
# case transact without a human. Kept as literals here (the SQL needs the strings) rather than an
# import, so this counts by the SAME names the engine writes.
_AUTOMATION_READINESS_SQL = text(
    """
    WITH latest_auto AS (
        SELECT DISTINCT ON (case_id) case_id, decision
        FROM decisions WHERE manual = false
        ORDER BY case_id, decision_sequence DESC
    )
    SELECT
        count(*) FILTER (WHERE decision IN ('approve','approve_buy_locked'))
            AS cases_would_auto_approve,
        count(*) FILTER (WHERE decision NOT IN ('approve','approve_buy_locked'))
            AS cases_engine_held,
        count(*) FILTER (WHERE decision NOT IN ('approve','approve_buy_locked') AND EXISTS (
            SELECT 1 FROM decisions d
            WHERE d.case_id = latest_auto.case_id AND d.manual = true))
            AS human_approved_after_engine_held
    FROM latest_auto
    """
)


def _automation_readiness(session) -> dict:
    """Read-only shadow-mode gauge: the engine ALREADY renders these decisions — this only MEASURES
    them, it enforces nothing. It is the "measure before you enforce" surface for a staged rollout.

    - `automatic_decisions_by_type`: the mix of decisions the ENGINE renders (manual=false). A
      sudden shift in the approve share is an early fraud/regression signal.
    - `manual_approvals_total`: human `manual_approve` decisions (manual=true).
    - `cases_would_auto_approve`: cases whose LATEST automatic decision is approve-class — the
      population that would auto-approve WITHOUT a human under enforcement. This is the set to
      SAMPLE and human-review to measure the residual false-approve rate.
    - `cases_engine_held` / `human_approved_after_engine_held`: of cases the engine did NOT
      auto-approve, how many a human later approved anyway — the conservative direction (workload
      automation would save), and the only engine-vs-human disagreement this system records.

    HONEST LIMIT: the dangerous direction — the engine auto-approving a case a human would REJECT —
    is NOT computable from stored data. This system records human APPROVALS, not rejections, and
    under enforcement no human reviews an auto-approve. Measuring the residual false-approve rate
    requires the shadow PROCESS (humans review a sample of `cases_would_auto_approve` while
    enforcement stays OFF); this gauge exposes that population and the measurable agreement, and
    deliberately does not claim the false-approve rate on its own.
    """
    counts = session.execute(_AUTOMATION_READINESS_SQL).one()
    return {
        "automatic_decisions_by_type": _grouped(
            session,
            "SELECT decision, count(*) FROM decisions WHERE manual = false GROUP BY decision",
        ),
        "manual_approvals_total": session.execute(
            text("SELECT count(*) FROM decisions WHERE manual = true")
        ).scalar_one(),
        "cases_would_auto_approve": counts.cases_would_auto_approve,
        "cases_engine_held": counts.cases_engine_held,
        "human_approved_after_engine_held": counts.human_approved_after_engine_held,
    }


@router.get("/v1/metrics")
def metrics(request: Request) -> dict:
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
            # WITHOUT enforcing, to inform a staged rollout. Read-only; enforces nothing.
            "automation_readiness": _automation_readiness(session),
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
