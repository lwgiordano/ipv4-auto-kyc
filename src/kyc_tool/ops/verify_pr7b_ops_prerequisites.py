"""Read-only ops PREREQUISITE preflight (PR 7b-core; re-audit `538e55e..42e1c7d` F12).

Run this BEFORE pausing service. The governed maintenance commands (restore/repair) enforce
sequence ownership only after their documented hard stop, so a wrong production credential is
otherwise discovered INSIDE the outage, at `ALTER SEQUENCE`. This command reports — WITHOUT taking
any maintenance lock (it needs no writer stop) — the current role, the exact owner of
`public.outbox_id_seq`, whether they match, the governed schema revision, and the configured
lock/statement timeout budgets, then exits 0 only when the current role OWNS the sequence (the
identity `ALTER SEQUENCE` requires). It NEVER writes.

    python -m kyc_tool.ops.verify_pr7b_ops_prerequisites --expect-revision 012
"""

import argparse
import sys

from sqlalchemy import text

from kyc_tool.config import get_settings
from kyc_tool.db.session import make_engine, make_session_factory
from kyc_tool.ops import binding


def check_prerequisites(
    session_factory, *, expect_revision: str, lock_timeout_seconds: int = 60,
    statement_timeout_seconds: int | None = None,
) -> tuple[int, dict]:
    """Return (exit_code, report). Read-only: `binding.bind(exact_revision=expect_revision)` pins
    the governed schema, requires EXACTLY the phase the operator is preflighting for (the restore
    path is `012`; a 013 DB is a different governed problem), validates sequence backing, and sets
    the timeout budgets — it takes NO maintenance lock. `require_sequence_owner=False` so this
    REPORTS ownership instead of refusing; we then read role/owner and roll back. The phase is part
    of the exit condition (a wrong schema refuses via BindingRefused — re-audit `42e1c7d..b39b82a`
    F5); exit 0 additionally requires the current role to OWN public.outbox_id_seq."""
    with session_factory() as s:
        binding.bind(s, lock_timeout_seconds=lock_timeout_seconds,
                     statement_timeout_seconds=statement_timeout_seconds,
                     exact_revision=expect_revision)
        role = s.execute(text("SELECT current_user")).scalar_one()
        owner = s.execute(text(
            "SELECT pg_catalog.pg_get_userbyid(c.relowner) FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname='public' AND c.relname='outbox_id_seq'"
        )).scalar_one()
        revision = s.execute(text("SELECT version_num FROM public.alembic_version")).scalar_one()
        s.rollback()  # read-only: hold nothing
    effective_statement = (
        lock_timeout_seconds + 300 if statement_timeout_seconds is None
        else statement_timeout_seconds
    )
    owns = role == owner
    report = {
        "current_role": role,
        "sequence_owner": owner,
        "owns_sequence": owns,
        "schema_revision": revision,
        "expected_revision": expect_revision,
        "lock_timeout_seconds": lock_timeout_seconds,
        "statement_timeout_seconds": effective_statement,
    }
    return (0 if owns else 1), report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expect-revision", required=True,
                        help="the schema phase this maintenance requires (the restore path is 012); "
                             "a different governed schema refuses, so OK cannot print on the wrong one")
    args = parser.parse_args(argv)
    settings = get_settings()
    try:
        code, report = check_prerequisites(
            make_session_factory(make_engine(settings.database_url)),
            expect_revision=args.expect_revision,
            lock_timeout_seconds=settings.ops_lock_timeout_seconds,
            statement_timeout_seconds=settings.ops_statement_timeout_seconds,
        )
    except binding.BindingRefused as exc:  # a schema/identity problem IS a preflight failure
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — a blocked catalog read is a governed timeout, not a crash
        message = binding.timeout_message(
            "verify_pr7b_ops_prerequisites", "a catalog read", exc)
        if message:
            print(message, file=sys.stderr)
            return 1
        raise
    for key, value in report.items():
        print(f"{key}: {value}")
    if code == 0:
        print("verify_pr7b_ops_prerequisites: OK — current role owns public.outbox_id_seq")
    else:
        print(
            f"verify_pr7b_ops_prerequisites: NOT READY — {binding.SEQUENCE_OWNER_SENTINEL}: current "
            f"role {report['current_role']!r} does not OWN public.outbox_id_seq (owner is "
            f"{report['sequence_owner']!r}). Run maintenance as the owning role (see RUNBOOK).",
            file=sys.stderr,
        )
    return code


if __name__ == "__main__":
    sys.exit(main())
