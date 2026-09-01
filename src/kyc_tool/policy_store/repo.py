"""DB-backed policy-bundle store (PR 6). Bundles are stored as base64-encoded raw
files keyed by content hash; ``store_bundle`` read-back-verifies on both the
insert and ON CONFLICT paths, and ``load_bundle`` reconstructs + validates. The
module's invariant: EVERY form of persisted-row corruption — bad base64, wrong
key set, a non-object ``files_json``, or non-reconstructable content — raises
``BundleCorrupt`` so callers fail closed. ``activate_epoch`` is the single-row
epoch compare-and-set; ``seed_and_verify``/``attest`` are the startup seam."""

import base64
import binascii
import json

import structlog
from sqlalchemy import text

from kyc_tool.db.session import uow
from kyc_tool.domain.engine import ENGINE_BUILD_ID
from kyc_tool.policy.loader import (  # PolicyBundle also lives here
    POLICY_FILES,
    build_bundle,
    read_policy_files,
)

log = structlog.get_logger(__name__)


class BundleCorrupt(Exception): ...

def _encode_files(raw):                                   # raw: dict[str, bytes]
    return {n: base64.b64encode(b).decode() for n, b in raw.items()}

def _decode_files(files_json):                            # byte-level only; no key-set check
    out = {}
    for name, b64 in files_json.items():
        try:
            out[name] = base64.b64decode(b64, validate=True)   # strict: raises on non-b64
        except (binascii.Error, ValueError, TypeError) as e:
            raise BundleCorrupt(f"{name}: bad base64") from e
    return out

def store_bundle(session, raw) -> str:
    bundle_hash = build_bundle(raw).bundle_hash
    session.execute(text(
        "INSERT INTO policy_bundles (bundle_hash, files_json) VALUES (:h, :f) "
        "ON CONFLICT (bundle_hash) DO NOTHING"),
        {"h": bundle_hash, "f": json.dumps(_encode_files(raw))})
    verified = load_bundle(session, bundle_hash)          # read-back BOTH paths
    if verified is None or verified.bundle_hash != bundle_hash:
        raise BundleCorrupt(f"persisted bundle {bundle_hash} did not reconstruct")
    return bundle_hash

def load_bundle(session, bundle_hash):
    row = session.execute(text("SELECT files_json FROM policy_bundles WHERE bundle_hash=:h"),
                          {"h": bundle_hash}).first()
    if row is None:
        return None
    try:
        # exact 7-key set (missing OR extra) — inside the try so a non-object
        # files_json (a JSON scalar/null via direct tamper) whose set() raises
        # TypeError also normalizes to BundleCorrupt, per the module invariant.
        if set(row.files_json) != set(POLICY_FILES):
            raise BundleCorrupt(f"{bundle_hash}: files_json keys {set(row.files_json)} "
                                f"!= {set(POLICY_FILES)}")
        bundle = build_bundle(_decode_files(row.files_json), policy_dir=None)
    except BundleCorrupt:
        raise
    except Exception as e:                                 # json/Pydantic/scalar → BundleCorrupt
        raise BundleCorrupt(f"{bundle_hash}: does not reconstruct") from e
    if bundle.bundle_hash != bundle_hash:
        raise BundleCorrupt(f"{bundle_hash}: reconstructed hash mismatch")
    return bundle

def activate_epoch(session, *, expect_bundle_hash, expect_engine) -> None:
    if expect_engine != ENGINE_BUILD_ID:
        raise BundleCorrupt(f"engine {expect_engine} != local {ENGINE_BUILD_ID}")
    if load_bundle(session, expect_bundle_hash) is None:
        raise BundleCorrupt(f"epoch bundle {expect_bundle_hash} not in store")
    session.execute(text(
        "INSERT INTO bundle_pinning_epoch (id, activated_at, bundle_hash, engine_build_id) "
        "VALUES (1, now(), :h, :e) ON CONFLICT (id) DO NOTHING"),
        {"h": expect_bundle_hash, "e": expect_engine})
    got = read_epoch(session)                              # read-back-compare
    if got != (expect_bundle_hash, expect_engine):
        raise BundleCorrupt(f"epoch already ({got}) != requested "
                            f"({expect_bundle_hash},{expect_engine})")

def read_epoch(session):
    row = session.execute(text(
        "SELECT bundle_hash, engine_build_id FROM bundle_pinning_epoch WHERE id=1")).first()
    return (row.bundle_hash, row.engine_build_id) if row else None

def seed_and_verify(session_factory, policy_dir) -> str:
    """Startup seam: store the on-disk policy bundle and read back to prove it
    persisted intact. Raises BundleCorrupt on a corrupt existing row — the
    caller (API/pipeline/dev startup) must let that fail the process boot."""
    with uow(session_factory) as s:
        h = store_bundle(s, read_policy_files(policy_dir))
        return h

def attest(*, flag: bool, bundle_hash: str) -> None:
    """Deployment witness (§8.18): only called after a successful seed_and_verify."""
    log.info("bundle_pinning_ready", flag=flag, bundle_hash=bundle_hash,
              engine_build_id=ENGINE_BUILD_ID)
