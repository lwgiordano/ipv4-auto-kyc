"""Post-013 outbox claim reset (PR 7b-core §Rollout). Run ONCE after ALL outbox publishers
are confirmed stopped and none have restarted, for post-013 future stops and the post-013
rollback path ONLY. It refuses pre-013 schema (the claim columns don't exist yet), clears only
rows with the COMPLETE claim tuple, PRESERVES next_attempt_at (an interrupted old claim simply
waits until its already-recorded due time), and read-back-asserts zero claim tuples — rolling
back atomically (never a partial reset) if any remain. It MUST NOT run while a publisher is live.

The whole reset runs under `LOCK TABLE public.outbox IN ACCESS EXCLUSIVE MODE`, held from
before the UPDATE through commit. The read-back alone could not carry the guarantee: a claimant
committing in the read-back→commit window left the command reporting "0 claim tuples remain"
while one existed (Codex re-audit `f495de8` F3). Under the exclusive lock a straggler publisher
blocks until the reset commits — and the operator contract is that none is running at all.
Execution is bound to the governed schema first (`ops/binding.py`): a URL smuggling a
`search_path` cannot point this at a shadow `outbox`, and a conflicting lock refuses with
`OPS_COMMAND_LOCK_TIMEOUT` after the bound instead of hanging the window.

    python -m kyc_tool.ops.reset_interrupted_outbox_claims
"""

import sys

from sqlalchemy import text

from kyc_tool.config import get_settings
from kyc_tool.db.session import make_engine, make_session_factory
from kyc_tool.ops import binding, shape


def reset_claims(session_factory, *, lock_timeout_seconds: int = 60,
                 statement_timeout_seconds: int | None = None) -> int:
    """Clear every complete outbox claim tuple; return the count. Refuses pre-013; on a
    surviving tuple it rolls back BEFORE commit and raises (atomic — no partial reset)."""
    with session_factory() as session:
        # min_revision='013' makes the pre-013 refusal a GOVERNED BindingRefused (stable sentinel,
        # nonzero, no traceback — re-audit `42e1c7d..b39b82a` F6) instead of the old bare
        # RuntimeError. The claim columns this command clears exist only from 013, and bind()
        # refuses anything below it before taking any lock or reading a row.
        # min_revision gates the migration LINEAGE; the shape contract gates the physical SHAPE (a
        # hand-stamped 013+ over a drifted schema passes lineage but not the shape). RESET_OUTBOX_CLAIMS
        # asserts `status` exists (NOT NULL) and the three claim columns are NULLABLE — so a
        # `claim_token` flipped to NOT NULL is refused here, not discovered as a NotNullViolation
        # mid-clear under ACCESS EXCLUSIVE (re-audit `8aba2df..2cee937` R3-F7).
        binding.bind(session, lock_timeout_seconds=lock_timeout_seconds,
                     statement_timeout_seconds=statement_timeout_seconds, min_revision="013",
                     shape_contract=shape.RESET_OUTBOX_CLAIMS)
        # Held through commit — the read-back below is diagnosis; THIS is the guarantee.
        session.execute(text("LOCK TABLE public.outbox IN ACCESS EXCLUSIVE MODE"))
        # Re-check the SAME contract under the lock: concurrent DDL between bind() and the lock cannot
        # slip a shape change past the mutation (re-audit R3-F7 — evaluate under the mutator's lock).
        under_lock = shape.shape_mismatches(session, shape.RESET_OUTBOX_CLAIMS)
        if under_lock:
            session.rollback()
            raise binding.BindingRefused(
                f"{binding.SCHEMA_REFUSED_SENTINEL}: outbox shape changed under the maintenance "
                f"lock: {'; '.join(under_lock)}"
            )
        count = session.execute(
            text(
                "UPDATE public.outbox SET claim_token=NULL, claim_lease_expires_at=NULL, "
                "claimed_by=NULL WHERE status='pending' AND claim_token IS NOT NULL "
                "AND claim_lease_expires_at IS NOT NULL AND claimed_by IS NOT NULL"
            )
        ).rowcount
        remaining = session.execute(
            text(
                "SELECT count(*) FROM public.outbox WHERE claim_token IS NOT NULL "
                "OR claim_lease_expires_at IS NOT NULL OR claimed_by IS NOT NULL"
            )
        ).scalar_one()
        if remaining != 0:  # roll back the whole reset BEFORE committing — never a partial clear
            session.rollback()
            raise RuntimeError(f"{remaining} outbox claim tuple(s) remain — publishers not stopped?")
        session.commit()
    return count


def main() -> int:
    settings = get_settings()
    session_factory = make_session_factory(make_engine(settings.database_url))
    try:
        n = reset_claims(
            session_factory,
            lock_timeout_seconds=settings.ops_lock_timeout_seconds,
            statement_timeout_seconds=settings.ops_statement_timeout_seconds,
        )
    except binding.BindingRefused as exc:  # governed schema/phase refusal: stable, no traceback
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — one-shot CLI: classify, print, exit nonzero
        message = binding.timeout_message(
            "reset_interrupted_outbox_claims", "ACCESS EXCLUSIVE on public.outbox", exc)
        if message:
            print(message, file=sys.stderr)
            return 1
        raise
    print(f"reset {n} interrupted outbox claim(s); 0 claim tuples remain")
    return 0


if __name__ == "__main__":
    sys.exit(main())
