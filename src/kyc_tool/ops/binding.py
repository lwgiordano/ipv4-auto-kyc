"""Trusted execution binding for one-shot maintenance commands.

Every ops command in this package mutates or attests the GOVERNED schema — `public` — and is
run by an operator pasting a database URL mid-window. Two failure classes follow from that
shape (Codex re-audit `f495de8` F4/F10), and this module closes both at the top of every
command's transaction:

- **Object identity.** A URL carrying `?options=-csearch_path=shadow,public` (or a role-level
  `search_path`) silently redirects unqualified `outbox` / `outbox_id_seq` references to a
  shadow object: a reset prints zero while real claims survive, a sequence repair repairs the
  wrong sequence and prints OK. `bind()` pins `search_path` to `pg_catalog, public` for the
  transaction, verifies `public.alembic_version` exists (and, when asked, meets a revision
  floor), and verifies `public.outbox.id` is actually backed by `public.outbox_id_seq` — on
  any mismatch the command refuses BEFORE touching anything. Commands additionally
  schema-qualify every object they name.

- **Unbounded waits.** These commands take table locks by design; without a bound, one orphan
  transaction turns a pre-window check into an indefinite hang with no diagnosis. `bind()`
  sets `lock_timeout` from `KYC_OPS_LOCK_TIMEOUT_SECONDS` (default 60s), and a timed-out lock
  surfaces as the stable `OPS_COMMAND_LOCK_TIMEOUT` sentinel with the recovery action, exit
  nonzero, nothing changed.
"""

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

LOCK_TIMEOUT_SENTINEL = "OPS_COMMAND_LOCK_TIMEOUT"

# psycopg surfaces a timed-out lock as SQLSTATE 55P03 (lock_not_available).
_LOCK_NOT_AVAILABLE = "55P03"


def bind(session, *, lock_timeout_seconds: int, min_revision: str | None = None) -> None:
    """Pin the transaction to the governed schema and bound its waits. Call FIRST.

    Raises RuntimeError (fail-closed, nothing touched) when the governed schema is absent,
    below `min_revision`, or `public.outbox` is not backed by `public.outbox_id_seq`.
    """
    session.execute(text("SET LOCAL search_path = pg_catalog, public"))
    # lock_timeout takes a literal; the value is our own bounded int, never operator text
    session.execute(text(f"SET LOCAL lock_timeout = '{int(lock_timeout_seconds) * 1000}ms'"))
    has_table = session.execute(
        text(
            "SELECT 1 FROM pg_catalog.pg_tables "
            "WHERE schemaname='public' AND tablename='alembic_version'"
        )
    ).first()
    version = (
        session.execute(text("SELECT version_num FROM public.alembic_version")).scalar()
        if has_table
        else None
    )
    if version is None:
        raise RuntimeError(
            "refusing: public.alembic_version is absent or empty — this database is not the "
            "governed schema this command maintains"
        )
    if min_revision is not None and version < min_revision:
        raise RuntimeError(
            f"refusing: public.alembic_version={version!r} is below the required "
            f"revision {min_revision!r}"
        )
    backing = session.execute(
        text("SELECT pg_get_serial_sequence('public.outbox', 'id')")
    ).scalar()
    if backing != "public.outbox_id_seq":
        raise RuntimeError(
            f"refusing: public.outbox.id is backed by {backing!r}, not public.outbox_id_seq — "
            "object identity cannot be trusted"
        )


def is_lock_timeout(exc: BaseException) -> bool:
    """True when `exc` is (or wraps) PostgreSQL's lock_not_available (55P03)."""
    orig = getattr(exc, "orig", exc) if isinstance(exc, OperationalError) else exc
    return getattr(orig, "sqlstate", None) == _LOCK_NOT_AVAILABLE


def lock_timeout_message(command: str, held_for: str) -> str:
    """The stable operator-facing refusal for a timed-out lock."""
    return (
        f"{LOCK_TIMEOUT_SENTINEL}: {command} could not acquire {held_for} within the bound "
        f"(KYC_OPS_LOCK_TIMEOUT_SECONDS) — another transaction holds a conflicting lock. "
        f"Nothing was changed. Find and resolve the blocker "
        f"(pg_stat_activity / pg_locks), then re-run."
    )
