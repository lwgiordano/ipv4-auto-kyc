"""Framed whole-tree engine drift guard (PR 6, item 7A).

Pins a sha256 over every `src/kyc_tool/**/*.py` file, framed as
`relpath \\x00 len(bytes) \\x00 bytes` in sorted relative-path order. Framing
binds path + byte-length boundaries into the hash, so a byte-identical
cross-file move, a rename, or an empty-file add all change the digest — an
unframed concatenation of file bytes would not catch any of those (see
test_framing_catches_cross_file_move_rename_empty). The whole source tree is
in the closure (not a curated file list), so ANY change under src/kyc_tool —
including outside domain/ — trips the guard (see
test_non_domain_edit_trips_guard); no file can be silently omitted.

On failure: decide whether scoring/decision semantics changed. If so, bump
`ENGINE_BUILD_ID` (domain/engine.py). Either way, re-pin
EXPECTED_ENGINE_SOURCE_HASH to the new digest in the SAME commit.
"""

import hashlib
import shutil
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "kyc_tool"
EXPECTED_ENGINE_SOURCE_HASH = "43179d701c0832a2278259693204f90876426389f1c5309aa30a9507204deb78"


def _framed_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        rel = p.relative_to(root).as_posix().encode()
        data = p.read_bytes()
        h.update(rel + b"\x00" + str(len(data)).encode() + b"\x00" + data)
    return h.hexdigest()


def test_engine_source_hash_pinned():
    assert _framed_hash(SRC) == EXPECTED_ENGINE_SOURCE_HASH, (
        "src/kyc_tool changed — if scoring/decision semantics changed bump "
        "ENGINE_BUILD_ID; re-pin EXPECTED_ENGINE_SOURCE_HASH in the SAME commit."
    )


def test_framing_catches_cross_file_move_rename_empty(tmp_path):
    # A real equal-byte transfer: move "MOVE\n" from a.py to b.py. The UNFRAMED
    # concat of sorted-path bytes is byte-IDENTICAL both ways; framing
    # (path\0len\0bytes) makes the two trees differ — proving boundary safety.
    d1 = tmp_path/"d1"
    d1.mkdir()
    (d1/"a.py").write_bytes(b"AAAA\nMOVE\n")
    (d1/"b.py").write_bytes(b"BBBB\n")
    d2 = tmp_path/"d2"
    d2.mkdir()
    (d2/"a.py").write_bytes(b"AAAA\n")
    (d2/"b.py").write_bytes(b"MOVE\nBBBB\n")
    assert b"AAAA\nMOVE\n"+b"BBBB\n" == b"AAAA\n"+b"MOVE\nBBBB\n"   # unframed concat identical
    assert _framed_hash(d1) != _framed_hash(d2)                     # framed differs
    d3 = tmp_path/"d3"
    d3.mkdir()
    (d3/"a.py").write_bytes(b"AAAA\n")
    d4 = tmp_path/"d4"
    d4.mkdir()
    (d4/"a.py").write_bytes(b"AAAA\n")
    (d4/"z.py").write_bytes(b"")
    assert _framed_hash(d3) != _framed_hash(d4)                     # empty-file add
    d5 = tmp_path/"d5"
    d5.mkdir()
    (d5/"a.py").write_bytes(b"AAAA\n")
    d6 = tmp_path/"d6"
    d6.mkdir()
    (d6/"renamed.py").write_bytes(b"AAAA\n")
    assert _framed_hash(d5) != _framed_hash(d6)                     # rename (path change)


def test_non_domain_edit_trips_guard(tmp_path):
    # The whole src tree is in the closure, so a semantic edit OUTSIDE domain/
    # (broker_gate.py) changes the hash — no curated list can omit it.
    base = tmp_path/"src"
    shutil.copytree(SRC, base)
    h0 = _framed_hash(base)
    bg = base/"orchestration"/"broker_gate.py"
    bg.write_bytes(bg.read_bytes() + b"\n# semantic change\n")
    assert _framed_hash(base) != h0
