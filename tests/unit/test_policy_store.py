import pytest
from sqlalchemy import text

from kyc_tool.policy_store import repo as store
from tests.integration._bundle_helpers import bundle_x, raw_x


def test_encode_decode_roundtrip_non_ascii():
    raw = {"scoring_rubric.json": "café·任意".encode()}   # valid UTF-8, non-ASCII
    assert store._decode_files(store._encode_files(raw)) == raw

def test_decode_rejects_bad_base64():
    with pytest.raises(store.BundleCorrupt):
        store._decode_files({"scoring_rubric.json": "%%%not-base64%%%"})

def test_store_then_load_roundtrip(session_factory, clean_db):
    with session_factory() as s:
        h = store.store_bundle(s, raw_x())
        s.commit()
    with session_factory() as s:
        b = store.load_bundle(s, h)
    assert b is not None and b.bundle_hash == h == bundle_x().bundle_hash
    assert b.policy_dir is None

def test_store_insert_path_verifies_and_returns_hash(session_factory, clean_db):
    with session_factory() as s:
        assert store.store_bundle(s, raw_x()) == bundle_x().bundle_hash   # read-back ran

def test_store_insert_path_readback_catches_corrupt_row(session_factory, clean_db, monkeypatch):
    # Adversarial: force the INSERT-path read-back to fail on a FRESH insert (empty
    # table → INSERT, not conflict). Encode every file as corrupt base64 so the
    # persisted row cannot reconstruct; store_bundle must raise — proving the
    # read-back runs on the INSERT path too (§8.2), not only the conflict path.
    monkeypatch.setattr(store, "_encode_files", lambda raw: {n: "%%%" for n in raw})
    with session_factory() as s, pytest.raises(store.BundleCorrupt):
        store.store_bundle(s, raw_x())

def test_load_unknown_returns_none(session_factory, clean_db):
    with session_factory() as s:
        assert store.load_bundle(s, "0"*64) is None

def test_store_conflict_path_raises_on_tampered_row(session_factory, engine, clean_db):
    with session_factory() as s:
        h = store.store_bundle(s, raw_x())
        s.commit()
    with engine.begin() as c:                              # tamper one file's base64
        c.execute(text("UPDATE policy_bundles SET files_json = "
                       "jsonb_set(files_json, '{scoring_rubric.json}', '\"%%%\"') "
                       "WHERE bundle_hash=:h"), {"h": h})
    with session_factory() as s, pytest.raises(store.BundleCorrupt):
        store.store_bundle(s, raw_x())                    # conflict → read-back verifies

@pytest.mark.parametrize("mutate", [
    "files_json = files_json - 'scoring_rubric.json'",                    # missing key
    "files_json = jsonb_set(files_json, '{extra_file.json}', '\"eA==\"')",  # extra key
], ids=["missing", "extra"])
def test_load_raises_on_wrong_key_set(session_factory, engine, clean_db, mutate):
    with session_factory() as s:
        h = store.store_bundle(s, raw_x())
        s.commit()
    with engine.begin() as c:
        c.execute(text(f"UPDATE policy_bundles SET {mutate} WHERE bundle_hash=:h"), {"h": h})
    with session_factory() as s, pytest.raises(store.BundleCorrupt):
        store.load_bundle(s, h)


def test_load_raises_on_scalar_files_json(session_factory, engine, clean_db):
    # a non-object files_json (JSON scalar via direct tamper) — set() over it would
    # raise a raw TypeError; the module invariant normalizes it to BundleCorrupt.
    with session_factory() as s:
        h = store.store_bundle(s, raw_x())
        s.commit()
    with engine.begin() as c:
        c.execute(text("UPDATE policy_bundles SET files_json = '42'::jsonb WHERE bundle_hash=:h"), {"h": h})
    with session_factory() as s, pytest.raises(store.BundleCorrupt):
        store.load_bundle(s, h)

def test_store_idempotent(session_factory, clean_db):
    with session_factory() as s:
        store.store_bundle(s, raw_x())
        store.store_bundle(s, raw_x())
        s.commit()
        assert s.execute(text("SELECT count(*) FROM policy_bundles")).scalar_one() == 1

def test_activate_epoch_idempotent(session_factory, clean_db):
    with session_factory() as s:
        h = store.store_bundle(s, raw_x())
        s.commit()
    for _ in range(2):
        with session_factory() as s:
            store.activate_epoch(s, expect_bundle_hash=h, expect_engine="eng-1")
            s.commit()
    with session_factory() as s:
        assert store.read_epoch(s) == (h, "eng-1")

def test_activate_epoch_valid_y_after_x_fails_on_readback(session_factory, engine, clean_db, tmp_path):
    from kyc_tool.policy.loader import read_policy_files
    from tests.integration._bundle_helpers import bundle_x, make_bundle_y
    T = bundle_x().rubric.check_types[0]                    # a real rubric type
    with session_factory() as s:
        hx = store.store_bundle(s, raw_x())
        ydir, by = make_bundle_y(tmp_path, check_type=T, points=999, category="x")
        hy = store.store_bundle(s, read_policy_files(ydir))
        s.commit()
    with session_factory() as s:                          # X activated first
        store.activate_epoch(s, expect_bundle_hash=hx, expect_engine="eng-1")
        s.commit()
    with session_factory() as s, pytest.raises(store.BundleCorrupt):  # valid Y loses on read-back
        store.activate_epoch(s, expect_bundle_hash=hy, expect_engine="eng-1")
