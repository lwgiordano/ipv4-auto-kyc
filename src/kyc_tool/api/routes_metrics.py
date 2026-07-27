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
        return {
            "runs_by_state": _grouped(session, "SELECT state, count(*) FROM runs GROUP BY state"),
            "decisions_by_type": _grouped(
                session, "SELECT decision, count(*) FROM decisions GROUP BY decision"
            ),
            "jobs_by_status": _grouped(session, "SELECT status, count(*) FROM jobs GROUP BY status"),
            # PR 7b-core: decision_callback rows are never pruned, so `outbox` grows without
            # bound. Report the LIVE statuses exactly — those are what an operator acts on, and
            # they stay small — and the terminal history as one bounded count, so this endpoint's
            # cost does not grow with retained history. A GROUP BY over the whole table would.
            "outbox_by_status": _grouped(
                session,
                "SELECT status, count(*) FROM outbox "
                "WHERE status IN ('pending','dead') GROUP BY status",
            ),
            "outbox_terminal_total": session.execute(
                text("SELECT count(*) FROM outbox WHERE status IN ('delivered','superseded')")
            ).scalar_one(),
            # superseded is a governed terminal (best-effort local suppression, zero sends) — it is
            # counted in outbox_terminal_total and EXCLUDED from the pending/dead alert set below.
            "outbox_alerting": _grouped(
                session,
                "SELECT status, count(*) FROM outbox WHERE status IN ('pending','dead') GROUP BY status",
            ),
            "review_tasks_open_by_type": _grouped(
                session,
                "SELECT task_type, count(*) FROM review_tasks WHERE status='open' GROUP BY task_type",
            ),
            "adapter_latency": adapter_latency,
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
