"""The single maintenance fence shared by every outbox-authority writer and every migration.

One advisory-lock namespace, defined ONCE. Writers (the publisher's attempt/terminal transactions,
retention's prune) take it SHARED — they never block each other. Migrations `017`+ take it
EXCLUSIVE before touching any table.

Why a fence at all: child-first table locking does not eliminate deadlocks, it only reorders them.
The publisher's terminal path locks the parent `outbox` row and its trigger then reads the child
`outbox_delivery_attempts`; retention updates the parent and then deletes from the child; a
migration that locks child-then-parent forms a real `40P01` cycle with either of them (re-audits
`15d875d` F5 and `cbb783b` F4). With one shared fence taken FIRST by every participant, the cycle
cannot form at all: maintenance simply queues behind live writers.

`prune()` and the publisher take this as the FIRST transactional statement, before any DML — a
fence acquired after the parent is already locked would fence nothing.
"""

from sqlalchemy import text

# Keep in sync with the `_FENCE_KEY` constant in alembic/versions/017_* and 018_* (a migration
# cannot import application code, so the value is asserted equal by test_outbox_fence.py).
MAINTENANCE_FENCE_KEY = 720170001

_SHARED_FENCE_SQL = text("SELECT pg_advisory_xact_lock_shared(:k)")


def take_shared_fence(session) -> None:
    """Acquire the writer side of the maintenance fence on `session`'s transaction.

    Must be the first statement in the transaction. The lock is released at commit/rollback —
    `pg_advisory_xact_lock_shared` is transaction-scoped, so no writer can leak it.
    """
    session.execute(_SHARED_FENCE_SQL, {"k": MAINTENANCE_FENCE_KEY})
