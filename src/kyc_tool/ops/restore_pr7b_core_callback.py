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
- **only a DELIVERED, terminal decision callback may be restored** (re-audit `8377440` F1):
  retention prunes only `delivered` rows, so that is the only legitimate gap. A restored
  `delivered` row is TERMINAL — the claim SQL selects `status='pending'`, so it is never
  claimed, signed, or sent. That closes the "manufacture a sendable callback from a
  self-consistent file" exploit at the root: even a wholly attacker-controlled body cannot be
  transmitted, because a delivered row is not sendable;
- the body must be a decision-callback body whose embedded `case_id`/`run_id` agree with the
  evidence tuple AND the linked automatic decision — a body that names a different case/run is
  refused;
- `--expect-body-digest` (optional) pins the digest OUT OF BAND: an operator who has the
  digest from a signed backup manifest supplies it, and the file's own `body_digest` must
  match it. The file is not self-authenticating — a signed/detached backup manifest is the
  operator prerequisite documented in RUNBOOK; this flag is where that authenticity enters;
- DRY-RUN by default: it runs the EXACT apply path inside a SAVEPOINT and rolls back, so a
  value that would fail on apply (a malformed timestamp, a lifecycle CHECK) fails dry-run too
  — dry-run and apply are the same code, never divergent previews (re-audit `8377440` F2);
- `--apply` commits: the exact original row AND the sequence floored to
  `GREATEST(max(id), original_id) + 1` land in ONE transaction, with fail-closed read-backs of
  both the acceptance predicate (exactly one row) and the sequence tuple before commit.

    python -m kyc_tool.ops.restore_pr7b_core_callback --evidence <file.json> \
        --expect-original-id <id> [--expect-body-digest <sha256>] [--apply]

After a green apply, rerun `verify_pr7b_core_backfill`; it is the gate that reopens cutover.

TRUST BOUNDARY: kind/status/lifecycle, the body's case/run tuple, the body digest, and the
decision linkage are ALL machine-refused. The remaining lifecycle values (attempts,
next_attempt_at, last_error, timestamps) are ATTESTED inputs from the backup — a delivered row
is terminal so they never affect delivery, and nothing in the target database can contradict a
falsified backup value. That is why the evidence must come from the authoritative backup by the
documented capture query, and why `--expect-body-digest` from a signed manifest is the
sanctioned authenticity anchor.
"""

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

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
# Fields that MUST be an ISO-8601 timestamp when non-null. Parsed in Python before any SQL, so a
# malformed value is a stable refusal, never a mid-INSERT DB error whose text leaks the payload.
_TIMESTAMP_FIELDS = ("original_delivered_at", "original_next_attempt_at", "original_created_at")

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


class _Refused(RuntimeError):
    """A governed refusal with a stable, payload-free operator message."""


def _load_evidence(path: Path, *, expect_body_digest: str | None) -> dict:
    raw = json.loads(path.read_text())
    missing = [f for f in _REQUIRED_FIELDS if f not in raw]
    if missing:
        raise _Refused(f"evidence file is missing required field(s): {missing}")
    if not isinstance(raw["payload_json"], str):
        raise _Refused(
            "payload_json must be the exact `payload_json::text` STRING from the backup row — "
            "a re-encoded object cannot prove byte identity"
        )
    if not isinstance(raw["original_outbox_id"], int) or raw["original_outbox_id"] < 1:
        raise _Refused(f"original_outbox_id must be a positive int, got {raw['original_outbox_id']!r}")
    # F1: only a delivered, terminal decision callback is a legitimate restore target.
    if raw["original_kind"] != "decision_callback":
        raise _Refused(f"only decision_callback rows are restorable, not {raw['original_kind']!r}")
    if raw["original_status"] != "delivered":
        raise _Refused(
            f"only a DELIVERED (terminal) callback may be restored, not {raw['original_status']!r} "
            "— retention prunes only delivered rows, and a non-terminal restore would be sendable"
        )
    if raw["original_delivered_at"] is None:
        raise _Refused("a delivered callback must carry a non-null original_delivered_at")
    # F2: normalize typed fields up front so a malformed value refuses cleanly, pre-SQL.
    for field in _TIMESTAMP_FIELDS:
        value = raw[field]
        if value is None:
            continue
        try:
            dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise _Refused(f"{field} is not an ISO-8601 timestamp: {value!r}") from exc
    if not isinstance(raw["original_attempts"], int) or raw["original_attempts"] < 0:
        raise _Refused(f"original_attempts must be a non-negative int, got {raw['original_attempts']!r}")
    # F1: the body must be a decision-callback body naming the SAME case/run as the evidence.
    try:
        body = json.loads(raw["payload_json"])
    except ValueError as exc:
        raise _Refused("payload_json is not valid JSON") from exc
    if body.get("case_id") != raw["case_id"] or body.get("run_id") != raw["run_id"]:
        raise _Refused(
            "the callback body's case_id/run_id do not match the evidence tuple "
            f"(body {body.get('case_id')!r}/{body.get('run_id')!r} vs evidence "
            f"{raw['case_id']!r}/{raw['run_id']!r})"
        )
    # F1: out-of-band authenticity anchor. When the operator supplies the digest from a signed
    # backup manifest, the file's own claimed digest must match it — the file cannot self-certify.
    if expect_body_digest is not None and raw["body_digest"] != expect_body_digest:
        raise _Refused(
            "the evidence file's body_digest does not match --expect-body-digest (the out-of-band "
            "manifest value) — the file is not the authenticated backup row"
        )
    return raw


def restore_callback(
    session_factory, evidence: dict, *, expect_original_id: int, apply: bool,
    lock_timeout_seconds: int = 60,
) -> dict:
    """Validate — and with apply=True perform — the governed restore. Returns a report dict.

    Dry-run and apply run the IDENTICAL body inside a SAVEPOINT; dry-run rolls it back after the
    read-backs pass. Any mismatch raises `_Refused` and rolls back row AND sequence together.
    """
    if evidence["original_outbox_id"] != expect_original_id:
        raise _Refused(
            f"--expect-original-id={expect_original_id} does not match the evidence file's "
            f"original_outbox_id={evidence['original_outbox_id']} — double-entry failed"
        )
    with uow(session_factory) as session:
        binding.bind(session, lock_timeout_seconds=lock_timeout_seconds,
                     require_sequence_owner=True)  # ALTER SEQUENCE needs ownership (F13)
        version = session.execute(text("SELECT version_num FROM public.alembic_version")).scalar_one()
        if version != "012":
            raise _Refused(
                "this restore is the schema-012 pre-cutover contract; alembic_version is "
                f"{version!r}. A post-013 gap is a different governed problem."
            )
        session.execute(text("LOCK TABLE public.outbox IN ACCESS EXCLUSIVE MODE"))

        oid = evidence["original_outbox_id"]
        if session.execute(text("SELECT 1 FROM public.outbox WHERE id=:i"), {"i": oid}).first():
            raise _Refused(f"public.outbox id {oid} already exists — nothing to restore")
        decision_ok = session.execute(
            text(
                "SELECT 1 FROM public.decisions d WHERE d.id=:d AND d.case_id=:c "
                "AND d.run_id IS NOT DISTINCT FROM :r AND d.manual=false"
            ),
            {"d": evidence["decision_id"], "c": evidence["case_id"], "r": evidence["run_id"]},
        ).first()
        if not decision_ok:
            raise _Refused(
                "no automatic decision matches the evidence tuple "
                f"(decision={evidence['decision_id']!r}, case={evidence['case_id']!r}, "
                f"run={evidence['run_id']!r})"
            )
        candidate_digest = session.execute(
            text("SELECT encode(sha256(convert_to(CAST(:p AS jsonb)::text,'UTF8')),'hex')"),
            {"p": evidence["payload_json"]},
        ).scalar_one()
        if candidate_digest != evidence["body_digest"]:
            raise _Refused(
                f"candidate body digests to {candidate_digest}, evidence claims "
                f"{evidence['body_digest']} — the payload is not the backed-up body"
            )
        next_id = session.execute(
            text("SELECT GREATEST(COALESCE(max(id), 0), :oid) + 1 FROM public.outbox"),
            {"oid": oid},
        ).scalar_one()

        # ONE code path for both modes (re-audit `8377440` F2): the exact INSERT + sequence
        # restart + both read-backs run inside a SAVEPOINT. Dry-run rolls the savepoint back
        # after proving acceptance; apply lets the outer uow commit it. A DB error here (a
        # lifecycle CHECK, a bad cast that slipped Python normalization) aborts BOTH modes
        # identically — no "DRY-RUN OK" that `--apply` would then reject.
        savepoint = session.begin_nested()
        try:
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
            session.execute(
                text(f"ALTER SEQUENCE public.outbox_id_seq RESTART WITH {int(next_id)}")
            )
            accepted = session.execute(
                _ACCEPTANCE_SQL,
                {k: evidence[k] for k in _REQUIRED_FIELDS if k != "payload_json"},
            ).fetchall()
            if accepted != [(1,)]:
                raise _Refused(
                    f"acceptance read-back returned {accepted!r}, want exactly one row — "
                    "nothing restored"
                )
            last_value, is_called = session.execute(
                text("SELECT last_value, is_called FROM public.outbox_id_seq")
            ).one()
            if last_value != next_id or is_called:
                raise _Refused(
                    f"sequence read-back expected ({next_id}, False), got "
                    f"({last_value}, {is_called}) — nothing restored"
                )
        except SQLAlchemyError as exc:
            savepoint.rollback()
            # sanitize: SQLAlchemy's str() embeds the SQL + bound params (incl. payload_json)
            raise _Refused(
                f"the database rejected the restore ({type(exc).__name__}); nothing changed"
            ) from exc
        if not apply:
            savepoint.rollback()  # dry-run: prove the exact path, keep the DB untouched
        # apply: fall through — the outer uow commits the savepoint's work
        return {"original_outbox_id": oid, "sequence_next": next_id, "applied": apply}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True, type=Path,
                        help="backup-evidence JSON (every schema-012 outbox column + digest)")
    parser.add_argument("--expect-original-id", required=True, type=int,
                        help="must equal the evidence file's original_outbox_id (double entry)")
    parser.add_argument("--expect-body-digest", default=None,
                        help="out-of-band body digest from a signed backup manifest (optional)")
    parser.add_argument("--apply", action="store_true",
                        help="perform the restore; without it, validate the exact path and roll back")
    args = parser.parse_args(argv)
    settings = get_settings()
    try:
        evidence = _load_evidence(args.evidence, expect_body_digest=args.expect_body_digest)
        report = restore_callback(
            make_session_factory(make_engine(settings.database_url)),
            evidence,
            expect_original_id=args.expect_original_id,
            apply=args.apply,
            lock_timeout_seconds=settings.ops_lock_timeout_seconds,
        )
    except _Refused as exc:
        print(f"restore_pr7b_core_callback: REFUSED — {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — one-shot CLI: classify, print, exit nonzero
        message = binding.timeout_message(
            "restore_pr7b_core_callback", "ACCESS EXCLUSIVE on public.outbox", exc)
        if message:
            print(f"restore_pr7b_core_callback: REFUSED — {message}", file=sys.stderr)
            return 1
        raise
    verb = "RESTORED" if report["applied"] else "DRY-RUN OK (exact path validated, nothing written)"
    print(
        f"restore_pr7b_core_callback: {verb} — id {report['original_outbox_id']}, "
        f"sequence next allocation {report['sequence_next']}. "
        f"Rerun verify_pr7b_core_backfill before proceeding."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
