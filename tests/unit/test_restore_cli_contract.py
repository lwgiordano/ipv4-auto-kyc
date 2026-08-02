"""Re-audit `5b0f0b8..b75a320` R4-F6: the restore "documented argv" proof must parse the ACTUAL
documented commands, not a hand-coded specimen, and must not be a substring search (which stayed green
when a real command was changed to `--expect-manifest-digest-bogus`).

This extracts every backtick-delimited `python -m kyc_tool.ops.restore_pr7b_core_callback ...` command
from each operator surface, joins line continuations, substitutes typed placeholders, `shlex.split`s
it, and parses it with the REAL `build_parser()` — which exits 2 on an unknown/misspelled/missing
option. A typo in any surface fails here.
"""

import re
import shlex

import pytest

from kyc_tool.config import REPO_ROOT
from kyc_tool.ops.restore_pr7b_core_callback import build_parser

_SURFACES = ("docs/DEPLOYMENT.md", "docs/RUNBOOK.md",
             ".agents/superpowers/plans/2026-07-23-pr7b-core-outbox-stream-separation.md")
_CMD_RE = re.compile(
    r"`([^`]*python -m kyc_tool\.ops\.restore_pr7b_core_callback[^`]*)`", re.S
)
_PLACEHOLDERS = {"<file.json>": "f.json", "<file>": "f.json", "<id>": "7", "<sha256>": "0" * 64}


def _argv(command: str) -> list[str]:
    command = " ".join(command.replace("\\", " ").split())  # join continuations, collapse whitespace
    command = command.replace("[--apply]", "--apply").replace("[", "").replace("]", "")
    for placeholder, value in _PLACEHOLDERS.items():
        command = command.replace(placeholder, value)
    parts = shlex.split(command)
    i = next(i for i, p in enumerate(parts) if "restore_pr7b_core_callback" in p)
    return parts[i + 1:]


def _documented_commands():
    for rel in _SURFACES:
        for match in _CMD_RE.findall((REPO_ROOT / rel).read_text()):
            yield rel, match


def test_at_least_the_known_surfaces_document_the_command():
    rels = {rel for rel, _ in _documented_commands()}
    assert rels == set(_SURFACES), f"a surface stopped documenting the restore command: {rels}"


def test_every_documented_restore_command_parses_through_the_real_parser():
    """Each extracted command parses with build_parser() (which enforces option spelling,
    requiredness and value type); a misspelled/unknown option exits 2 and fails the test."""
    seen = 0
    for rel, command in _documented_commands():
        argv = _argv(command)
        try:
            ns = build_parser().parse_args(argv)
        except SystemExit as exc:  # argparse exits 2 on a bad/unknown/missing option
            raise AssertionError(f"{rel}: documented command does not parse: {command!r}") from exc
        assert ns.evidence and ns.expect_original_id and ns.expect_manifest_digest
        seen += 1
    assert seen >= len(_SURFACES)


def test_the_extractor_actually_catches_a_misspelled_option():
    """Guard-the-guard: a command with --expect-manifest-digest-bogus (Codex's exact bypass) must be
    rejected by the real parser, proving this is not a substring search."""
    bad = "python -m kyc_tool.ops.restore_pr7b_core_callback --evidence f.json " \
          "--expect-original-id 7 --expect-manifest-digest-bogus " + "0" * 64
    with pytest.raises(SystemExit):
        build_parser().parse_args(_argv(bad))
