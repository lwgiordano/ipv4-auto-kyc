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


def verify_backfill(session_factory) -> tuple[int, list[str]]:
    """Return (exit_code, offending_ids). Takes the SHARE lock first, runs the shared parity
    matrix, and NEVER writes (rolls back before returning)."""
    with session_factory() as s:
        s.execute(text("LOCK TABLE outbox IN SHARE MODE"))  # BEFORE any SELECT
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
    code, _ = verify_backfill(make_session_factory(make_engine(get_settings().database_url)))
    if code == 0:
        print("verify_pr7b_core_backfill: OK (schema-012 parity matrix clean)")
    return code


if __name__ == "__main__":
    sys.exit(main())
