"""Pre-window backfill diagnostic (PR 7b-core §Rollout step 0). Run BEFORE any outage, with
the retention schedule suspended AND every active retention task terminated (orchestrator-
attested zero-running) — see docs/RUNBOOK.md. Schema-012-compatible: it runs the SAME shared
parity matrix as migration 013 (kyc_tool.migration_contracts.v013_backfill), imports NO 013-only ORM.

It begins its transaction with `LOCK TABLE outbox IN SHARE MODE` BEFORE any SELECT (defense in
depth: the SHARE lock waits for any in-flight DELETE's ROW EXCLUSIVE to resolve and blocks a
new outbox delete/write from invalidating the snapshot until the diagnostic commits), then runs
the read-only parity checks at READ COMMITTED. On any violation it prints actionable decision/
run/outbox ids and exits nonzero; a missing mapping additionally prints the exact
BLOCKED_NO_AUTHORITATIVE_MAPPING sentinel. Recovery is restore-from-authoritative-backup or
remain on 012 — activation (`024`) is downstream and cannot repair this. It NEVER writes.

    python -m kyc_tool.ops.verify_pr7b_core_backfill
"""

import sys

from sqlalchemy import text

from kyc_tool.config import get_settings
from kyc_tool.db.session import make_engine, make_session_factory
from kyc_tool.migration_contracts.v013_backfill import BLOCKED_SENTINEL, MISSING_CALLBACK, run_parity
from kyc_tool.ops import binding, shape


def verify_backfill(session_factory, *, lock_timeout_seconds: int = 60,
                    statement_timeout_seconds: int | None = None) -> tuple[int, list[str]]:
    """Return (exit_code, offending_ids). Takes the SHARE lock first, runs the shared parity
    matrix, and NEVER writes (rolls back before returning)."""
    with session_factory() as s:
        # Governed-schema binding runs BEFORE the lock: its catalog reads establish that the
        # `public.outbox` we are about to lock is the real object (a smuggled search_path
        # otherwise redirects everything below), and touch no outbox DATA — the SHARE lock
        # still precedes every data SELECT, which is the property the retention-race test pins.
        # exact 012: this is the PRE-window diagnostic and its parity matrix is schema-012 shaped.
        # On a 013+ DB it would otherwise lock, run, and print "schema-012 parity matrix clean" —
        # certifying a phase it never checked (re-audit `8377440` F12).
        binding.bind(s, lock_timeout_seconds=lock_timeout_seconds,
                     statement_timeout_seconds=statement_timeout_seconds, exact_revision="012",
                     shape_contract=shape.PR7B_CORE_PREWINDOW)
        s.execute(text("LOCK TABLE public.outbox IN SHARE MODE"))  # BEFORE any data SELECT
        violations = run_parity(s)
        s.rollback()  # read-only: never write, never hold the lock past the check
    if not violations:
        return 0, []
    offenders: list[str] = []
    names = {n for n, _ in violations}
    if MISSING_CALLBACK in names:
        missing = next(pairs for n, pairs in violations if n == MISSING_CALLBACK)
        print(f"{BLOCKED_SENTINEL} missing decision_callback (decision, run): {missing}")
        offenders += [str(x) for pair in missing for x in pair if x is not None]
    for name, pairs in violations:
        if name != MISSING_CALLBACK:
            print(f"parity violation {name}: {pairs}")
        offenders += [str(x) for pair in pairs for x in pair if x is not None]
    return 1, offenders


def main() -> int:
    settings = get_settings()
    try:
        code, _ = verify_backfill(
            make_session_factory(make_engine(settings.database_url)),
            lock_timeout_seconds=settings.ops_lock_timeout_seconds,
            statement_timeout_seconds=settings.ops_statement_timeout_seconds,
        )
    except binding.BindingRefused as exc:  # governed schema/phase refusal: stable, no traceback
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — one-shot CLI: classify, print, exit nonzero
        message = binding.timeout_message(
            "verify_pr7b_core_backfill", "SHARE on public.outbox", exc
        )
        if message:
            print(message, file=sys.stderr)
            return 1
        raise
    if code == 0:
        print("verify_pr7b_core_backfill: OK (schema-012 parity matrix clean)")
    return code


if __name__ == "__main__":
    sys.exit(main())
