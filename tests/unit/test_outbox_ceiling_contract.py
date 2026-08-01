"""Re-audit `d3c0852..23e005e` F4: the outbox retry ceiling is PROCESS-LOCAL — each publisher
enforces the `outbox_max_attempts` it started with. Lowering it fleet-wide is therefore a DRAINED
publisher cutover, not a rolling restart (an overlapping old publisher could make one more send past
the new value). This guard keeps that operational contract documented so it cannot silently drop.
"""

from kyc_tool.config import REPO_ROOT


def test_deployment_documents_the_drained_cutover_for_lowering_the_ceiling():
    dep = (REPO_ROOT / "docs" / "DEPLOYMENT.md").read_text().lower()
    assert "outbox_max_attempts" in dep
    assert "drained" in dep and "rolling restart" in dep     # lowering is a drained cutover
    assert "raising" in dep                                  # the safe direction is stated too


def test_config_comment_flags_the_process_local_ceiling():
    cfg = (REPO_ROOT / "src" / "kyc_tool" / "config.py").read_text().lower()
    assert "process-local" in cfg and "drained" in cfg
