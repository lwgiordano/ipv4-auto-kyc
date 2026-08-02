"""Immutable, operation-specific physical-shape contracts for the maintenance CLIs (re-audit
`8aba2df..2cee937` R3-F7, completed under `5b0f0b8..b75a320` R4-F4).

A revision STAMP is not a physical schema: a DB stamped at a known descendant of a floor over a
drifted shape passes lineage yet crashes mid-command. A `ShapeContract` is the SINGLE object a
command's prerequisite, diagnostic and mutator all import and check identically — and, for a mutator,
re-check under its own lock. Every mismatch becomes a governed `OPS_COMMAND_SCHEMA_REFUSED` with NO
traceback and NO mutation.

What this layer asserts for every relation a command consumes:
- **relkind** via `pg_class` — the relation is an ORDINARY TABLE (`relkind='r'`), so a VIEW/matview
  named `outbox`/`decisions` (which `information_schema.tables` happily lists) cannot certify a false
  OK by filtering rows out of sight (re-audit R4-F4 repro d).
- **each consumed column** — existence, normalized `information_schema` `data_type` (so an INTEGER
  `outbox.status` or a TEXT `decisions.manual` is refused before the command compares it, repros a/c),
  and nullability where that is stable across the phase the command runs in.

The outbox→`outbox_id_seq` binding and (where required) sequence ownership are enforced separately by
`binding.bind()` for every ops command, so the profile plus bind together cover
relation/relkind/columns/types/nullability/sequence. Column DEFAULTS and specific
constraint/trigger/function DEFINITIONS are asserted only where a shipped command depends on them
(none beyond the sequence today); the framework carries the column/type/nullability/relkind matrix
that closes the reproduced certify-then-crash cases.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import text

# Normalized information_schema.columns.data_type values for the SQLAlchemy column types in use.
TEXT = "text"
BOOLEAN = "boolean"
UUID = "uuid"
TIMESTAMPTZ = "timestamp with time zone"
INTEGER = "integer"
BIGINT = "bigint"
JSONB = "jsonb"


@dataclass(frozen=True)
class ColumnShape:
    """Required shape of one consumed column. `nullable`/`data_type` are asserted only when set —
    `None` means "do not assert" (used for columns whose nullability is revision-sensitive at the
    phase this command runs in, e.g. a column a later migration flips to NOT NULL)."""

    nullable: bool | None = None
    data_type: str | None = None


@dataclass(frozen=True)
class ShapeContract:
    """One operation's required physical shape. `relations` maps a public relation name to the columns
    the operation consumes; an empty column map asserts only that the relation exists AS A TABLE."""

    name: str
    relations: dict[str, dict[str, ColumnShape]] = field(default_factory=dict)


def shape_mismatches(session, contract: ShapeContract) -> list[str]:
    """Return human-readable mismatches for `contract` against the live schema (empty ⇒ matches).
    Reads only the catalog; performs no mutation and takes no lock."""
    problems: list[str] = []
    for relation, columns in contract.relations.items():
        relkind = session.execute(
            text(
                "SELECT c.relkind FROM pg_catalog.pg_class c "
                "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname='public' AND c.relname=:t"
            ),
            {"t": relation},
        ).scalar_one_or_none()
        if relkind is None:
            problems.append(f"relation public.{relation} is absent")
            continue
        if relkind != "r":  # v=view, m=matview, f=foreign, p=partitioned — none is a plain table
            problems.append(
                f"public.{relation} is relkind {relkind!r}, not an ordinary table ('r') — "
                "a view/foreign table can hide rows and certify a false OK"
            )
            continue
        if not columns:
            continue
        actual = {
            r.column_name: r
            for r in session.execute(
                text(
                    "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name=:t"
                ),
                {"t": relation},
            )
        }
        for col, want in columns.items():
            got = actual.get(col)
            if got is None:
                problems.append(f"public.{relation}.{col} is absent")
                continue
            if want.data_type is not None and got.data_type != want.data_type:
                problems.append(
                    f"public.{relation}.{col} type is {got.data_type!r}, "
                    f"contract requires {want.data_type!r}"
                )
            if want.nullable is not None:
                is_nullable = got.is_nullable == "YES"
                if is_nullable != want.nullable:
                    problems.append(
                        f"public.{relation}.{col} is {'NULL' if is_nullable else 'NOT NULL'}, "
                        f"contract requires {'NULL' if want.nullable else 'NOT NULL'}"
                    )
    return problems


# ── Operation contracts (the shared source of truth; commands import THESE constants) ──────────────

# reset_interrupted_outbox_claims (schema 013). `status` is the NOT-NULL text lifecycle column it
# filters on — an INTEGER status crashes the `status='pending'` comparison (R4-F4 repro a). The three
# claim columns MUST be nullable (the command sets them NULL) and carry their real types.
RESET_OUTBOX_CLAIMS = ShapeContract(
    "reset_interrupted_outbox_claims",
    {
        "outbox": {
            "status": ColumnShape(nullable=False, data_type=TEXT),
            "claim_token": ColumnShape(nullable=True, data_type=UUID),
            "claim_lease_expires_at": ColumnShape(nullable=True, data_type=TIMESTAMPTZ),
            "claimed_by": ColumnShape(nullable=True, data_type=TEXT),
        }
    },
)

# The 7b-core PRE-WINDOW diagnostics — verify_pr7b_ops_prerequisites and verify_pr7b_core_backfill —
# both bind at exact revision 012 and read the decision→callback parity, so they import this SAME
# object. It asserts the relations are TABLES and the parity's referenced columns exist with the right
# types: a placeholder `decisions` with only `id` (missing run_id) or a TEXT `decisions.manual` are
# refused before the parity SQL tracebacks (R4-F4 repros b/c/d). Nullability of case_id is NOT asserted
# here — migration 013 (not yet applied at this phase) flips it to NOT NULL.
PR7B_CORE_PREWINDOW = ShapeContract(
    "pr7b_core_prewindow",
    {
        "outbox": {
            "id": ColumnShape(data_type=BIGINT),
            "kind": ColumnShape(nullable=False, data_type=TEXT),
            "status": ColumnShape(nullable=False, data_type=TEXT),
            "run_id": ColumnShape(data_type=TEXT),
            "case_id": ColumnShape(data_type=TEXT),
        },
        "decisions": {
            "id": ColumnShape(data_type=TEXT),
            "run_id": ColumnShape(data_type=TEXT),
            "manual": ColumnShape(data_type=BOOLEAN),
            "case_id": ColumnShape(data_type=TEXT),
        },
        "cases": {"id": ColumnShape(data_type=TEXT)},
        "runs": {"id": ColumnShape(data_type=TEXT), "case_id": ColumnShape(data_type=TEXT)},
    },
)

# restore_pr7b_core_callback (schema 012) re-inserts one schema-012 outbox row and reads the
# decision linkage. It consumes these columns by name in its INSERT + acceptance predicate; a wrong
# type would fail the INSERT/comparison mid-window. case_id nullability is 012-sensitive, so unset.
RESTORE_OUTBOX_CALLBACK = ShapeContract(
    "restore_pr7b_core_callback",
    {
        "outbox": {
            "id": ColumnShape(data_type=BIGINT),
            "kind": ColumnShape(nullable=False, data_type=TEXT),
            "case_id": ColumnShape(data_type=TEXT),
            "run_id": ColumnShape(data_type=TEXT),
            "payload_json": ColumnShape(data_type=JSONB),
            "status": ColumnShape(nullable=False, data_type=TEXT),
            "attempts": ColumnShape(data_type=INTEGER),
            "next_attempt_at": ColumnShape(data_type=TIMESTAMPTZ),
            "delivered_at": ColumnShape(nullable=True, data_type=TIMESTAMPTZ),
            "last_error": ColumnShape(nullable=True, data_type=TEXT),
            "created_at": ColumnShape(data_type=TIMESTAMPTZ),
        },
        "decisions": {
            "id": ColumnShape(data_type=TEXT),
            "case_id": ColumnShape(data_type=TEXT),
            "run_id": ColumnShape(data_type=TEXT),
        },
    },
)

# repair_outbox_sequence realigns outbox_id_seq to max(outbox.id). It consumes outbox.id; the
# sequence binding + ownership are enforced by bind(require_sequence_owner=True).
SEQUENCE_REPAIR = ShapeContract(
    "repair_outbox_sequence",
    {"outbox": {"id": ColumnShape(nullable=False, data_type=BIGINT)}},
)
