"""Drained outbox-sequence repair (PR 7b-core RUNBOOK step 0.6d escape hatch).

Run ONLY inside a drained maintenance stop, after every writer is hard-stopped and attested
at zero. It exists because the step-0 precondition can legitimately fail when a restored id is
NOT below the sequence high-water (a restore from a divergent lineage rather than a prune).

Two things make this a shipped CLI rather than pasted SQL. `ALTER SEQUENCE ... RESTART WITH`
takes a LITERAL, not an expression, so the obvious one-liner is invalid SQL discovered mid-outage.
And the check must be fail-closed: a psql script that SELECTs a boolean still exits 0 when that
boolean is false, so a bad repair reports success. Here the exit status IS the result.

It never calls nextval() to probe: consuming an id and setval-ing it back is itself a write to
the object under repair. The read-back reads last_value/is_called from the sequence relation.

Restart target: `GREATEST(max(id), floor) + 1`. `--floor` (optional) serves the restore path —
when an authoritative row with an id ABOVE current max(id) is about to be restored, the floor
keeps the sequence from being restarted below it and later colliding (Codex re-audit `f495de8`
F1: max=10, missing id=100 → a floorless repair sets next=11, and the restored 100 collides
when the sequence catches up). `restore_pr7b_core_callback` passes it automatically; a bare run
repairs to the current table contents.

Execution is bound to the governed schema first (`ops/binding.py`): the sequence repaired is
provably `public.outbox_id_seq`, and a conflicting lock refuses with `OPS_COMMAND_LOCK_TIMEOUT`
after the bound instead of hanging the outage.

    python -m kyc_tool.ops.repair_outbox_sequence [--floor N]
"""

import argparse
import sys

from sqlalchemy import text

from kyc_tool.config import get_settings
from kyc_tool.db.session import make_engine, make_session_factory, uow
from kyc_tool.ops import binding, shape

_SEQUENCE = "public.outbox_id_seq"


def repair_sequence(session_factory, *, floor: int = 0, lock_timeout_seconds: int = 60,
                    statement_timeout_seconds: int | None = None) -> int:
    """Restart the outbox id sequence at GREATEST(max(id), floor)+1. Returns the value the
    NEXT allocation takes.

    Raises RuntimeError (rolling the transaction back) if the post-restart read-back is not
    exactly (next_id, is_called=false) — the caller must treat that as a failed repair.
    """
    if not isinstance(floor, int) or floor < 0:
        raise RuntimeError(f"refusing: floor must be a non-negative int, got {floor!r}")
    with uow(session_factory) as session:
        binding.bind(session, lock_timeout_seconds=lock_timeout_seconds,
                     statement_timeout_seconds=statement_timeout_seconds,
                     require_sequence_owner=True,  # ALTER SEQUENCE needs ownership (F13)
                     shape_contract=shape.SEQUENCE_REPAIR)
        # The fence. Writers are already stopped by the runbook; this makes that a guarantee
        # rather than an assumption, and ALTER SEQUENCE (unlike setval) excludes concurrent
        # nextval for the duration of the transaction.
        session.execute(text("LOCK TABLE public.outbox IN ACCESS EXCLUSIVE MODE"))
        # (PostgreSQL sequences cannot be LOCK TABLE'd; the outbox ACCESS EXCLUSIVE lock blocks the
        # writers that would touch it, and the integrity check + RESTART run in ONE transaction so the
        # certified definition is the one RESTART acts on — re-audit `03dbfab..bc325e7` R5-F3.)
        # Re-check the SAME contract under the lock (re-audit R4-F4): outbox must still be a table
        # with a bigint id before we realign its sequence.
        under_lock = shape.shape_mismatches(session, shape.SEQUENCE_REPAIR)
        if under_lock:
            session.rollback()
            raise binding.BindingRefused(
                f"{binding.SCHEMA_REFUSED_SENTINEL}: outbox shape changed under the maintenance "
                f"lock: {'; '.join(under_lock)}"
            )
        # Verify the sequence actually allocates monotonically from next_id (re-audit R5-F3): a bare
        # nextval default with unit increment, no cycle, bigint domain. Otherwise RESTART reports a
        # success it cannot deliver (an arithmetic default or negative increment recreates collisions).
        seq_problems = shape.sequence_integrity_violations(
            session, table="outbox", column="id", sequence="outbox_id_seq"
        )
        if seq_problems:
            session.rollback()
            raise binding.BindingRefused(
                f"{binding.SCHEMA_REFUSED_SENTINEL}: outbox_id_seq integrity: {'; '.join(seq_problems)}"
            )
        next_id = session.execute(
            text("SELECT GREATEST(COALESCE(max(id), 0), :f) + 1 FROM public.outbox"),
            {"f": floor},
        ).scalar_one()
        if not isinstance(next_id, int) or next_id < 1:  # never interpolate an untrusted value
            raise RuntimeError(f"refusing to restart {_SEQUENCE}: computed next_id={next_id!r}")
        session.execute(text(f"ALTER SEQUENCE {_SEQUENCE} RESTART WITH {next_id}"))
        last_value, is_called = session.execute(
            text(f"SELECT last_value, is_called FROM {_SEQUENCE}")
        ).one()
        if last_value != next_id or is_called:
            raise RuntimeError(
                f"{_SEQUENCE} repair FAILED read-back: expected ({next_id}, False), "
                f"got ({last_value}, {is_called}) — transaction rolled back, sequence unchanged"
            )
        return next_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--floor", type=int, default=0,
                        help="minimum id the restarted sequence must clear (restore path)")
    args = parser.parse_args(argv)
    settings = get_settings()
    try:
        next_id = repair_sequence(
            make_session_factory(make_engine(settings.database_url)),
            floor=args.floor,
            lock_timeout_seconds=settings.ops_lock_timeout_seconds,
            statement_timeout_seconds=settings.ops_statement_timeout_seconds,
        )
    except RuntimeError as exc:
        print(f"repair_outbox_sequence: FAILED — {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — one-shot CLI: classify, print, exit nonzero
        message = binding.timeout_message(
            "repair_outbox_sequence", "ACCESS EXCLUSIVE on public.outbox", exc)
        if message:
            print(f"repair_outbox_sequence: FAILED — {message}", file=sys.stderr)
            return 1
        raise
    print(f"repair_outbox_sequence: OK — next allocation will be {next_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
