"""Staged raw-evidence sweeper (re-audit `750630c..ca85355` F8, crash-window arm).

The pipeline STAGES raw adapter bytes in the object store BEFORE the fenced recording
transaction (the job row lock is never held over I/O). Every in-process failure deletes the
staged object, but a process kill between `put()` and commit leaves bytes with NO database
reference — untracked raw upstream evidence that retention can never find. This sweeper is the
durable cleanup: it enumerates ONLY the pipeline's `adapter-raw/` staging namespace (platform
uploads live elsewhere and are never candidates), keeps every ref adopted by `adapter_results`,
and deletes unadopted objects older than a minimum age (default 24h — far beyond any live
staging window, which is bounded by the job lease).

    python -m kyc_tool.ops.sweep_staged_evidence [--min-age-hours N] [--apply]

Dry-run by default: prints the JSON plan; `--apply` performs the deletions. Runs under the
RETENTION process role (the one sanctioned remover of evidence)."""

import argparse
import json
import sys
from datetime import UTC, datetime

from sqlalchemy import text

from kyc_tool.config import ProcessRole, get_settings, validate_process_role
from kyc_tool.db.session import make_engine, make_session_factory
from kyc_tool.storage.object_store import make_object_store

STAGED_PREFIX = "adapter-raw/"
_MIN_AGE_FLOOR_HOURS = 1  # never sweep inside any plausible live staging window


def _staged_key(ref: str) -> str | None:
    """The store-relative key of a staged ref, or None when the ref is outside the staging
    namespace (fs://<key> vs s3://<bucket>/<key>)."""
    scheme, _, rest = ref.partition("://")
    key = rest.partition("/")[2] if scheme == "s3" else rest
    return key if key.startswith(STAGED_PREFIX) else None


def sweep_staged_evidence(
    session_factory, store, *, min_age_seconds: float, apply: bool, now: datetime | None = None
) -> dict:
    """One sweep pass. Authority order is load-bearing: the DB reference set is read FIRST, so an
    object staged-and-adopted during the sweep is either in the set (kept) or younger than
    min_age (skipped) — never deleted."""
    now = now or datetime.now(UTC)
    with session_factory() as session:
        referenced = {
            ref
            for (ref,) in session.execute(
                text("SELECT raw_ref FROM adapter_results WHERE raw_ref IS NOT NULL")
            )
        }
    kept_referenced = 0
    kept_fresh = 0
    swept: list[str] = []
    for ref, modified in store.iter_refs():
        if _staged_key(ref) is None:
            continue  # outside the staging namespace: never a candidate
        if ref in referenced:
            kept_referenced += 1
            continue
        if (now - modified).total_seconds() < min_age_seconds:
            kept_fresh += 1
            continue
        swept.append(ref)
        if apply:
            store.delete(ref)
    return {
        "namespace": STAGED_PREFIX,
        "referenced_kept": kept_referenced,
        "fresh_kept": kept_fresh,
        "swept": swept,
        "applied": apply,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min-age-hours", type=int, default=24)
    parser.add_argument("--apply", action="store_true", help="delete (default: dry-run plan)")
    args = parser.parse_args(argv)
    if args.min_age_hours < _MIN_AGE_FLOOR_HOURS:
        print(f"refusing --min-age-hours below {_MIN_AGE_FLOOR_HOURS}", file=sys.stderr)
        return 2
    settings = get_settings()
    validate_process_role(settings, ProcessRole.RETENTION)
    session_factory = make_session_factory(make_engine(settings.database_url))
    store = make_object_store(
        settings.object_store, fs_root=settings.object_store_root, s3_bucket=settings.s3_bucket
    )
    result = sweep_staged_evidence(
        session_factory, store, min_age_seconds=args.min_age_hours * 3600, apply=args.apply
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover — thin CLI shim
    sys.exit(main())
