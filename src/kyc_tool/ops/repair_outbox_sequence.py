"""Drained outbox-sequence repair (PR 7b-core RUNBOOK step 0.6d escape hatch).

Run ONLY inside the drained maintenance window, after every writer is hard-stopped and attested
at zero. It exists because the step-0 precondition can legitimately fail when a restored id is
NOT below the sequence high-water (a restore from a divergent lineage rather than a prune).

Two things make this a shipped CLI rather than pasted SQL. `ALTER SEQUENCE ... RESTART WITH`
takes a LITERAL, not an expression, so the obvious one-liner is invalid SQL discovered mid-outage.
And the check must be fail-closed: a psql script that SELECTs a boolean still exits 0 when that
boolean is false, so a bad repair reports success. Here the exit status IS the result.

It never calls nextval() to probe: consuming an id and setval-ing it back is itself a write to
the object under repair. The read-back reads last_value/is_called from the sequence relation.

    python -m kyc_tool.ops.repair_outbox_sequence
"""

import sys

from sqlalchemy import text

from kyc_tool.config import get_settings
from kyc_tool.db.session import make_engine, make_session_factory, uow

_SEQUENCE = "outbox_id_seq"


def repair_sequence(session_factory) -> int:
    """Restart the outbox id sequence at max(id)+1. Returns the value the NEXT allocation takes.

    Raises RuntimeError (rolling the transaction back) if the post-restart read-back is not
    exactly (next_id, is_called=false) — the caller must treat that as a failed repair.
    """
    with uow(session_factory) as session:
        # The fence. Writers are already stopped by the runbook; this makes that a guarantee
        # rather than an assumption, and ALTER SEQUENCE (unlike setval) excludes concurrent
        # nextval for the duration of the transaction.
        session.execute(text("LOCK TABLE outbox IN ACCESS EXCLUSIVE MODE"))
        next_id = session.execute(text("SELECT COALESCE(max(id), 0) + 1 FROM outbox")).scalar_one()
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


def main() -> int:
    settings = get_settings()
    try:
        next_id = repair_sequence(make_session_factory(make_engine(settings.database_url)))
    except RuntimeError as exc:
        print(f"repair_outbox_sequence: FAILED — {exc}", file=sys.stderr)
        return 1
    print(f"repair_outbox_sequence: OK — next allocation will be {next_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
