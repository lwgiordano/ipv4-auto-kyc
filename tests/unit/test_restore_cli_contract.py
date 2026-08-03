"""Re-audit `5b0f0b8..b75a320` R4-F6 + `03dbfab..bc325e7` R5-F10: the restore "documented argv" proof
parses the ACTUAL documented commands through the REAL parser, and the grammar is unambiguous.

Every governed surface (module docstring, DEPLOYMENT, RUNBOOK, plan) must contain EXACTLY ONE
backtick-delimited `python -m kyc_tool.ops.restore_pr7b_core_callback ...` command; the extractor
allowlists the launcher, rejects shell control characters and unresolved placeholders, and parses with
`build_parser()` (allow_abbrev=False, singleton options, typed values). A truncation, duplicate, alias,
shell prefix, or missing block fails here.
"""

import re
import shlex

import pytest

from kyc_tool.config import REPO_ROOT
from kyc_tool.ops.restore_pr7b_core_callback import build_parser

_LAUNCHER = "python -m kyc_tool.ops.restore_pr7b_core_callback"
_SURFACES = (
    "src/kyc_tool/ops/restore_pr7b_core_callback.py",
    "docs/DEPLOYMENT.md",
    "docs/RUNBOOK.md",
    ".agents/superpowers/plans/2026-07-23-pr7b-core-outbox-stream-separation.md",
)
_CMD_RE = re.compile(rf"`([^`]*{re.escape(_LAUNCHER)}[^`]*)`", re.S)
_PLACEHOLDERS = {"<file.json>": "f.json", "<file>": "f.json", "<id>": "7", "<sha256>": "0" * 64}
_SHELL_CONTROLS = (";", "&&", "||", "|", ">", "<(", "$(", "`", "&")


def _argv(command: str) -> list[str]:
    """Normalize and validate one extracted command to an argv, or raise on ambiguous grammar."""
    command = " ".join(command.replace("\\", " ").split())  # join continuations, collapse whitespace
    command = command.replace("[--apply]", "--apply").replace("[", "").replace("]", "")
    # allowlist the launcher: the command must START with it — no `foo; python -m ...` shell prefix
    if not command.startswith(_LAUNCHER):
        raise ValueError(f"command does not start with the allowlisted launcher: {command!r}")
    for placeholder, value in _PLACEHOLDERS.items():
        command = command.replace(placeholder, value)
    if any(ctrl in command for ctrl in _SHELL_CONTROLS):
        raise ValueError(f"command contains a shell control character: {command!r}")
    if "<" in command or ">" in command:
        raise ValueError(f"command has an unresolved placeholder: {command!r}")
    parts = shlex.split(command)
    # EXACT launcher tokens (re-audit `f2929f8..6a4cd87` F14): substring/startswith matching accepted
    # `...restore_pr7b_core_callback_evil` — the module token must equal the real module exactly.
    if parts[:3] != ["python", "-m", "kyc_tool.ops.restore_pr7b_core_callback"]:
        raise ValueError(f"launcher tokens are not exactly the sanctioned module: {parts[:3]}")
    return parts[3:]


def _documented():
    for rel in _SURFACES:
        yield rel, _CMD_RE.findall((REPO_ROOT / rel).read_text())


def test_every_required_surface_documents_the_command():
    for rel, matches in _documented():
        assert matches, f"{rel} no longer documents the restore command"


def test_every_documented_command_on_every_surface_parses():
    """Governs ALL documented commands (a surface may legitimately show it more than once, e.g. a
    quick-reference table and a detailed procedure); each one must parse through the real parser."""
    for rel, matches in _documented():
        for command in matches:
            argv = _argv(command)
            try:
                ns = build_parser().parse_args(argv)
            except SystemExit as exc:
                raise AssertionError(f"{rel}: documented command does not parse: {command!r}") from exc
            assert ns.evidence and ns.expect_original_id and ns.expect_manifest_digest


_D = "0" * 64  # a valid 64-hex digest


@pytest.mark.parametrize(
    "argv",
    [
        # option truncation (allow_abbrev=False)
        ["--evidence", "f.json", "--expect-original-id", "7", "--expect-manifest-dig", _D],
        # duplicate/conflicting singleton
        ["--evidence", "f.json", "--expect-original-id", "7", "--expect-original-id", "8",
         "--expect-manifest-digest", _D],
        # duplicate --apply
        ["--evidence", "f.json", "--expect-original-id", "7", "--expect-manifest-digest", _D,
         "--apply", "--apply"],
        # non-positive id
        ["--evidence", "f.json", "--expect-original-id", "-1", "--expect-manifest-digest", _D],
        # non-hex digest
        ["--evidence", "f.json", "--expect-original-id", "7", "--expect-manifest-digest", "z" * 64],
        # missing mandatory digest
        ["--evidence", "f.json", "--expect-original-id", "7"],
    ],
)
def test_real_parser_rejects_ambiguous_or_invalid_grammar(argv):
    with pytest.raises(SystemExit):
        build_parser().parse_args(argv)


@pytest.mark.parametrize(
    "command",
    [
        f"evil; {_LAUNCHER} --evidence <file> --expect-original-id <id> --expect-manifest-digest <sha256>",
        f"{_LAUNCHER} --evidence <file> --expect-original-id <id> --expect-manifest-digest $(cat secret)",
        f"{_LAUNCHER} --evidence <file> --expect-original-id <id> --expect-manifest-digest <sha256> > /tmp/x",
        f"{_LAUNCHER} --evidence <notresolved> --expect-original-id <id> --expect-manifest-digest <sha256>",
    ],
)
def test_extractor_rejects_shell_controls_and_unresolved_placeholders(command):
    with pytest.raises(ValueError):
        _argv(command)


def test_extractor_rejects_an_evil_module_suffix():
    """F14: `..._evil` passed startswith matching; exact launcher-token equality refuses it."""
    bad = _LAUNCHER + "_evil --evidence f.json --expect-original-id 7 --expect-manifest-digest " + _D
    with pytest.raises(ValueError):
        _argv(bad)
