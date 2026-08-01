"""Governed schema-012 restore of a pruned decision callback (PR 7b-core step 0.5/0.6).

The pre-window diagnostic refuses on a missing callback (`BLOCKED_NO_AUTHORITATIVE_MAPPING`),
and the documented remediation used to be pasted SQL — an unexecutable `:param` predicate, a
sequence step that was unreachable before cutover, and a repair that only knew current
`max(id)` and could restart the sequence BELOW the id about to be restored (Codex re-audit
`f495de8` F1: max=10, missing id=100 → repair says OK at next=11, the restored 100 collides
later). This command IS the executable form, run inside a PRE-WINDOW MAINTENANCE STOP (every
writer stopped and attested — it takes `ACCESS EXCLUSIVE` on `public.outbox`):

- input is a STRICT, VERSIONED backup-evidence JSON file (`schema_version` plus every schema-012
  outbox column, the decision id, and the body digest computed ON THE BACKUP ROW) parsed through a
  typed model that refuses unknown fields, naive timestamps, a non-object body, and malformed
  shapes with a stable payload-free refusal — never a traceback (re-audit `538e55e..42e1c7d` F3) —
  plus `--expect-original-id`, which must equal the file's id (double-entry against the wrong row);
- **only a DELIVERED, terminal decision callback may be restored** (re-audit `8377440` F1):
  retention prunes only `delivered` rows, so that is the only legitimate gap. A restored
  `delivered` row is TERMINAL — the claim SQL selects `status='pending'`, so it is never
  claimed, signed, or sent. That closes the "manufacture a sendable callback from a
  self-consistent file" exploit at the root: even a wholly attacker-controlled body cannot be
  transmitted, because a delivered row is not sendable;
- the body must be a decision-callback body whose embedded `case_id`/`run_id` agree with the
  evidence tuple AND the linked automatic decision — a body that names a different case/run is
  refused;
- `--expect-manifest-digest` (REQUIRED, dry-run AND apply) is a mandatory INTEGRITY check, NOT
  signature verification (re-audit `42e1c7d..b39b82a` F4): the tool recomputes sha256 over the
  ENTIRE evidence file and refuses unless it matches this value, before any parsing or DB work — so
  altering the body OR any lifecycle field is rejected, and a file cannot self-certify by carrying
  its own digest. What the tool does NOT do is verify a cryptographic signature or vouch for the
  digest itself; the digest's AUTHENTICITY is the operator's responsibility — source it OUT OF BAND
  from a trusted/signed backup manifest (the RUNBOOK documents the capture step). A pinned-key
  signed-manifest verification is a deliberate future option, not yet built;
- DRY-RUN by default: it runs the EXACT apply path inside a SAVEPOINT and rolls back, so a
  value that would fail on apply (a malformed timestamp, a lifecycle CHECK) fails dry-run too
  — dry-run and apply are the same code, never divergent previews (re-audit `8377440` F2);
- `--apply` commits: the exact original row AND the sequence floored to
  `GREATEST(max(id), original_id) + 1` land in ONE transaction, with fail-closed read-backs of
  both the acceptance predicate (exactly one row) and the sequence tuple before commit.

    python -m kyc_tool.ops.restore_pr7b_core_callback --evidence <file.json> \
        --expect-original-id <id> --expect-manifest-digest <sha256> [--apply]

After a green apply, rerun `verify_pr7b_core_backfill`; it is the gate that reopens cutover.

TRUST BOUNDARY: kind/status/lifecycle, the body's case/run tuple, the body digest, and the
decision linkage are ALL machine-refused. The remaining lifecycle values (attempts,
next_attempt_at, last_error, timestamps) are ATTESTED inputs from the backup — a delivered row
is terminal so they never affect delivery, and nothing in the target database can contradict a
falsified backup value. That is why the evidence must come from the authoritative backup by the
documented capture query, and why the MANDATORY `--expect-manifest-digest` (sha256 of the whole
evidence file) binds every attested field for INTEGRITY. Its limit is explicit (F4): the tool
checks the file matches the digest, but does not verify the digest is genuinely signed — that
authenticity is operator-attested (source the digest from a trusted/signed backup manifest, out of
band). Machine-verified authenticity (a pinned-key detached signature) is a deliberate future
option, not built here.
"""

import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from kyc_tool.config import get_settings
from kyc_tool.db.session import make_engine, make_session_factory, uow
from kyc_tool.ops import binding

_SCHEMA_VERSION = "pr7b-core.restore.v1"


def _reject_nonfinite(value: str):
    """json.loads parse_constant hook: PostgreSQL JSONB rejects NaN/Infinity/-Infinity, but
    Python's json.loads accepts them by default — which would slip past validation and only fault
    at the JSONB cast (re-audit `42e1c7d..b39b82a` F3). Reject them at parse time instead."""
    raise ValueError(f"non-finite JSON constant not allowed: {value}")


# An evidence file is a SINGLE schema-012 outbox row plus a digest; a legitimate one is a few KiB.
# The ceiling bounds memory and, with the recursion translation below, the parse cost of a hostile
# file (re-audit `b39b82a..b53daf4` F9).
_MAX_EVIDENCE_BYTES = 1 << 20  # 1 MiB


def _loads_strict(raw) -> object:
    """json.loads that refuses NaN/Infinity/-Infinity AND deeply nested JSON — for every untrusted
    evidence parse. Deep nesting otherwise raises RecursionError (NOT a ValueError), which escapes
    the parse-boundary handlers as an uncaught traceback in both dry-run and apply (re-audit
    `b39b82a..b53daf4` F9). Python's recursion limit is the effective nesting ceiling; the overflow
    is translated into the same ValueError every caller already refuses on, payload-free."""
    try:
        return json.loads(raw, parse_constant=_reject_nonfinite)
    except RecursionError as exc:
        raise ValueError("JSON nesting exceeds the allowed depth") from exc


class _Evidence(BaseModel):
    """Strict, versioned backup-evidence schema (re-audit `538e55e..42e1c7d` F3).

    Unknown fields are refused, ids are non-blank, the digest is 64-hex, timestamps are
    timezone-aware ISO-8601, `payload_json` is a JSON OBJECT string, kind/status are closed to the
    only restorable shape (a delivered decision callback), and attempts is non-negative. So a
    malformed shape is a stable, payload-free refusal — never a traceback (e.g. the old
    `payload_json="[]"` → `AttributeError`) and never a mid-INSERT DB error whose text leaks the
    payload. Every schema-012 field is REQUIRED (nullables carry an explicit null): omitting the
    retry/audit fields is exactly what makes a restore not "exact".
    """

    # strict=True so mistyped fields are REFUSED, not coerced: `"1"`->1 and `true`->1 no longer
    # pass an int field (re-audit `42e1c7d..b39b82a` F3). extra=forbid rejects unknown fields.
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[_SCHEMA_VERSION]
    decision_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    original_outbox_id: int = Field(ge=1)
    original_kind: Literal["decision_callback"]
    original_status: Literal["delivered"]
    body_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    original_delivered_at: str  # delivered ⇒ non-null (Literal status forces it)
    original_next_attempt_at: str | None
    original_last_error: str | None
    original_attempts: int = Field(ge=0)
    original_created_at: str
    payload_json: str = Field(min_length=1)

    @field_validator("original_delivered_at", "original_next_attempt_at", "original_created_at")
    @classmethod
    def _tz_aware(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            parsed = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("not an ISO-8601 timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware (carry a UTC offset)")
        return v

    @field_validator("decision_id", "run_id", "case_id")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("identifier must not be blank/whitespace")
        return v

    @field_validator("payload_json")
    @classmethod
    def _body_is_json_object(cls, v: str) -> str:
        try:
            parsed = _loads_strict(v)  # rejects NaN/Infinity, which JSONB would reject at the cast
        except ValueError as exc:
            raise ValueError("payload_json is not valid JSON (or carries a non-finite constant)") from exc
        if not isinstance(parsed, dict):
            raise ValueError("payload_json must be a JSON object, not a list/scalar")
        return v


# Columns the acceptance predicate binds: every evidence field mapping to a public.outbox column
# (the model minus schema_version and payload_json, which the SQL renders separately).
_ACCEPTANCE_COLUMNS = (
    "decision_id", "run_id", "case_id", "original_outbox_id", "original_kind", "body_digest",
    "original_status", "original_delivered_at", "original_attempts", "original_next_attempt_at",
    "original_last_error", "original_created_at",
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


class _Refused(RuntimeError):
    """A governed refusal with a stable, payload-free operator message."""


def _load_evidence(path: Path, *, expect_manifest_digest: str) -> dict:
    """Verify the MANDATORY out-of-band manifest digest over the whole file, then strictly parse and
    validate it. Every file/JSON/shape error becomes a payload-free `_Refused` — never a traceback,
    never an echo of the evidence values (re-audit `538e55e..42e1c7d` F2/F3)."""
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise _Refused(f"evidence file could not be read ({type(exc).__name__})") from exc
    if len(raw_bytes) > _MAX_EVIDENCE_BYTES:
        raise _Refused(
            f"evidence file exceeds the {_MAX_EVIDENCE_BYTES}-byte ceiling — an evidence file is a "
            "single outbox row; a larger file is not a backup row and is refused before parsing"
        )
    # F2: the manifest anchor is checked FIRST and binds the ENTIRE file byte-for-byte. The operator
    # obtains sha256(evidence.json) from a trusted/signed backup manifest and passes it here; a file
    # that self-certifies cannot pass, and altering ANY field (body OR any lifecycle field) changes
    # this digest and refuses before any parsing or DB work. F4/F13: this is an INTEGRITY digest, not
    # a cryptographic signature — the tool does not verify the digest is genuine (that authenticity
    # is operator-attested), so the refusal names only integrity, never "authenticated".
    actual = hashlib.sha256(raw_bytes).hexdigest()
    if actual != expect_manifest_digest.strip().lower():
        raise _Refused(
            "the evidence file's sha256 does not match --expect-manifest-digest (the operator-"
            "supplied integrity digest) — the file is not the one that digest was taken over, or a "
            "field was altered"
        )
    try:
        raw = _loads_strict(raw_bytes)  # rejects NaN/Infinity that JSONB would fault on later
    except ValueError as exc:
        raise _Refused(f"evidence file is not valid JSON ({type(exc).__name__})") from exc
    if not isinstance(raw, dict):
        raise _Refused("evidence file must be a JSON object")
    try:
        model = _Evidence.model_validate(raw)
    except ValidationError as exc:
        # payload-free: report only field locations + error TYPES, never the offending input values
        problems = sorted(
            {f"{'.'.join(str(p) for p in e['loc'])}: {e['type']}" for e in exc.errors()}
        )
        raise _Refused(f"evidence file failed schema validation: {problems}") from exc
    data = model.model_dump()
    # F1: the body (a JSON object, model-checked) must name the SAME case/run as the evidence tuple.
    body = _loads_strict(data["payload_json"])
    if body.get("case_id") != data["case_id"] or body.get("run_id") != data["run_id"]:
        raise _Refused("the callback body's case_id/run_id do not match the evidence tuple")
    return data


def restore_callback(
    session_factory, evidence: dict, *, expect_original_id: int, apply: bool,
    lock_timeout_seconds: int = 60, statement_timeout_seconds: int | None = None,
) -> dict:
    """Validate — and with apply=True perform — the governed restore. Returns a report dict.

    Dry-run and apply run the IDENTICAL body inside a SAVEPOINT; dry-run rolls it back after the
    read-backs pass. Any mismatch raises `_Refused` and rolls back row AND sequence together.
    """
    if evidence["original_outbox_id"] != expect_original_id:
        raise _Refused(
            "--expect-original-id does not match the evidence file's original_outbox_id — "
            "double-entry failed"
        )
    with uow(session_factory) as session:
        # bind(exact_revision="012") owns the schema/phase gate: it reads the FULL version set,
        # requires cardinality one, and refuses a multi-head or off-012 schema as BindingRefused —
        # no separate .scalar_one() that would itself traceback on a multi-head state (F4).
        binding.bind(session, lock_timeout_seconds=lock_timeout_seconds,
                     statement_timeout_seconds=statement_timeout_seconds,
                     exact_revision="012", require_sequence_owner=True)
        session.execute(text("LOCK TABLE public.outbox IN ACCESS EXCLUSIVE MODE"))

        oid = evidence["original_outbox_id"]
        if session.execute(text("SELECT 1 FROM public.outbox WHERE id=:i"), {"i": oid}).first():
            raise _Refused(
                "an outbox row with the evidence's original_outbox_id already exists — "
                "nothing to restore"
            )
        decision_ok = session.execute(
            text(
                "SELECT 1 FROM public.decisions d WHERE d.id=:d AND d.case_id=:c "
                "AND d.run_id IS NOT DISTINCT FROM :r AND d.manual=false"
            ),
            {"d": evidence["decision_id"], "c": evidence["case_id"], "r": evidence["run_id"]},
        ).first()
        if not decision_ok:
            # F10: name the failed invariant/fields only — NEVER the candidate values (an attacker
            # controls decision_id/case_id/run_id in the file; echoing them injects the operator log).
            raise _Refused(
                "no automatic (manual=false) decision matches the evidence tuple "
                "(decision_id/case_id/run_id) — verify the evidence against the target database"
            )
        try:
            candidate_digest = session.execute(
                text("SELECT encode(sha256(convert_to(CAST(:p AS jsonb)::text,'UTF8')),'hex')"),
                {"p": evidence["payload_json"]},
            ).scalar_one()
        except SQLAlchemyError as exc:
            # The JSONB cast is the one pre-savepoint query that touches untrusted body text. A
            # value that slipped the strict parser must still refuse SANITIZED here, never leak the
            # payload through a traceback (re-audit `42e1c7d..b39b82a` F3).
            raise _Refused(
                f"the database rejected the evidence body ({type(exc).__name__}); nothing changed"
            ) from exc
        if candidate_digest != evidence["body_digest"]:
            raise _Refused(
                "the candidate body does not digest to the evidence's body_digest — the payload is "
                "not the backed-up body (recompute sha256 over the backup row's payload_json)"
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
                {k: evidence[k] for k in _ACCEPTANCE_COLUMNS},
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
    parser.add_argument("--expect-manifest-digest", required=True,
                        help="sha256 of the evidence file, taken from your signed/detached backup "
                             "manifest (out-of-band; REQUIRED — the file cannot self-certify)")
    parser.add_argument("--apply", action="store_true",
                        help="perform the restore; without it, validate the exact path and roll back")
    args = parser.parse_args(argv)
    settings = get_settings()
    try:
        evidence = _load_evidence(args.evidence, expect_manifest_digest=args.expect_manifest_digest)
        report = restore_callback(
            make_session_factory(make_engine(settings.database_url)),
            evidence,
            expect_original_id=args.expect_original_id,
            apply=args.apply,
            lock_timeout_seconds=settings.ops_lock_timeout_seconds,
            statement_timeout_seconds=settings.ops_statement_timeout_seconds,
        )
    except (_Refused, binding.BindingRefused) as exc:  # governed refusal: stable, no traceback
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
