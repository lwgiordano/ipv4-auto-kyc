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


# --- the migration FILE itself is frozen too (re-audit 1f8412e F1; provenance split 4dfdf8a F7) ---
# 013 was once amended in place after being committed: the attempt-authority table was added to
# the already-published revision, so a database stamped '013' by the original file would never
# receive it — Alembic performs no work for a recorded revision, and fresh-database CI passes on
# both shapes, so nothing structural catches the split. Repair revision 014 exists because of
# that mistake.
#
# TWO pins, because they freeze two different things and must fail independently:
# - the UPGRADE BODY is byte-identical to the original committed revision (9092fdb) — this is
#   what "restored" means, and any edit here re-creates the split-brain;
# - the FULL FILE is the original upgrade body PLUS the approved downgrade-only compatibility
#   guards (witness-in-use + amended-history refusals, 25 lines) added by the repair round. It is
#   NOT byte-identical to 9092fdb, and describing it as such conflated the two artifacts.
# ANY change to either must ship as a NEW revision (016+). NEVER re-pin.
_MIGRATION_013 = REPO_ROOT / "alembic" / "versions" / "013_outbox_stream_separation.py"
# exact 9092fdb upgrade body:
MIGRATION_013_UPGRADE_BODY_SHA = "108e04a88fd36587f559b2d1ecb929cd9a6b459a4e5fa862146c0940b14d2465"
# original upgrade + approved downgrade-only guards:
MIGRATION_013_FULL_SHA = "4c0ead28c1a57a7ed1d726ea204c54ca41390d0cb4d8477a96c409376b9580aa"


def test_migration_013_upgrade_body_is_the_original():
    text = _MIGRATION_013.read_text()
    body = text[text.index("def upgrade"):text.index("def downgrade")]
    got = hashlib.sha256(body.encode()).hexdigest()
    assert got == MIGRATION_013_UPGRADE_BODY_SHA, (
        "013's UPGRADE body must remain byte-identical to the originally committed revision "
        "(9092fdb) — editing it in place silently skips the edit on every database already "
        "stamped '013'. Ship the change as a NEW revision; do NOT re-pin."
    )


def test_migration_013_file_is_frozen():
    got = hashlib.sha256(_MIGRATION_013.read_bytes()).hexdigest()
    assert got == MIGRATION_013_FULL_SHA, (
        "alembic/versions/013_outbox_stream_separation.py is FROZEN as: original upgrade body + "
        "approved downgrade-only guards. Any further change — including downgrade edits — ships "
        "as a NEW revision; do NOT re-pin."
    )
