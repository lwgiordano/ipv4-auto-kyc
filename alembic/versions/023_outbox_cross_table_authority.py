"""outbox cross-table authority validation

Revision ID: 023
Revises: 022

Revision 022 validated the owned trigger/function authority surface, but it did not validate the
cross-table authority constraints that make the read pointers and outbox callback binding belong
to the same case. A drifted 021 database could drop those FKs, write cross-case pointers, and then
upgrade through 022 cleanly. This revision is deliberately validation-only: canonical databases
move forward unchanged; drifted databases refuse before 7b-activation can build on bad authority.
"""

import sqlalchemy as sa
from alembic import op

revision = "023"
down_revision = "022"
branch_labels = None
depends_on = None

_SENTINEL = "MIGRATION_023_CROSS_TABLE_AUTHORITY_MISMATCH"
_FENCE_KEY = 720170001

_EXPECTED_CONSTRAINTS = {
    ("outbox", "fk_outbox_decision_triple"): (
        "FOREIGN KEY (run_id, case_id, decision_sequence) "
        "REFERENCES decisions(run_id, case_id, decision_sequence)"
    ),
    ("cases", "fk_cases_latest_decision"): (
        "FOREIGN KEY (latest_decision_row_id, id) REFERENCES decisions(id, case_id)"
    ),
    ("cases", "fk_cases_latest_manual_decision"): (
        "FOREIGN KEY (latest_manual_decision_row_id, id) REFERENCES decisions(id, case_id)"
    ),
    ("decisions", "uq_decisions_run_case_sequence"): (
        "UNIQUE (run_id, case_id, decision_sequence)"
    ),
    ("decisions", "uq_decisions_id_case_id"): "UNIQUE (id, case_id)",
    ("decisions", "uq_decisions_case_decision_sequence"): (
        "UNIQUE (case_id, decision_sequence)"
    ),
}


def _constraint_definitions(conn) -> dict[tuple[str, str], tuple[str, bool]]:
    names = sorted({name for _, name in _EXPECTED_CONSTRAINTS})
    rows = conn.execute(
        sa.text(
            "SELECT c.relname AS rel, con.conname, con.convalidated, "
            "pg_get_constraintdef(con.oid) AS definition "
            "FROM pg_constraint con "
            "JOIN pg_class c ON c.oid = con.conrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = current_schema() AND con.conname = ANY(:names)"
        ),
        {"names": names},
    )
    return {
        (row.rel, row.conname): (row.definition, bool(row.convalidated))
        for row in rows
    }


def _validate_constraints(conn) -> list[str]:
    found = _constraint_definitions(conn)
    problems: list[str] = []
    for key, expected in _EXPECTED_CONSTRAINTS.items():
        actual = found.get(key)
        if actual is None:
            problems.append(f"{key[0]}.{key[1]}: missing")
            continue
        definition, validated = actual
        if definition != expected:
            problems.append(
                f"{key[0]}.{key[1]}: definition {definition!r} != expected {expected!r}"
            )
        if not validated:
            problems.append(f"{key[0]}.{key[1]}: NOT VALID")
    return problems


def _validate_data(conn) -> list[str]:
    problems: list[str] = []

    bad_latest = conn.execute(
        sa.text(
            "SELECT c.id AS case_id, c.latest_decision_row_id AS pointer, d.case_id AS decision_case "
            "FROM cases c LEFT JOIN decisions d ON d.id = c.latest_decision_row_id "
            "WHERE c.latest_decision_row_id IS NOT NULL "
            "AND (d.id IS NULL OR d.case_id <> c.id) ORDER BY c.id LIMIT 20"
        )
    ).fetchall()
    if bad_latest:
        problems.append(
            "latest_decision_row_id data: "
            + ", ".join(
                f"case={r.case_id} pointer={r.pointer} decision_case={r.decision_case}"
                for r in bad_latest
            )
        )

    bad_manual = conn.execute(
        sa.text(
            "SELECT c.id AS case_id, c.latest_manual_decision_row_id AS pointer, "
            "d.case_id AS decision_case, d.manual "
            "FROM cases c LEFT JOIN decisions d ON d.id = c.latest_manual_decision_row_id "
            "WHERE c.latest_manual_decision_row_id IS NOT NULL "
            "AND (d.id IS NULL OR d.case_id <> c.id OR d.manual IS NOT TRUE) "
            "ORDER BY c.id LIMIT 20"
        )
    ).fetchall()
    if bad_manual:
        problems.append(
            "latest_manual_decision_row_id data: "
            + ", ".join(
                f"case={r.case_id} pointer={r.pointer} decision_case={r.decision_case} "
                f"manual={r.manual}"
                for r in bad_manual
            )
        )

    bad_outbox = conn.execute(
        sa.text(
            "SELECT o.id AS outbox_id, o.case_id AS outbox_case, o.run_id, "
            "o.decision_sequence, d.case_id AS decision_case "
            "FROM outbox o LEFT JOIN decisions d "
            "ON d.run_id = o.run_id AND d.case_id = o.case_id "
            "AND d.decision_sequence = o.decision_sequence "
            "WHERE o.kind = 'decision_callback' "
            "AND (d.id IS NULL OR d.case_id <> o.case_id) ORDER BY o.id LIMIT 20"
        )
    ).fetchall()
    if bad_outbox:
        problems.append(
            "fk_outbox_decision_triple data: "
            + ", ".join(
                f"outbox={r.outbox_id} outbox_case={r.outbox_case} run={r.run_id} "
                f"sequence={r.decision_sequence} decision_case={r.decision_case}"
                for r in bad_outbox
            )
        )

    return problems


def upgrade() -> None:
    conn = op.get_bind()
    op.execute(sa.text("SELECT pg_advisory_xact_lock(:k)").bindparams(k=_FENCE_KEY))
    op.execute("LOCK TABLE outbox IN ACCESS EXCLUSIVE MODE")
    op.execute("LOCK TABLE decisions IN ACCESS EXCLUSIVE MODE")
    op.execute("LOCK TABLE cases IN ACCESS EXCLUSIVE MODE")

    problems = _validate_constraints(conn) + _validate_data(conn)
    if problems:
        raise RuntimeError(
            f"migration 023: {_SENTINEL} — cross-table authority constraints and data must "
            f"match the reviewed 7b-core model before activation can build on them. "
            f"Problems: {problems}"
        )


def downgrade() -> None:
    # Validation-only revision: downgrading one step merely removes the stamp. Earlier 7b-core
    # revisions remain forward-only where they actually changed durable authority.
    pass
