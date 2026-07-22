"""validate pinning check constraints: VALIDATE CONSTRAINT for 011's NOT VALID CHECKs

Revision ID: 012
Revises: 011

Migration 011 added three provenance-column CHECK constraints on the existing
checks/runs/decisions tables — ck_checks_policy_bundle_hash_nonblank,
ck_runs_engine_build_id_nonblank, ck_decisions_engine_build_id_nonblank — as
NOT VALID: each is enforced against every new/updated row immediately, but not
yet proven against the rows that already existed when 011 ran.

This is a SEPARATE follow-on revision rather than folded into 011 because
`ALTER TABLE ... VALIDATE CONSTRAINT` only needs a SHARE UPDATE EXCLUSIVE lock
— compatible with concurrent reads and writes — while a single-step validating
`ADD CONSTRAINT` takes ACCESS EXCLUSIVE for the entire table scan and holds it
until commit. On a production-sized checks/decisions table, that one-step form
would stall every concurrent ingest/decide writer for the duration of the
scan. Splitting the NOT VALID add (011, brief metadata-only lock) from the
VALIDATE (here, non-blocking scan) keeps both steps hot-compatible, even
though Alembic still wraps this migration's statements in one transaction —
VALIDATE's weaker lock is what matters, not the transaction boundary.

Downgrade is a no-op: PostgreSQL has no "un-validate constraint" operation,
and 011's own downgrade drops the columns (and with them these constraints)
entirely when it is safe to do so.
"""

from alembic import op

revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE checks VALIDATE CONSTRAINT ck_checks_policy_bundle_hash_nonblank")
    op.execute("ALTER TABLE runs VALIDATE CONSTRAINT ck_runs_engine_build_id_nonblank")
    op.execute("ALTER TABLE decisions VALIDATE CONSTRAINT ck_decisions_engine_build_id_nonblank")


def downgrade() -> None:
    # No-op: PostgreSQL has no "un-validate constraint" operation — a constraint
    # marked valid simply stays valid. Downgrading past this revision means
    # downgrading 011 too, which drops the columns (and with them the
    # constraints) entirely; there is nothing for this revision to undo on its
    # own.
    pass
