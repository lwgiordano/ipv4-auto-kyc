"""FROZEN historical migration contract guard. NEVER re-pin V013_BACKFILL_SHA — a change to
v013_backfill.py alters how a historical 012→013 upgrade behaves on a persistent DB. For any
later backfill semantics, add src/kyc_tool/migration_contracts/v014_*.py and pin it separately.
(This is distinct from the whole-source engine drift guard, which stays re-pinnable.)"""

import hashlib

from kyc_tool.config import REPO_ROOT

_CONTRACT = REPO_ROOT / "src" / "kyc_tool" / "migration_contracts" / "v013_backfill.py"
# sha256 of the exact Step-1 module bytes (write the file verbatim: LF endings, one trailing newline).
V013_BACKFILL_SHA = "bdd2342be673c2b324af02cf00644ecde70739a233e7c7cb39670123fb6d1c04"


def test_v013_backfill_contract_is_frozen():
    got = hashlib.sha256(_CONTRACT.read_bytes()).hexdigest()
    assert got == V013_BACKFILL_SHA, (
        "v013_backfill.py is a FROZEN historical migration contract — do NOT re-pin this hash; "
        "create migration_contracts/v014_*.py for any later semantics."
    )
