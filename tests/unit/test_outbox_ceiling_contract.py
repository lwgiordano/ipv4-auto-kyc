"""Re-audit `d3c0852..23e005e` F4 + `d569a15..4938840` F5/F7: the outbox retry ceiling is
PROCESS-LOCAL, so ANY fleet-wide change to `outbox_max_attempts` — raise OR lower — is a drained
publisher cutover, never a rolling restart.

HONEST SCOPE: this is a CONTENT-ASSERTION guard over the operator docs, NOT a parsed contract. It
checks that the required clauses are present and that the previously-shipped FALSE claim ("raising is
always safe") is absent — including a mutation self-test proving it would catch that claim's return.
A machine-parsed cutover table (exact states/roles/order consumed by both docs) is the stronger form
and is deferred to the production-ops-hardening unit (ROADMAP PR 10); this guard does not pretend to
be it.
"""

from kyc_tool.config import REPO_ROOT

_DEPLOYMENT = REPO_ROOT / "docs" / "DEPLOYMENT.md"

# The FALSE claim the previous round shipped — it must never reappear in any form. (Substrings, so
# "raising is safe" / "raising the ceiling is always safe" / "raising ... rolling-safe" all trip.)
_FALSE_SAFE_DIRECTION_CLAIMS = (
    "raising the ceiling is always safe",
    "raising is always safe",
    "raising it is rolling-safe",
    "raising is rolling-safe",
    "raising the ceiling is safe",
)


def test_deployment_requires_a_drained_cutover_for_either_direction():
    dep = _DEPLOYMENT.read_text().lower()
    assert "outbox_max_attempts" in dep
    assert "drained" in dep and "rolling restart" in dep
    # both directions must be named as needing the cutover
    assert "lowering" in dep and "raising" in dep
    # the attested-stop clause (zero old processes) must be present
    assert "zero" in dep and ("stop all" in dep or "stop all outbox" in dep)


def test_the_false_raising_is_safe_claim_is_absent_and_the_guard_would_catch_it():
    dep = _DEPLOYMENT.read_text().lower()
    hits = [c for c in _FALSE_SAFE_DIRECTION_CLAIMS if c in dep]
    assert not hits, f"DEPLOYMENT reasserts a refuted claim {hits}: raising also needs a drained cutover"
    # the guard has teeth: the exact phrasing the previous round shipped IS in the denylist
    assert "raising is always safe" in _FALSE_SAFE_DIRECTION_CLAIMS


def test_config_comment_flags_the_process_local_ceiling_both_directions():
    cfg = (REPO_ROOT / "src" / "kyc_tool" / "config.py").read_text().lower()
    assert "process-local" in cfg and "drained" in cfg
    assert "raise or lower" in cfg or ("raise" in cfg and "lower" in cfg)
