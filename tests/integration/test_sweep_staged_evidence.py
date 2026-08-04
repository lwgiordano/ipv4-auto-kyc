"""Re-audit `750630c..ca85355` F8 (crash-window arm): the staged-evidence sweeper deletes
`adapter-raw/` objects never adopted by adapter_results — and ONLY those. Adopted refs, fresh
stages (possibly mid-transaction), and platform uploads outside the namespace all survive."""

import os
import time
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from kyc_tool.ops.sweep_staged_evidence import sweep_staged_evidence
from kyc_tool.storage.object_store import FsStore

pytestmark = pytest.mark.postgres


def _seed_result_with_ref(session_factory, ref):
    with session_factory() as s:
        s.execute(text("INSERT INTO cases (id) VALUES ('sw')"))
        s.execute(
            text(
                "INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
                "actor_json, payload_json, event_sequence) "
                "VALUES ('e-sw', 'sw', 'k-sw', 'h', 'x', '{}'::jsonb, '{}'::jsonb, 1)"
            )
        )
        s.execute(
            text(
                "INSERT INTO runs (id, case_id, triggering_event_id, state) "
                "VALUES ('r-sw', 'sw', 'e-sw', 'COMPLETE')"
            )
        )
        from kyc_tool.db.tables import AdapterResult

        s.add(
            AdapterResult(
                run_id="r-sw", adapter_id="gleif", status="ok", raw_ref=ref,
                normalized_json={}, input_hash="h1", latency_ms=1,
            )
        )
        s.commit()


def _age(store_root, key, seconds):
    past = time.time() - seconds
    os.utime(store_root / key, (past, past))


def test_sweeper_deletes_only_old_unadopted_staged_objects(session_factory, clean_db, tmp_path):
    store = FsStore(tmp_path / "ev")
    adopted = store.put("adapter-raw/sw/r-sw/gleif/aaa", b"adopted")
    orphan = store.put("adapter-raw/sw/r-sw/gleif/bbb", b"orphan")
    fresh = store.put("adapter-raw/sw/r-sw/gleif/ccc", b"fresh-orphan")
    upload = store.put("uploads/doc.json", b"platform upload")
    _age(store.root, "adapter-raw/sw/r-sw/gleif/aaa", 100_000)
    _age(store.root, "adapter-raw/sw/r-sw/gleif/bbb", 100_000)
    _age(store.root, "uploads/doc.json", 100_000)  # old but OUTSIDE the namespace
    _seed_result_with_ref(session_factory, adopted)

    plan = sweep_staged_evidence(
        session_factory, store, min_age_seconds=3600, apply=False,
        now=datetime.now(UTC),
    )
    assert plan["swept"] == [orphan] and plan["applied"] is False
    store.get(orphan)  # dry-run deleted nothing

    result = sweep_staged_evidence(session_factory, store, min_age_seconds=3600, apply=True)
    assert result["swept"] == [orphan]
    assert result["referenced_kept"] == 1 and result["fresh_kept"] == 1
    store.get(adopted)  # adopted evidence preserved
    store.get(fresh)  # fresh stage (possibly mid-transaction) preserved
    store.get(upload)  # platform upload never a candidate
    with pytest.raises(FileNotFoundError):
        store.get(orphan)  # the crash-window orphan is gone
