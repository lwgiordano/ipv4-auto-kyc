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

**What this fence does NOT cover.** It orders the WITNESS writers — the publisher's attempt and
terminal transactions, and retention — against maintenance. The pipeline's decide transaction is
a different writer: it locks the case row (`SELECT … FOR UPDATE`) and only then inserts the
outbox row, and it does not take this fence. A migration that locks `outbox` before `cases` can
therefore still deadlock with a live decide transaction, and the live-claim preflight cannot see
one (a run mid-decide holds no outbox claim). That case is covered by the DRAINED cutover, which
stops the pipeline along with every other writer — not by this fence. Do not describe the fence
as making maintenance safe against arbitrary concurrent writers; it does not.
"""

from sqlalchemy import text

# Keep in sync with every `_FENCE_KEY` constant in alembic/versions/017_* and later (a migration
# cannot import application code, so the value is asserted equal by test_outbox_fence.py).
MAINTENANCE_FENCE_KEY = 720170001

_SHARED_FENCE_SQL = text("SELECT pg_advisory_xact_lock_shared(:k)")


def take_shared_fence(session) -> None:
    """Acquire the writer side of the maintenance fence on `session`'s transaction.

    Must be the first statement in the transaction. The lock is released at commit/rollback —
    `pg_advisory_xact_lock_shared` is transaction-scoped, so no writer can leak it.
    """
    session.execute(_SHARED_FENCE_SQL, {"k": MAINTENANCE_FENCE_KEY})
