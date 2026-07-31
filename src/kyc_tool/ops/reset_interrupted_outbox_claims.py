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
from kyc_tool.ops import binding


def reset_claims(session_factory, *, lock_timeout_seconds: int = 60,
                 statement_timeout_seconds: int | None = None) -> int:
    """Clear every complete outbox claim tuple; return the count. Refuses pre-013; on a
    surviving tuple it rolls back BEFORE commit and raises (atomic — no partial reset)."""
    with session_factory() as session:
        binding.bind(session, lock_timeout_seconds=lock_timeout_seconds,
                     statement_timeout_seconds=statement_timeout_seconds)
        has_col = session.execute(
            text(
                "SELECT 1 FROM information_schema.columns WHERE table_schema='public' "
                "AND table_name='outbox' AND column_name='claim_token'"
            )
        ).first()
        if has_col is None:
            raise RuntimeError(
                "reset_interrupted_outbox_claims refuses pre-013 schema: no claim_token column "
                "(an interrupted pre-013 claim is encoded only in next_attempt_at)"
            )
        # Held through commit — the read-back below is diagnosis; THIS is the guarantee.
        session.execute(text("LOCK TABLE public.outbox IN ACCESS EXCLUSIVE MODE"))
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
