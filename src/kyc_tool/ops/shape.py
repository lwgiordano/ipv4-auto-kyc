"""Immutable, operation-specific physical-shape contracts for the maintenance CLIs (re-audit
`8aba2df..2cee937` R3-F7).

A revision STAMP is not a physical schema: a DB stamped at a known descendant of a floor over a
drifted shape passes lineage yet crashes mid-command. `binding.bind(require_columns=...)` only proved
column PRESENCE for one command, so a shipped preflight still certified schemas the next command
tracebacked against — e.g. `decisions` dropped at stamp 012 (prerequisite exits OK, backfill
`UndefinedTable`), or `claim_token` flipped to NOT NULL (reset's presence check passes, then it
crashes clearing the column under ACCESS EXCLUSIVE).

A `ShapeContract` is the SINGLE object a command's prerequisite, diagnostic and mutator all import and
check identically — and, for a mutator, re-check under its own lock. Every mismatch becomes a governed
`OPS_COMMAND_SCHEMA_REFUSED` with NO traceback and NO mutation.

SCOPE (honest): this layer covers relation existence + each referenced column's nullability (and,
optionally, its normalized `information_schema` type). It deliberately does NOT yet assert
constraints, triggers, functions, defaults, or sequence ownership — `bind()` already checks the
outbox→sequence binding separately, and the remaining constraint/trigger/function/sequence-owner
matrix is the deferred production-ops-hardening layer (ROADMAP PR 10). It closes the two shipped
certify-then-crash reproductions above at the class level for the columns/relations each command
consumes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import text


@dataclass(frozen=True)
class ColumnShape:
    """Required shape of one column. `nullable` is asserted; `data_type` (an
    `information_schema.columns.data_type` string, e.g. 'uuid', 'text') is asserted only when set."""

    nullable: bool
    data_type: str | None = None


@dataclass(frozen=True)
class ShapeContract:
    """One operation's required physical shape. `relations` maps a public relation name to the columns
    the operation consumes; an empty column map asserts only that the relation EXISTS."""

    name: str
    relations: dict[str, dict[str, ColumnShape]] = field(default_factory=dict)


def shape_mismatches(session, contract: ShapeContract) -> list[str]:
    """Return human-readable mismatches for `contract` against the live schema (empty ⇒ matches).
    Reads only the catalog (`information_schema`); performs no mutation and takes no lock."""
    problems: list[str] = []
    for relation, columns in contract.relations.items():
        present = session.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name=:t"
            ),
            {"t": relation},
        ).first()
        if present is None:
            problems.append(f"relation public.{relation} is absent")
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
            is_nullable = got.is_nullable == "YES"
            if is_nullable != want.nullable:
                problems.append(
                    f"public.{relation}.{col} is {'NULL' if is_nullable else 'NOT NULL'}, "
                    f"contract requires {'NULL' if want.nullable else 'NOT NULL'}"
                )
            if want.data_type is not None and got.data_type != want.data_type:
                problems.append(
                    f"public.{relation}.{col} type is {got.data_type!r}, "
                    f"contract requires {want.data_type!r}"
                )
    return problems


# ── Operation contracts (the shared source of truth; commands import THESE constants) ──────────────

# reset_interrupted_outbox_claims clears the claim tuple on pending rows. `status` is the NOT-NULL
# lifecycle column it filters on; the three claim columns MUST be nullable — the command sets them
# NULL, so a claim_token flipped to NOT NULL must refuse, not crash under ACCESS EXCLUSIVE.
RESET_OUTBOX_CLAIMS = ShapeContract(
    "reset_interrupted_outbox_claims",
    {
        "outbox": {
            "status": ColumnShape(nullable=False),
            "claim_token": ColumnShape(nullable=True),
            "claim_lease_expires_at": ColumnShape(nullable=True),
            "claimed_by": ColumnShape(nullable=True),
        }
    },
)

# The 7b-core PRE-WINDOW diagnostics — verify_pr7b_ops_prerequisites and verify_pr7b_core_backfill —
# both bind at exact revision 012 and read the decision→callback parity. They import this SAME object,
# so a dropped `decisions` is refused before the parity matrix tracebacks UndefinedTable (re-audit
# `8aba2df..2cee937` R3-F7). Column nullability of `case_id`/`ordering_stream` is deliberately NOT
# asserted here: migration 013 (not yet applied at this phase) performs those SET NOT NULLs, so a
# 012-phase contract asserts relation EXISTENCE only — the invariant the reproduction needs.
PR7B_CORE_PREWINDOW = ShapeContract(
    "pr7b_core_prewindow",
    {"outbox": {}, "decisions": {}},
)
