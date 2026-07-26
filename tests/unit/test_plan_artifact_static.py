"""Static gate for the PR 7b-core plan artifact (re-review `6a408a3` F8).

A build plan whose fences are unbalanced renders — and copy-pastes — as the wrong content, and a
plan whose file blocks do not lint produces code that fails the repo's own gates the moment an
implementer transcribes them.

Both defects shipped in earlier revisions because the checking was too weak: it counted only
```python fences (so an unbalanced ```markdown fence was invisible) and linted blocks in isolation
(so `F401` on a COMPLETE assembled file — an import left behind when its user was deleted — could
not be seen). This gate closes exactly those two holes.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

from kyc_tool.config import REPO_ROOT

PLAN = REPO_ROOT / ".agents" / "superpowers" / "plans" / (
    "2026-07-23-pr7b-core-outbox-stream-separation.md"
)
# Invoke Ruff through the RUNNING interpreter, never a hardcoded ".venv/bin/ruff": CI installs
# the project with `pip install -e '.[dev]'` and has no .venv, so a hardcoded path fails there
# while passing locally — which is exactly how this gate broke CI on its first commit.
RUFF_CMD = [sys.executable, "-m", "ruff"]
SELECTORS = "E,F,I,UP,B,SIM"  # the repository's own selector set
LINE_LENGTH = "110"


def _fenced_blocks(lines: list[str]) -> tuple[list[tuple[int, str, str]], list[int]]:
    """Return (blocks, unclosed_openers). A block is (start_line, opener, body).

    Fences toggle, so this walks them as a state machine rather than counting: an odd total is
    only the symptom, and the diagnostic an author needs is WHICH opener never closed.
    """
    blocks: list[tuple[int, str, str]] = []
    opener_line, opener_tag, body = 0, "", None
    open_stack: list[int] = []
    for i, line in enumerate(lines, 1):
        if line.startswith("```"):
            if body is None:
                opener_line, opener_tag, body = i, line.strip(), []
                open_stack.append(i)
            else:
                blocks.append((opener_line, opener_tag, "\n".join(body)))
                body = None
                open_stack.pop()
        elif body is not None:
            body.append(line)
    return blocks, open_stack


def _complete_file_blocks(lines: list[str]) -> list[tuple[int, str, str]]:
    """Python blocks the plan introduces as `create <path>` — i.e. a whole file, not a fragment.

    Only these can be linted as files; append/insertion fragments legitimately reference names
    defined in the target file and would report spurious F821.
    """
    blocks, _ = _fenced_blocks(lines)
    out = []
    for start, tag, body in blocks:
        if tag != "```python":
            continue
        context = "\n".join(lines[max(0, start - 4):start - 1])
        match = re.search(r"create `([^`]+\.py)`", context)
        if match:
            out.append((start, match.group(1), body))
    return out


def test_plan_fences_are_balanced():
    lines = PLAN.read_text().splitlines()
    _, unclosed = _fenced_blocks(lines)
    assert not unclosed, (
        f"unclosed code fence(s) opened at line(s) {unclosed}: every fence after one of these is "
        "inverted, so the plan renders and copy-pastes the wrong content"
    )


def test_plan_has_complete_file_blocks_to_check():
    # guards the gate itself: a refactor that broke the `create <path>` convention would silently
    # reduce this suite to a no-op.
    assert len(_complete_file_blocks(PLAN.read_text().splitlines())) >= 8


@pytest.mark.parametrize(
    "start,path,body",
    _complete_file_blocks(PLAN.read_text().splitlines()),
    ids=lambda v: v if isinstance(v, str) and "/" in str(v) else "",
)
def test_plan_complete_file_blocks_lint_clean(tmp_path, start, path, body):
    """Lint each COMPLETE file block as the file it claims to be."""
    target = tmp_path / Path(path).name
    target.write_text(body + "\n")
    result = subprocess.run(
        [*RUFF_CMD, "check", "--select", SELECTORS, "--line-length", LINE_LENGTH,
         "--isolated", "--output-format", "concise", str(target)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"plan block at line {start} ({path}) does not lint as a complete file:\n{result.stdout}"
    )


@pytest.mark.parametrize(
    "start,path,body",
    _complete_file_blocks(PLAN.read_text().splitlines()),
    ids=lambda v: v if isinstance(v, str) and "/" in str(v) else "",
)
def test_plan_complete_file_blocks_compile(start, path, body):
    try:
        compile(body, f"{path}@plan:{start}", "exec")
    except SyntaxError as exc:  # pragma: no cover - failure path carries the diagnostic
        pytest.fail(f"plan block at line {start} ({path}) is not valid Python: {exc}")


def test_plan_lines_within_line_length():
    """Every python block honours the repo's line length, so transcription never needs reflowing."""
    lines = PLAN.read_text().splitlines()
    blocks, _ = _fenced_blocks(lines)
    over = [
        (start + offset, len(line))
        for start, tag, body in blocks if tag == "```python"
        for offset, line in enumerate(body.splitlines(), 1)
        if len(line) > int(LINE_LENGTH)
    ]
    assert not over, f"python block lines exceeding {LINE_LENGTH} chars at {over}"


def test_ruff_is_available():
    """The lint gate must fail loudly rather than silently skip if Ruff cannot be invoked."""
    result = subprocess.run([*RUFF_CMD, "--version"], capture_output=True, text=True)
    assert result.returncode == 0, (
        f"`{' '.join(RUFF_CMD)} --version` failed — this gate cannot silently pass without Ruff:\n"
        f"{result.stdout}{result.stderr}"
    )
    assert sys.version_info >= (3, 11)
