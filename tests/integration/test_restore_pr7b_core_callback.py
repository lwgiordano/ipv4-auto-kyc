"""The governed schema-012 restore CLI (Codex re-audit `f495de8` F1, hardened `538e55e..42e1c7d`
F2/F3).

The pasted-SQL restore procedure was circular: the diagnostic stayed red until the restore,
the sequence repair was documented as reachable only after cutover step 2, and the repair knew
only current max(id) — so max=10 / missing id=100 "repaired" to next=11 and the restored row
collided later. These tests drive the REAL subprocess entry point end-to-end on schema 012, and
pin the authenticity anchor (`--expect-manifest-digest`, mandatory) and the strict versioned
evidence schema (unknown fields, naive timestamps, non-object bodies, malformed shapes all refuse
cleanly, never a traceback, never an echo of the payload).
"""

import hashlib
import json
import os
import subprocess
import sys

import pytest
from alembic import command
from sqlalchemy import create_engine, text

from kyc_tool.ops.restore_pr7b_core_callback import _SCHEMA_VERSION
from tests.integration.test_migrations import _config, _fresh_db, _seed_legacy_callback

pytestmark = pytest.mark.postgres


def _manifest(path):
    """sha256 of the evidence FILE — the out-of-band anchor an operator obtains from a signed
    backup manifest. Recomputed from whatever bytes are on disk at call time (an authentic run)."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _cli(url, *args):
    return subprocess.run(
        [sys.executable, "-m", "kyc_tool.ops.restore_pr7b_core_callback", *args],
        env={**os.environ, "KYC_DATABASE_URL": url},
        capture_output=True, text=True, timeout=60,
    )


def _restore(url, path, *extra, manifest=None):
    """Invoke restore with the MANDATORY --expect-manifest-digest, defaulting to the digest of the
    file as it is on disk now (an authentic invocation). Pass `manifest=` to simulate a STALE
    signed digest against a tampered file."""
    digest = _manifest(path) if manifest is None else manifest
    return _cli(url, "--evidence", str(path), "--expect-manifest-digest", digest, *extra)


def _verify(url):
    return subprocess.run(
        [sys.executable, "-m", "kyc_tool.ops.verify_pr7b_core_backfill"],
        env={**os.environ, "KYC_DATABASE_URL": url},
        capture_output=True, text=True, timeout=60,
    )


def _seed_and_prune(url, tmp_path, *, gap_to: int | None = None):
    """One case with a delivered, previously-retried, nested-unicode callback; capture the
    evidence tuple exactly as the runbook prescribes (from the row = the backup), then prune it.
    With `gap_to`, the pruned row's id is bumped ABOVE current max first — the F1 collision
    shape (original id above the sequence high-water)."""
    engine = create_engine(url)
    payload = json.dumps(
        {"case_id": "c1", "run_id": "rA", "decision": "approve",
         "checks": [{"source": "reviewer:José Ω"}], "gates": {"ok": True}},
        ensure_ascii=False,
    )
    with engine.begin() as conn:
        oid = _seed_legacy_callback(conn, case_id="c1", run_id="rA", decision_id="dA",
                                    ev_seq=1, status="delivered", payload=payload)
        conn.execute(text(  # a previously-retried shape; next_attempt_at is NOT NULL at 012
            "UPDATE outbox SET attempts=3, last_error='upstream 503', "
            "next_attempt_at = now() - interval '2 days' WHERE id=:i"), {"i": oid})
        if gap_to is not None:
            conn.execute(text("UPDATE outbox SET id=:n WHERE id=:i"), {"i": oid, "n": gap_to})
            oid = gap_to
        row = conn.execute(text(
            "SELECT id, kind, run_id, case_id, status, delivered_at, attempts, next_attempt_at, "
            "last_error, created_at, payload_json::text AS body, "
            "encode(sha256(convert_to(payload_json::text,'UTF8')),'hex') AS digest "
            "FROM outbox WHERE id=:i"), {"i": oid}).one()
        evidence = {
            "schema_version": _SCHEMA_VERSION,
            "decision_id": "dA", "run_id": row.run_id, "case_id": row.case_id,
            "original_outbox_id": row.id, "original_kind": row.kind,
            "body_digest": row.digest, "original_status": row.status,
            "original_delivered_at": row.delivered_at.isoformat(),
            "original_attempts": row.attempts,
            "original_next_attempt_at": (
                row.next_attempt_at.isoformat() if row.next_attempt_at else None
            ),
            "original_last_error": row.last_error,
            "original_created_at": row.created_at.isoformat(),
            "payload_json": row.body,
        }
        conn.execute(text("DELETE FROM outbox WHERE id=:i"), {"i": oid})
    engine.dispose()
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence, ensure_ascii=False))
    return evidence, path


def test_restore_above_high_water_floors_the_sequence_and_greens_the_diagnostic(pg, tmp_path):
    """The audit's exact trigger: current max(id)=10-ish, authoritative missing id=100. The
    restore must insert id 100, restart the sequence PAST it (101) in the same transaction,
    pass both read-backs, and turn the diagnostic green — no separate circular repair step."""
    url = _fresh_db(pg, "kyc_restore_gap")
    command.upgrade(_config(url), "012")
    evidence, path = _seed_and_prune(url, tmp_path, gap_to=100)
    assert _verify(url).returncode != 0  # red: the mapping is missing

    dry = _restore(url, path, "--expect-original-id", "100")
    assert dry.returncode == 0 and "DRY-RUN OK" in dry.stdout, dry.stdout + dry.stderr
    engine = create_engine(url)
    with engine.connect() as conn:  # dry run wrote NOTHING
        assert conn.execute(text("SELECT count(*) FROM outbox WHERE id=100")).scalar_one() == 0

    applied = _restore(url, path, "--expect-original-id", "100", "--apply")
    assert applied.returncode == 0 and "RESTORED" in applied.stdout, applied.stdout + applied.stderr
    with engine.connect() as conn:
        stored = conn.execute(text(
            "SELECT payload_json, attempts, last_error FROM outbox WHERE id=100")).one()
        assert stored.payload_json["checks"][0]["source"] == "reviewer:José Ω"
        assert (stored.attempts, stored.last_error) == (3, "upstream 503")
        nxt = conn.execute(text(
            "SELECT last_value + (CASE WHEN is_called THEN 1 ELSE 0 END) FROM outbox_id_seq"
        )).scalar_one()
        assert nxt == 101, f"sequence must clear the restored id, next={nxt}"
    with engine.begin() as conn:  # and the NEXT allocation really does not collide
        new_id = conn.execute(text(
            "INSERT INTO outbox (kind, case_id, payload_json, status) "
            "VALUES ('poc_email','c1','{}'::jsonb,'pending') RETURNING id")).scalar_one()
    assert new_id == 101
    engine.dispose()
    assert _verify(url).returncode == 0  # the gate that reopens cutover is green


# Machine-refused components: id (double entry), kind/status/lifecycle, the body's case/run
# tuple, the body digest, and the decision linkage. Each here rides an AUTHENTIC file (manifest
# recomputed after the mutation), so what is proven is the SEMANTIC guard, independent of the
# authenticity anchor. The remaining lifecycle VALUES are attested backup inputs — but a delivered
# row is TERMINAL, so a falsified value there can never make the row sendable.
@pytest.mark.parametrize("mutation", [
    {"original_outbox_id": 999},          # fails the double-entry against --expect
    {"body_digest": "0" * 64},            # payload no longer proves the backed-up body
    {"run_id": "r-nope"},                 # body tuple + decision linkage disagree
    {"decision_id": "d-nope"},            # no matching automatic decision
    {"case_id": "c-nope"},                # body tuple disagrees
    {"original_kind": "poc_email"},       # only decision_callback is restorable (schema Literal)
    {"original_status": "pending"},       # F1: only DELIVERED (terminal) may be restored (Literal)
    {"original_delivered_at": None},      # a delivered row must carry the timestamp
])
def test_one_changed_evidence_component_refuses_and_writes_nothing(pg, tmp_path, mutation):
    url = _fresh_db(pg, f"kyc_restore_mut_{abs(hash(str(mutation))) % 10_000}")
    command.upgrade(_config(url), "012")
    evidence, path = _seed_and_prune(url, tmp_path)
    bad = {**evidence, **mutation}
    path.write_text(json.dumps(bad, ensure_ascii=False))

    proc = _restore(url, path, "--expect-original-id",
                    str(evidence["original_outbox_id"]), "--apply")
    assert proc.returncode != 0 and "REFUSED" in proc.stderr
    engine = create_engine(url)
    with engine.connect() as conn:  # atomic: no row, sequence untouched by the failed attempt
        assert conn.execute(text("SELECT count(*) FROM outbox")).scalar_one() == 0
    engine.dispose()


def test_expect_id_mismatch_wrong_revision_and_existing_row_all_refuse(pg, tmp_path):
    url = _fresh_db(pg, "kyc_restore_refusals")
    command.upgrade(_config(url), "012")
    evidence, path = _seed_and_prune(url, tmp_path)
    oid = evidence["original_outbox_id"]

    wrong_expect = _restore(url, path, "--expect-original-id", str(oid + 7), "--apply")
    assert wrong_expect.returncode != 0 and "double-entry" in wrong_expect.stderr

    ok = _restore(url, path, "--expect-original-id", str(oid), "--apply")
    assert ok.returncode == 0
    again = _restore(url, path, "--expect-original-id", str(oid), "--apply")
    assert again.returncode != 0 and "already exists" in again.stderr  # idempotent refusal

    command.upgrade(_config(url), "013")  # post-013 gap is a DIFFERENT governed problem
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM outbox WHERE id=:i"), {"i": oid})
    engine.dispose()
    post = _restore(url, path, "--expect-original-id", str(oid), "--apply")
    # bind(exact_revision="012") now owns the phase gate: a governed schema refusal, not a traceback
    assert post.returncode != 0 and "012" in post.stderr and "Traceback" not in post.stderr


def test_injected_sendable_callback_is_refused_at_the_root(pg, tmp_path):
    """Re-audit `8377440` F1: the exploit was manufacturing a SENDABLE (pending) callback with an
    attacker body from a self-consistent file. The versioned schema now closes `status` to a Literal
    `delivered` — a pending restore is refused at validation (schema layer), so no injected body can
    ever be claimed, signed, or sent, even before the manifest anchor is considered."""
    url = _fresh_db(pg, "kyc_restore_inject")
    command.upgrade(_config(url), "012")
    evidence, path = _seed_and_prune(url, tmp_path)
    # attacker rewrites body + recomputes its digest + flips to sendable — internally consistent
    engine = create_engine(url)
    poisoned_body = json.dumps({"case_id": "c1", "run_id": "rA", "decision": "approve",
                                "injected": "attacker-controlled"})
    with engine.connect() as conn:
        digest = conn.execute(text(
            "SELECT encode(sha256(convert_to(CAST(:p AS jsonb)::text,'UTF8')),'hex')"),
            {"p": poisoned_body}).scalar_one()
    engine.dispose()
    bad = {**evidence, "payload_json": poisoned_body, "body_digest": digest,
           "original_status": "pending", "original_delivered_at": None}
    path.write_text(json.dumps(bad, ensure_ascii=False))

    proc = _restore(url, path, "--expect-original-id",
                    str(evidence["original_outbox_id"]), "--apply")
    assert proc.returncode != 0 and "REFUSED" in proc.stderr
    assert "original_status" in proc.stderr  # the closed-vocab field that rejected it
    engine = create_engine(url)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM outbox")).scalar_one() == 0
    engine.dispose()


def test_dry_run_and_apply_agree_on_a_value_that_only_fails_deep(pg, tmp_path):
    """Re-audit `8377440` F2 + `538e55e..42e1c7d` F3: a malformed timestamp used to pass DRY-RUN
    and blow up under --apply with a traceback leaking payload_json. The versioned schema now
    refuses it pre-DB in BOTH modes identically — no traceback, no payload in the output."""
    url = _fresh_db(pg, "kyc_restore_parity")
    command.upgrade(_config(url), "012")
    evidence, path = _seed_and_prune(url, tmp_path)
    bad = {**evidence, "original_created_at": "not-a-timestamp"}
    path.write_text(json.dumps(bad, ensure_ascii=False))
    oid = str(evidence["original_outbox_id"])

    dry = _restore(url, path, "--expect-original-id", oid)
    applied = _restore(url, path, "--expect-original-id", oid, "--apply")
    for proc in (dry, applied):
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "REFUSED" in proc.stderr
        assert "DRY-RUN OK" not in proc.stdout, "a value that fails apply must fail dry-run too"
        assert "Traceback" not in proc.stderr, "a refusal must be stable, not a crash"
        assert "attacker" not in proc.stderr and "José" not in proc.stderr  # never echo the body
    engine = create_engine(url)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM outbox")).scalar_one() == 0
    engine.dispose()


def test_apply_requires_the_manifest_digest(pg, tmp_path):
    """F2: `--expect-manifest-digest` is mandatory — the file cannot self-certify. Omitting it is
    an argparse error, so a restore can never run without the out-of-band anchor."""
    url = _fresh_db(pg, "kyc_restore_needs_manifest")
    command.upgrade(_config(url), "012")
    evidence, path = _seed_and_prune(url, tmp_path)
    proc = _cli(url, "--evidence", str(path),
                "--expect-original-id", str(evidence["original_outbox_id"]), "--apply")
    assert proc.returncode != 0
    assert "expect-manifest-digest" in (proc.stderr + proc.stdout)


def test_stale_manifest_refuses_a_tampered_file(pg, tmp_path):
    """F2 (Codex's exact repro): the file is NOT self-authenticating. An attacker who rewrites the
    delivered body and recomputes the file's OWN body_digest cannot forge the out-of-band signed
    manifest; passing the original (signed) digest against the tampered file refuses before any DB
    work, so a falsified backup cannot be committed as immutable delivered evidence."""
    url = _fresh_db(pg, "kyc_restore_stale_manifest")
    command.upgrade(_config(url), "012")
    evidence, path = _seed_and_prune(url, tmp_path)
    signed = _manifest(path)  # the operator's out-of-band, signed digest of the AUTHENTIC file

    engine = create_engine(url)
    tampered_body = json.dumps({"case_id": "c1", "run_id": "rA", "decision": "reject",
                                "checks": [{"source": "attacker"}]})
    with engine.connect() as conn:
        new_digest = conn.execute(text(
            "SELECT encode(sha256(convert_to(CAST(:p AS jsonb)::text,'UTF8')),'hex')"),
            {"p": tampered_body}).scalar_one()
    engine.dispose()
    # internally consistent (body_digest matches the new body), but the signed manifest is unchanged
    path.write_text(json.dumps({**evidence, "payload_json": tampered_body, "body_digest": new_digest}))

    proc = _restore(url, path, "--expect-original-id", str(evidence["original_outbox_id"]),
                    "--apply", manifest=signed)
    assert proc.returncode != 0 and "manifest" in proc.stderr.lower()
    engine = create_engine(url)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM outbox")).scalar_one() == 0
    engine.dispose()


def _corrupt_payload_list(ev):
    return json.dumps({**ev, "payload_json": "[]"})            # body is a list, not an object


def _corrupt_top_level_array(ev):
    return json.dumps([ev])                                    # the record itself is not an object


def _corrupt_naive_timestamp(ev):
    return json.dumps({**ev, "original_created_at": "2026-01-01T00:00:00"})  # no UTC offset


def _corrupt_extra_field(ev):
    return json.dumps({**ev, "surprise": "unexpected"})        # extra="forbid"


def _corrupt_missing_version(ev):
    d = {**ev}
    d.pop("schema_version")
    return json.dumps(d)                                       # versioned schema is required


def _corrupt_digest_shape(ev):
    return json.dumps({**ev, "body_digest": "not-64-hex"})     # digest pattern


def _corrupt_not_json(ev):
    return "{ this is not valid json"


@pytest.mark.parametrize("corruptor", [
    _corrupt_payload_list, _corrupt_top_level_array, _corrupt_naive_timestamp,
    _corrupt_extra_field, _corrupt_missing_version, _corrupt_digest_shape, _corrupt_not_json,
])
def test_malformed_evidence_refuses_cleanly_in_both_modes(pg, tmp_path, corruptor):
    """F3: every malformed shape (non-object body, top-level array, naive timestamp, unknown field,
    missing version, bad digest, non-JSON) is a stable payload-free refusal in BOTH dry-run and
    apply — never a traceback (the old `payload_json="[]"` → AttributeError), never a DB change."""
    url = _fresh_db(pg, f"kyc_restore_bad_{corruptor.__name__[9:]}")
    command.upgrade(_config(url), "012")
    evidence, path = _seed_and_prune(url, tmp_path)
    path.write_text(corruptor(evidence))
    oid = str(evidence["original_outbox_id"])

    for extra in ([], ["--apply"]):  # both refuse identically, before any DB work
        proc = _restore(url, path, "--expect-original-id", oid, *extra)
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "REFUSED" in proc.stderr, proc.stderr
        assert "Traceback" not in proc.stderr, proc.stderr
        assert "José" not in proc.stderr  # never echo the evidence body
    engine = create_engine(url)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM outbox")).scalar_one() == 0
    engine.dispose()
