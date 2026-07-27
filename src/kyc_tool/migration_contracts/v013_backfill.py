"""FROZEN migration contract for the 7b-core 012→013 backfill (historical & immutable).

Migration 013's preflight AND the verify_pr7b_core_backfill diagnostic both run THIS matrix,
so the two can never diverge. This module is deliberately dependency-light — it imports ONLY
`sqlalchemy.text` — and is pinned by a frozen-SHA test (tests/unit/test_migration_contract_v013.py)
that must NEVER be re-pinned: changing this file changes how a historical 012→013 upgrade behaves.
For any later backfill semantics, add a NEW `v014_*.py` contract instead of editing this one.

Raw SQL only — references NO 013-only columns (ordering_stream, decision_sequence, claim_*). Each
check SELECTs offending id pairs (a, b); empty ⇒ clean. MISSING_CALLBACK is the fail-closed
no-authoritative-mapping state (a decision whose callback was pruned): the safe order cannot be
reconstructed, so it maps to BLOCKED_NO_AUTHORITATIVE_MAPPING.
"""

from sqlalchemy import text

MISSING_CALLBACK = "missing_callback"
BLOCKED_SENTINEL = "BLOCKED_NO_AUTHORITATIVE_MAPPING"

PARITY_CHECKS: list[tuple[str, str]] = [
    (MISSING_CALLBACK,
     "SELECT d.id AS a, d.run_id AS b FROM decisions d WHERE d.manual=false AND NOT EXISTS "
     "(SELECT 1 FROM outbox o WHERE o.kind='decision_callback' AND o.run_id=d.run_id)"),
    ("orphan_callback",
     "SELECT o.id AS a, o.run_id AS b FROM outbox o WHERE o.kind='decision_callback' AND NOT EXISTS "
     "(SELECT 1 FROM decisions d WHERE d.manual=false AND d.run_id=o.run_id)"),
    ("null_run_callback",
     "SELECT o.id AS a, NULL AS b FROM outbox o WHERE o.kind='decision_callback' AND o.run_id IS NULL"),
    ("duplicate_callback_per_run",
     "SELECT run_id AS a, count(*) AS b FROM outbox WHERE kind='decision_callback' "
     "GROUP BY run_id HAVING count(*)>1"),
    ("duplicate_auto_decision_per_run",
     "SELECT run_id AS a, count(*) AS b FROM decisions WHERE manual=false AND run_id IS NOT NULL "
     "GROUP BY run_id HAVING count(*)>1"),
    ("null_or_orphan_outbox_case",
     "SELECT o.id AS a, o.case_id AS b FROM outbox o WHERE o.case_id IS NULL "
     "OR o.case_id NOT IN (SELECT id FROM cases)"),
    ("callback_case_ne_decision_case",
     "SELECT o.id AS a, o.run_id AS b FROM outbox o JOIN decisions d ON d.run_id=o.run_id AND d.manual=false "
     "WHERE o.kind='decision_callback' AND o.case_id <> d.case_id"),
    ("decision_case_ne_run_case",
     "SELECT d.id AS a, d.run_id AS b FROM decisions d JOIN runs r ON r.id=d.run_id "
     "WHERE d.case_id <> r.case_id"),
    ("unknown_kind",
     "SELECT id AS a, kind AS b FROM outbox WHERE kind NOT IN ('decision_callback','poc_email')"),
    ("poc_row_with_run",
     "SELECT id AS a, run_id AS b FROM outbox WHERE kind='poc_email' AND run_id IS NOT NULL"),
    ("manual_decision_with_run",
     "SELECT id AS a, run_id AS b FROM decisions WHERE manual=true AND run_id IS NOT NULL"),
    ("auto_decision_null_run",
     "SELECT id AS a, NULL AS b FROM decisions WHERE manual=false AND run_id IS NULL"),
    # Pre-013 projection of the FINAL ck_outbox_status_lifecycle (schema 012 has no resolved_at /
    # claim_* columns, so a legacy row can only violate via status vocabulary or the delivered_at
    # shape). A valid pending/dead has delivered_at NULL; a valid delivered has it NOT NULL;
    # 'superseded' and any unknown status are impossible pre-013 and fail the 013 CHECK.
    ("invalid_legacy_outbox_lifecycle",
     "SELECT id AS a, status AS b FROM outbox WHERE "
     "status NOT IN ('pending','delivered','dead') "
     "OR (status='pending' AND delivered_at IS NOT NULL) "
     "OR (status='delivered' AND delivered_at IS NULL) "
     "OR (status='dead' AND delivered_at IS NOT NULL)"),
]


def run_parity(executor) -> list[tuple[str, list[tuple]]]:
    """Return [(check_name, [(a, b), ...]), ...] for every check that found offenders.
    `executor` is any object with `.execute(text(sql))` — an alembic Connection or a Session."""
    found: list[tuple[str, list[tuple]]] = []
    for name, sql in PARITY_CHECKS:
        rows = executor.execute(text(sql)).fetchall()
        if rows:
            found.append((name, [(r.a, r.b) for r in rows]))
    return found
