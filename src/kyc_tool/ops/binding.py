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
STATEMENT_TIMEOUT_SENTINEL = "OPS_COMMAND_STATEMENT_TIMEOUT"
SEQUENCE_OWNER_SENTINEL = "OPS_COMMAND_NOT_SEQUENCE_OWNER"
# Every schema-identity / phase / cardinality refusal `bind()` raises carries this sentinel, so a
# one-shot CLI's main() catches `BindingRefused`, prints the stable line, and exits nonzero WITHOUT
# a Python traceback (re-audit `538e55e..42e1c7d` F4).
SCHEMA_REFUSED_SENTINEL = "OPS_COMMAND_SCHEMA_REFUSED"

# psycopg surfaces a timed-out lock as SQLSTATE 55P03 (lock_not_available); a statement that
# overruns statement_timeout as 57014 (query_canceled).
_LOCK_NOT_AVAILABLE = "55P03"
_QUERY_CANCELED = "57014"

# PostgreSQL stores lock_timeout/statement_timeout as a signed 32-bit millisecond value; a setting
# that overflows it is rejected by the server mid-command. We refuse such a value up front as a
# governed BindingRefused instead of letting it traceback (re-audit `42e1c7d..b39b82a` F7).
_PG_MAX_TIMEOUT_MS = 2_147_483_647


class BindingRefused(RuntimeError):
    """A governed pre-flight refusal from `bind()`: wrong/absent/multi-head schema revision, a
    sequence-ownership gap, or an incoherent timeout budget. A `RuntimeError` subclass so existing
    broad handlers still catch it, but a distinct type so every ops `main()` can turn it into a
    stable, traceback-free, non-mutating exit (re-audit `538e55e..42e1c7d` F4)."""


def _revision_is_known(version: str) -> bool:
    """True iff `version` is a revision that EXISTS in the checked-out Alembic graph (re-audit
    `d569a15..4938840` F8). Graph resolution previously ran ONLY when a caller supplied a floor/exact
    revision, so a no-floor command (`repair_outbox_sequence`) would happily mutate a DB stamped with
    an unknown revision like '999'. Every command now resolves the stamp through the graph. Unknown →
    False (fail closed)."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from kyc_tool.config import REPO_ROOT

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    try:
        return script.get_revision(version) is not None
    except Exception:
        return False


def _revision_is_at_or_after(version: str, floor: str) -> bool:
    """True iff `version` is `floor` or a descendant of it in the CHECKED-OUT Alembic graph — real
    lineage, not string ordering (re-audit `b39b82a..b53daf4` F8). A spoofed/unknown revision like
    '999' string-compares `>= '013'` yet is not a descendant of 013, so the old `version < floor`
    let it satisfy a floor it never reached and a phase-specific command then hit columns that do
    not exist. Loaded the same way `api/app.py` resolves the head; unknown/unreachable → False
    (fail closed)."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from kyc_tool.config import REPO_ROOT

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    try:
        # `version` down to base; if `floor` appears in that ancestry, version is floor-or-later.
        ancestry = {rev.revision for rev in script.iterate_revisions(version, "base")}
    except Exception:
        return False  # `version` is not a known revision in this graph
    return floor in ancestry


def bind(
    session,
    *,
    lock_timeout_seconds: int,
    statement_timeout_seconds: int | None = None,
    min_revision: str | None = None,
    exact_revision: str | None = None,
    require_sequence_owner: bool = False,
    require_columns: dict[str, tuple[str, ...]] | None = None,
) -> None:
    """Pin the transaction to the governed schema and bound its waits. Call FIRST.

    Raises `BindingRefused` (fail-closed, nothing touched) when the governed schema is absent,
    empty, or in a multi-head/invalid state (`alembic_version` cardinality != 1), off
    `exact_revision`/below `min_revision`, `public.outbox` is not backed by `public.outbox_id_seq`,
    or (when `require_sequence_owner`) the current role does not OWN the sequence — the last is a
    preflight so an operator learns it BEFORE entering maintenance, not at `ALTER SEQUENCE`
    (re-audit `8377440` F13).

    `statement_timeout_seconds` is the SEPARATE per-statement ceiling (its own governed budget, not
    a constant derived from the lock — re-audit `538e55e..42e1c7d` F11). It must exceed
    `lock_timeout_seconds` (a statement may wait most of the lock budget, then run its query); a
    ceiling at or below the lock budget would cancel the lock wait before `lock_timeout` fires and
    misclassify the failure, so `bind()` refuses it. When omitted it falls back to
    `lock_timeout_seconds + 300` (the historical headroom) so non-production callers keep working.
    """
    lock_s = int(lock_timeout_seconds)
    statement_s = lock_s + 300 if statement_timeout_seconds is None else int(statement_timeout_seconds)
    if statement_s <= lock_s:
        raise BindingRefused(
            f"{SCHEMA_REFUSED_SENTINEL}: statement_timeout ({statement_s}s) must exceed "
            f"lock_timeout ({lock_s}s) — a statement ceiling at or below the lock budget cancels "
            f"the lock wait before lock_timeout fires and misclassifies the failure. Fix "
            f"KYC_OPS_STATEMENT_TIMEOUT_SECONDS / KYC_OPS_LOCK_TIMEOUT_SECONDS."
        )
    for label, secs, env in (
        ("lock_timeout", lock_s, "KYC_OPS_LOCK_TIMEOUT_SECONDS"),
        ("statement_timeout", statement_s, "KYC_OPS_STATEMENT_TIMEOUT_SECONDS"),
    ):
        if secs * 1000 > _PG_MAX_TIMEOUT_MS:
            raise BindingRefused(
                f"{SCHEMA_REFUSED_SENTINEL}: {label} of {secs}s = {secs * 1000}ms exceeds "
                f"PostgreSQL's maximum {_PG_MAX_TIMEOUT_MS}ms (~24.8 days); refusing before "
                f"SET LOCAL. Lower {env}."
            )
    session.execute(text("SET LOCAL search_path = pg_catalog, public"))
    # both take a literal; the values are our own bounded ints, never operator text
    session.execute(text(f"SET LOCAL lock_timeout = '{lock_s * 1000}ms'"))
    session.execute(text(f"SET LOCAL statement_timeout = '{statement_s * 1000}ms'"))
    has_table = session.execute(
        text(
            "SELECT 1 FROM pg_catalog.pg_tables "
            "WHERE schemaname='public' AND tablename='alembic_version'"
        )
    ).first()
    # Read the FULL ordered version set and require cardinality one BEFORE any comparison: `.scalar()`
    # reads one arbitrary row, so a multi-head `{012, 999}` would silently satisfy an exact/floor
    # check against whichever row came back (re-audit `538e55e..42e1c7d` F4).
    versions = (
        sorted(session.execute(text("SELECT version_num FROM public.alembic_version")).scalars().all())
        if has_table
        else []
    )
    if not versions:
        raise BindingRefused(
            f"{SCHEMA_REFUSED_SENTINEL}: public.alembic_version is absent or empty — this database "
            "is not the governed schema this command maintains"
        )
    if len(versions) > 1:
        raise BindingRefused(
            f"{SCHEMA_REFUSED_SENTINEL}: public.alembic_version holds {len(versions)} rows "
            f"({versions}) — a multi-head/invalid migration state; refusing before any comparison "
            "or mutation. Resolve the migration heads first."
        )
    version = versions[0]
    # ALWAYS resolve the singleton stamp through the graph — not only when a floor/exact is given
    # (re-audit `d569a15..4938840` F8). A valid label string is not a valid schema.
    if not _revision_is_known(version):
        raise BindingRefused(
            f"{SCHEMA_REFUSED_SENTINEL}: public.alembic_version={version!r} is not a revision known "
            "to this build's migration graph — refusing to operate on an unknown/spoofed schema "
            "stamp (every ops command resolves the stamp through the graph, not just gated ones)"
        )
    if exact_revision is not None and version != exact_revision:
        raise BindingRefused(
            f"{SCHEMA_REFUSED_SENTINEL}: public.alembic_version={version!r} is not the required "
            f"revision {exact_revision!r} (this command is phase-specific)"
        )
    if min_revision is not None and not _revision_is_at_or_after(version, min_revision):
        raise BindingRefused(
            f"{SCHEMA_REFUSED_SENTINEL}: public.alembic_version={version!r} is not revision "
            f"{min_revision!r} or a descendant of it in the migration graph — this command needs "
            f"the {min_revision!r} schema (string ordering is not lineage; an unknown/spoofed "
            f"revision is refused here rather than tracebacking on a missing column)"
        )
    backing = session.execute(
        text("SELECT pg_get_serial_sequence('public.outbox', 'id')")
    ).scalar()
    if backing != "public.outbox_id_seq":
        raise BindingRefused(
            f"{SCHEMA_REFUSED_SENTINEL}: public.outbox.id is backed by {backing!r}, not "
            "public.outbox_id_seq — object identity cannot be trusted"
        )
    if require_sequence_owner:
        owner_ok = session.execute(
            text(
                "SELECT pg_catalog.pg_get_userbyid(c.relowner) = current_user "
                "FROM pg_catalog.pg_class c "
                "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname='public' AND c.relname='outbox_id_seq'"
            )
        ).scalar()
        if not owner_ok:
            raise BindingRefused(
                f"{SEQUENCE_OWNER_SENTINEL}: current_user does not OWN public.outbox_id_seq — "
                "ALTER SEQUENCE requires ownership, not merely ALL privileges. Run this command "
                "as the sequence's owning role (the migration/ops credential; see RUNBOOK)."
            )

    # Structural preflight (re-audit `d3c0852..23e005e` F6): a revision STAMP is not the physical
    # schema. A DB stamped at a known descendant of the floor (e.g. `023` set by hand) over a drifted
    # or wrong physical shape passes the lineage check yet would traceback mid-mutation on a missing
    # column. Verify every column the command will actually touch EXISTS before any lock or write —
    # lineage answers "which migration", this answers "does the shape match".
    if require_columns:
        for table, cols in require_columns.items():
            present = {
                r[0]
                for r in session.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name=:t"
                    ),
                    {"t": table},
                )
            }
            missing = [c for c in cols if c not in present]
            if missing:
                raise BindingRefused(
                    f"{SCHEMA_REFUSED_SENTINEL}: public.{table} is missing required column(s) "
                    f"{sorted(missing)} — the alembic_version stamp does not match the physical "
                    "schema this command mutates (a valid revision label is not a valid shape)"
                )


def _sqlstate(exc: BaseException) -> str | None:
    orig = getattr(exc, "orig", exc) if isinstance(exc, OperationalError) else exc
    return getattr(orig, "sqlstate", None)


def is_lock_timeout(exc: BaseException) -> bool:
    """True when `exc` is (or wraps) PostgreSQL's lock_not_available (55P03)."""
    return _sqlstate(exc) == _LOCK_NOT_AVAILABLE


def is_statement_timeout(exc: BaseException) -> bool:
    """True when `exc` is (or wraps) PostgreSQL's query_canceled (57014)."""
    return _sqlstate(exc) == _QUERY_CANCELED


def timeout_message(command: str, held_for: str, exc: BaseException) -> str | None:
    """Stable operator refusal for a bounded-wait timeout, or None if `exc` is neither.

    One classifier for both bounds so every ops CLI handles them identically: an orphan LOCK and
    a runaway STATEMENT each get a distinct sentinel and the same "nothing changed, find the
    blocker, re-run" guidance."""
    if is_lock_timeout(exc):
        return (
            f"{LOCK_TIMEOUT_SENTINEL}: {command} could not acquire {held_for} within the bound "
            f"(KYC_OPS_LOCK_TIMEOUT_SECONDS) — another transaction holds a conflicting lock. "
            f"Nothing was changed. Find and resolve the blocker (pg_stat_activity / pg_locks), "
            f"then re-run."
        )
    if is_statement_timeout(exc):
        return (
            f"{STATEMENT_TIMEOUT_SENTINEL}: {command} exceeded its statement_timeout while "
            f"working on {held_for} — a query ran longer than the bound allows. Nothing was "
            f"changed. Investigate the blocker/load, then re-run."
        )
    return None


def lock_timeout_message(command: str, held_for: str) -> str:
    """Back-compat shim: the lock-timeout half of `timeout_message`."""
    return (
        f"{LOCK_TIMEOUT_SENTINEL}: {command} could not acquire {held_for} within the bound "
        f"(KYC_OPS_LOCK_TIMEOUT_SECONDS) — another transaction holds a conflicting lock. "
        f"Nothing was changed. Find and resolve the blocker "
        f"(pg_stat_activity / pg_locks), then re-run."
    )
