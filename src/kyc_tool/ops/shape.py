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
    `None` means "do not assert" (a column whose nullability is revision-sensitive at this phase).
    `deterministic_collation` (re-audit `03dbfab..bc325e7` R5-F4) requires the column NOT carry a
    nondeterministic (e.g. case-insensitive) collation, so a value the command compares
    case-sensitively (`kind='decision_callback'`, `status='pending'`) cannot match under a folded
    collation."""

    nullable: bool | None = None
    data_type: str | None = None
    deterministic_collation: bool = False


@dataclass(frozen=True)
class ShapeContract:
    """One operation's required physical shape. `relations` maps a public relation name to the columns
    the operation consumes; an empty column map asserts only that the relation exists AS A TABLE."""

    name: str
    relations: dict[str, dict[str, ColumnShape]] = field(default_factory=dict)
    # The revisions this command is admitted to run against (re-audit `03dbfab..bc325e7` R5-F5):
    # explicit, never inherited by column coincidence. Empty ⇒ the command's own min/exact gate
    # governs (bind()); a non-empty set is additionally enforced by supported_revision_violation().
    supported_revisions: tuple[str, ...] = ()
    # Every relation the command reads/writes, in a canonical (sorted) order the mutator locks before
    # its final under-lock re-check, so no consumed relation drifts after certification.
    lock_relations: tuple[str, ...] = ()


def supported_revision_violation(revision: str, contract: ShapeContract) -> str | None:
    """`revision` must be explicitly admitted by the contract (empty set ⇒ no extra gate)."""
    if contract.supported_revisions and revision not in contract.supported_revisions:
        return (
            f"schema revision {revision!r} is not in the {contract.name!r} supported set "
            f"{list(contract.supported_revisions)} — a future revision needs explicit admission"
        )
    return None


def shape_mismatches(session, contract: ShapeContract) -> list[str]:
    """Return human-readable mismatches for `contract` against the live schema (empty ⇒ matches).
    Reads only the catalog; performs no mutation and takes no lock."""
    problems: list[str] = []
    for relation, columns in contract.relations.items():
        rel = session.execute(
            text(
                "SELECT c.oid, c.relkind, c.relispartition, "
                "EXISTS(SELECT 1 FROM pg_catalog.pg_inherits WHERE inhrelid = c.oid) AS inherits "
                "FROM pg_catalog.pg_class c "
                "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname='public' AND c.relname=:t"
            ),
            {"t": relation},
        ).one_or_none()
        if rel is None:
            problems.append(f"relation public.{relation} is absent")
            continue
        if rel.relkind != "r":  # v=view, m=matview, f=foreign, p=partitioned — none is a plain table
            problems.append(
                f"public.{relation} is relkind {rel.relkind!r}, not an ordinary table ('r') — "
                "a view/foreign table can hide rows and certify a false OK"
            )
            continue
        # A partition child (relispartition) or an inheritance child (pg_inherits) has relkind='r'
        # yet is a filtered slice of a larger table (re-audit R5-F4): reject it as the governed table.
        if rel.relispartition or rel.inherits:
            problems.append(
                f"public.{relation} is a partition/inheritance child, not the standalone governed "
                "table — it can expose a filtered subset of rows"
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
            if want.deterministic_collation:
                nondet = session.execute(
                    text(
                        "SELECT co.collname FROM pg_catalog.pg_attribute a "
                        "JOIN pg_catalog.pg_class c ON c.oid = a.attrelid "
                        "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                        "JOIN pg_catalog.pg_collation co ON co.oid = a.attcollation "
                        "WHERE n.nspname='public' AND c.relname=:t AND a.attname=:col "
                        "AND NOT co.collisdeterministic"
                    ),
                    {"t": relation, "col": col},
                ).scalar_one_or_none()
                if nondet is not None:
                    problems.append(
                        f"public.{relation}.{col} carries nondeterministic collation {nondet!r}; "
                        "a case-sensitive comparison could match a folded value"
                    )
    return problems


_BIGINT_MAX = 9_223_372_036_854_775_807


def sequence_integrity_violations(session, *, table: str, column: str, sequence: str) -> list[str]:
    """The exact behavior repair_outbox_sequence promises to fix (re-audit `03dbfab..bc325e7` R5-F3).
    Verifying only the sequence name/owner and a last_value read-back is a FALSE success: an arithmetic
    column default (`nextval(seq)+1000`) or a non-unit/negative increment lets repair report "next 1"
    while the real allocation is 1001 or collides. Bind the exact default expression and the sequence's
    type/increment/cycle/min/max so a repaired sequence actually allocates monotonically from next_id.
    Call UNDER the outbox lock, before RESTART."""
    problems: list[str] = []
    default = session.execute(
        text(
            "SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid) FROM pg_catalog.pg_attribute a "
            "JOIN pg_catalog.pg_class c ON c.oid = a.attrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
            "WHERE n.nspname='public' AND c.relname=:t AND a.attname=:col"
        ),
        {"t": table, "col": column},
    ).scalar_one_or_none()
    norm = (default or "").strip()
    # must be EXACTLY nextval(<this sequence>::regclass) — no trailing arithmetic
    if not (norm.startswith("nextval(") and norm.endswith("::regclass)") and sequence in norm):
        problems.append(
            f"public.{table}.{column} default is {default!r}, not a bare "
            f"nextval('{sequence}'::regclass) — an arithmetic default defeats the repair"
        )
    seq = session.execute(
        text(
            "SELECT s.seqincrement, s.seqcycle, s.seqmin, s.seqmax, "
            "pg_catalog.format_type(s.seqtypid, NULL) AS typ "
            "FROM pg_catalog.pg_sequence s "
            "WHERE s.seqrelid = to_regclass(:seq)"
        ),
        {"seq": f"public.{sequence}"},
    ).one_or_none()
    if seq is None:
        problems.append(f"sequence public.{sequence} is absent")
        return problems
    if seq.typ != "bigint":
        problems.append(f"sequence public.{sequence} type is {seq.typ!r}, expected 'bigint'")
    if seq.seqincrement != 1:
        problems.append(f"sequence public.{sequence} increment is {seq.seqincrement}, expected 1")
    if seq.seqcycle:
        problems.append(f"sequence public.{sequence} cycles — a wrapped id can collide")
    if seq.seqmin != 1:
        problems.append(f"sequence public.{sequence} min_value is {seq.seqmin}, expected 1")
    if seq.seqmax != _BIGINT_MAX:
        problems.append(
            f"sequence public.{sequence} max_value is {seq.seqmax}, expected the bigint max"
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
            "status": ColumnShape(nullable=False, data_type=TEXT, deterministic_collation=True),
            "claim_token": ColumnShape(nullable=True, data_type=UUID),
            "claim_lease_expires_at": ColumnShape(nullable=True, data_type=TIMESTAMPTZ),
            "claimed_by": ColumnShape(nullable=True, data_type=TEXT),
        }
    },
    lock_relations=("outbox",),
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
            "kind": ColumnShape(nullable=False, data_type=TEXT, deterministic_collation=True),
            "status": ColumnShape(nullable=False, data_type=TEXT, deterministic_collation=True),
            "run_id": ColumnShape(data_type=TEXT),
            "case_id": ColumnShape(data_type=TEXT),
            "payload_json": ColumnShape(data_type=JSONB),  # so the preflight OKs the shape restore uses
            "delivered_at": ColumnShape(nullable=True, data_type=TIMESTAMPTZ),
        },
        "decisions": {
            "id": ColumnShape(data_type=TEXT),
            "run_id": ColumnShape(data_type=TEXT),
            # NOT NULL boolean: a NULL `manual` makes `d.manual=false` UNKNOWN and the parity CHECK
            # silently accepts it (re-audit R5-F4).
            "manual": ColumnShape(nullable=False, data_type=BOOLEAN),
            "case_id": ColumnShape(data_type=TEXT),
        },
        "cases": {"id": ColumnShape(data_type=TEXT)},
        "runs": {"id": ColumnShape(data_type=TEXT), "case_id": ColumnShape(data_type=TEXT)},
    },
    supported_revisions=("012",),
    lock_relations=("cases", "decisions", "outbox", "runs"),
)

# restore_pr7b_core_callback (schema 012) re-inserts one schema-012 outbox row and reads the
# decision linkage. It consumes these columns by name in its INSERT + acceptance predicate; a wrong
# type would fail the INSERT/comparison mid-window. case_id nullability is 012-sensitive, so unset.
RESTORE_OUTBOX_CALLBACK = ShapeContract(
    "restore_pr7b_core_callback",
    {
        "outbox": {
            "id": ColumnShape(data_type=BIGINT),
            "kind": ColumnShape(nullable=False, data_type=TEXT, deterministic_collation=True),
            "case_id": ColumnShape(data_type=TEXT),
            "run_id": ColumnShape(data_type=TEXT),
            "payload_json": ColumnShape(data_type=JSONB),
            "status": ColumnShape(nullable=False, data_type=TEXT, deterministic_collation=True),
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
            # the acceptance predicate joins on `d.manual=false` (re-audit R5-F4): a TEXT/nullable
            # manual passed the old profile then raised `text = boolean` mid-restore.
            "manual": ColumnShape(nullable=False, data_type=BOOLEAN),
        },
    },
    supported_revisions=("012",),
    lock_relations=("decisions", "outbox"),
)

# repair_outbox_sequence realigns outbox_id_seq to max(outbox.id). It consumes outbox.id; the
# sequence binding + ownership + increment/default integrity are enforced by bind() and
# sequence_integrity_violations().
SEQUENCE_REPAIR = ShapeContract(
    "repair_outbox_sequence",
    {"outbox": {"id": ColumnShape(nullable=False, data_type=BIGINT)}},
    lock_relations=("outbox",),
)
