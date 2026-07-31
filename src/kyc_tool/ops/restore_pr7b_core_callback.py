"""Governed schema-012 restore of a pruned decision callback (PR 7b-core step 0.5/0.6).

The pre-window diagnostic refuses on a missing callback (`BLOCKED_NO_AUTHORITATIVE_MAPPING`),
and the documented remediation used to be pasted SQL — an unexecutable `:param` predicate, a
sequence step that was unreachable before cutover, and a repair that only knew current
`max(id)` and could restart the sequence BELOW the id about to be restored (Codex re-audit
`f495de8` F1: max=10, missing id=100 → repair says OK at next=11, the restored 100 collides
later). This command IS the executable form, run inside a PRE-WINDOW MAINTENANCE STOP (every
writer stopped and attested — it takes `ACCESS EXCLUSIVE` on `public.outbox`):

- input is a structured backup-evidence JSON file (every schema-012 outbox column, the
  decision id, and the body digest computed ON THE BACKUP ROW) plus `--expect-original-id`,
  which must equal the file's id — double-entry against restoring the wrong row;
- it binds to the governed schema first (`ops/binding.py`), requires `alembic_version='012'`
  exactly, and refuses if the target id already exists or the decision row does not match;
- DRY-RUN by default: validates everything (including the candidate body's jsonb-normalized
  digest) and prints the plan; `--apply` inserts the EXACT original row, restarts
  `public.outbox_id_seq` at `GREATEST(max(id), original_id) + 1` in the SAME transaction, and
  fail-closed read-backs BOTH the runbook's positive acceptance predicate (exactly one row,
  every recorded component matched) and the sequence tuple before committing. Any failure
  rolls the whole restore back — row and sequence together.

    python -m kyc_tool.ops.restore_pr7b_core_callback --evidence <file.json> \
        --expect-original-id <id> [--apply]

After a green apply, rerun `verify_pr7b_core_backfill`; it is the gate that reopens cutover.

TRUST BOUNDARY: the id, the body, and the decision linkage have in-database oracles (the
double-entry flag, the digest, the decisions row) and are machine-refused on mismatch. The
lifecycle fields (status/attempts/next_attempt_at/last_error/timestamps) are ATTESTED inputs —
they come from the operator's backup and nothing inside this database can contradict a
falsified value. That is precisely why the evidence must be captured from the authoritative
backup by the documented query and why the file, not ad-hoc SQL, is the only sanctioned path.
"""

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import text

from kyc_tool.config import get_settings
from kyc_tool.db.session import make_engine, make_session_factory, uow
from kyc_tool.ops import binding

# Every schema-012 evidence field is REQUIRED (nullables carry explicit null). Omitting the
# retry/audit fields is what makes "exact" false — a previously retried callback restored with
# a reset attempts/next_attempt_at/last_error is not the row that was pruned.
_REQUIRED_FIELDS = (
    "decision_id", "run_id", "case_id", "original_outbox_id", "original_kind", "body_digest",
    "original_status", "original_delivered_at", "original_attempts", "original_next_attempt_at",
    "original_last_error", "original_created_at", "payload_json",
)

# The runbook's POSITIVE acceptance predicate, verbatim contract: MUST return exactly one row.
# `IS NOT DISTINCT FROM` so NULL matches NULL; digest over the stored row's jsonb::text — the
# same rendering the backup digest was computed over, so key normalization cancels out.
_ACCEPTANCE_SQL = text(
    "SELECT 1 AS accepted FROM public.outbox o JOIN public.decisions d ON d.id = :decision_id "
    "WHERE o.id = :original_outbox_id AND o.kind = :original_kind "
    "AND o.case_id = :case_id AND o.run_id IS NOT DISTINCT FROM :run_id "
    "AND d.case_id = o.case_id AND d.run_id IS NOT DISTINCT FROM o.run_id "
    "AND encode(sha256(convert_to(o.payload_json::text,'UTF8')),'hex') = :body_digest "
    "AND o.status = :original_status "
    "AND o.delivered_at IS NOT DISTINCT FROM CAST(:original_delivered_at AS timestamptz) "
    "AND o.attempts = :original_attempts "
    "AND o.next_attempt_at IS NOT DISTINCT FROM CAST(:original_next_attempt_at AS timestamptz) "
    "AND o.last_error IS NOT DISTINCT FROM :original_last_error "
    "AND o.created_at IS NOT DISTINCT FROM CAST(:original_created_at AS timestamptz)"
)


def _load_evidence(path: Path) -> dict:
    raw = json.loads(path.read_text())
    missing = [f for f in _REQUIRED_FIELDS if f not in raw]
    if missing:
        raise RuntimeError(f"refusing: evidence file is missing required field(s): {missing}")
    if not isinstance(raw["payload_json"], str):
        raise RuntimeError(
            "refusing: payload_json must be the exact `payload_json::text` STRING from the "
            "backup row — a re-encoded object cannot prove byte identity"
        )
    if not isinstance(raw["original_outbox_id"], int) or raw["original_outbox_id"] < 1:
        raise RuntimeError(f"refusing: original_outbox_id={raw['original_outbox_id']!r}")
    return raw


def restore_callback(
    session_factory, evidence: dict, *, expect_original_id: int, apply: bool,
    lock_timeout_seconds: int = 60,
) -> dict:
    """Validate (and with apply=True perform) the governed restore. Returns a report dict.

    Raises RuntimeError on ANY mismatch; with apply=True the transaction — row insert AND
    sequence restart — rolls back whole.
    """
    if evidence["original_outbox_id"] != expect_original_id:
        raise RuntimeError(
            f"refusing: --expect-original-id={expect_original_id} does not match the evidence "
            f"file's original_outbox_id={evidence['original_outbox_id']} — double-entry failed"
        )
    with uow(session_factory) as session:
        binding.bind(session, lock_timeout_seconds=lock_timeout_seconds)
        version = session.execute(text("SELECT version_num FROM public.alembic_version")).scalar_one()
        if version != "012":
            raise RuntimeError(
                f"refusing: this restore is the schema-012 pre-cutover contract; "
                f"alembic_version is {version!r}. A post-013 gap is a different governed problem."
            )
        # the pre-window maintenance stop made writers zero; this makes it a guarantee
        session.execute(text("LOCK TABLE public.outbox IN ACCESS EXCLUSIVE MODE"))

        oid = evidence["original_outbox_id"]
        if session.execute(
            text("SELECT 1 FROM public.outbox WHERE id=:i"), {"i": oid}
        ).first():
            raise RuntimeError(f"refusing: public.outbox id {oid} already exists — nothing to restore")
        decision_ok = session.execute(
            text(
                "SELECT 1 FROM public.decisions d WHERE d.id=:d AND d.case_id=:c "
                "AND d.run_id IS NOT DISTINCT FROM :r AND d.manual=false"
            ),
            {"d": evidence["decision_id"], "c": evidence["case_id"], "r": evidence["run_id"]},
        ).first()
        if not decision_ok:
            raise RuntimeError(
                "refusing: no automatic decision matches the evidence tuple "
                f"(decision={evidence['decision_id']!r}, case={evidence['case_id']!r}, "
                f"run={evidence['run_id']!r})"
            )
        # candidate-body digest, jsonb-normalized EXACTLY like the acceptance predicate,
        # without inserting anything — a dry run proves the whole restore would be accepted
        candidate_digest = session.execute(
            text("SELECT encode(sha256(convert_to(CAST(:p AS jsonb)::text,'UTF8')),'hex')"),
            {"p": evidence["payload_json"]},
        ).scalar_one()
        if candidate_digest != evidence["body_digest"]:
            raise RuntimeError(
                f"refusing: candidate body digests to {candidate_digest}, evidence says "
                f"{evidence['body_digest']} — the payload is not the backed-up body"
            )
        next_id = session.execute(
            text("SELECT GREATEST(COALESCE(max(id), 0), :oid) + 1 FROM public.outbox"),
            {"oid": oid},
        ).scalar_one()
        report = {"original_outbox_id": oid, "sequence_next": next_id, "applied": False}
        if not apply:
            return report  # uow commit of a read-only txn; nothing was written

        session.execute(
            text(
                "INSERT INTO public.outbox (id, kind, case_id, run_id, payload_json, status, "
                "delivered_at, attempts, next_attempt_at, last_error, created_at) "
                "VALUES (:i, :k, :c, :r, CAST(:p AS jsonb), :st, "
                "CAST(:da AS timestamptz), :at, CAST(:na AS timestamptz), :le, "
                "CAST(:ca AS timestamptz))"
            ),
            {
                "i": oid, "k": evidence["original_kind"], "c": evidence["case_id"],
                "r": evidence["run_id"], "p": evidence["payload_json"],
                "st": evidence["original_status"], "da": evidence["original_delivered_at"],
                "at": evidence["original_attempts"], "na": evidence["original_next_attempt_at"],
                "le": evidence["original_last_error"], "ca": evidence["original_created_at"],
            },
        )
        # the sequence is floored past the restored id IN THE SAME TRANSACTION — a crash or a
        # failed read-back below rolls back row and sequence together, never one without the other
        session.execute(text(f"ALTER SEQUENCE public.outbox_id_seq RESTART WITH {int(next_id)}"))

        accepted = session.execute(_ACCEPTANCE_SQL, {
            k: evidence[k]
            for k in _REQUIRED_FIELDS
            if k not in ("payload_json",)
        }).fetchall()
        if accepted != [(1,)]:
            raise RuntimeError(
                f"restore FAILED acceptance read-back (got {accepted!r}, want exactly one row) — "
                "transaction rolled back, nothing restored"
            )
        last_value, is_called = session.execute(
            text("SELECT last_value, is_called FROM public.outbox_id_seq")
        ).one()
        if last_value != next_id or is_called:
            raise RuntimeError(
                f"restore FAILED sequence read-back: expected ({next_id}, False), got "
                f"({last_value}, {is_called}) — transaction rolled back, nothing restored"
            )
        report["applied"] = True
        return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True, type=Path,
                        help="backup-evidence JSON (every schema-012 outbox column + digest)")
    parser.add_argument("--expect-original-id", required=True, type=int,
                        help="must equal the evidence file's original_outbox_id (double entry)")
    parser.add_argument("--apply", action="store_true",
                        help="perform the restore; without it, validate and print the plan only")
    args = parser.parse_args(argv)
    settings = get_settings()
    try:
        evidence = _load_evidence(args.evidence)
        report = restore_callback(
            make_session_factory(make_engine(settings.database_url)),
            evidence,
            expect_original_id=args.expect_original_id,
            apply=args.apply,
            lock_timeout_seconds=settings.ops_lock_timeout_seconds,
        )
    except RuntimeError as exc:
        print(f"restore_pr7b_core_callback: REFUSED — {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — one-shot CLI: classify, print, exit nonzero
        if binding.is_lock_timeout(exc):
            print(
                "restore_pr7b_core_callback: REFUSED — "
                + binding.lock_timeout_message(
                    "restore_pr7b_core_callback", "ACCESS EXCLUSIVE on public.outbox"
                ),
                file=sys.stderr,
            )
            return 1
        raise
    verb = "RESTORED" if report["applied"] else "DRY-RUN OK (nothing written)"
    print(
        f"restore_pr7b_core_callback: {verb} — id {report['original_outbox_id']}, "
        f"sequence next allocation {report['sequence_next']}. "
        f"Rerun verify_pr7b_core_backfill before proceeding."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
