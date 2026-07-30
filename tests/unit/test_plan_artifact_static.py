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

import pytest

from kyc_tool.config import REPO_ROOT

PLAN = REPO_ROOT / ".agents" / "superpowers" / "plans" / (
    "2026-07-23-pr7b-core-outbox-stream-separation.md"
)
# Invoke Ruff through the RUNNING interpreter, never a hardcoded ".venv/bin/ruff": CI installs
# the project with `pip install -e '.[dev]'` and has no .venv, so a hardcoded path fails there
# while passing locally — which is exactly how this gate broke CI on its first commit.
RUFF_CMD = [sys.executable, "-m", "ruff"]
# Lint each block under the REPOSITORY'S OWN config, addressed by the path the plan says the
# block becomes. `--isolated` was tried first and is wrong here: it discards `src = ["src",
# "tests"]`, so Ruff has to GUESS whether `kyc_tool` is first-party from the working directory —
# which made the same block pass locally and fail in CI, and flagged different blocks in each.
# `--stdin-filename` makes the verdict identical to `ruff check .` on the real file, everywhere.
RUFF_CONFIG = REPO_ROOT / "pyproject.toml"
_MARKER = re.compile(r"^<!--\s*complete-file:\s*(\S+\.py)\s*-->$")

# The exact set of whole-file blocks the plan is expected to carry, in order. Pinned rather than
# counted: a `>= 8` floor passed while three real blocks were undiscovered, because the ones it
# did find outnumbered the threshold. An exact list makes a missing marker, a duplicated block,
# and an unexpected new one all fail — and names which.
EXPECTED_COMPLETE_FILES = [
    "tests/integration/test_verify_pr7b_core_backfill.py",
    "src/kyc_tool/ops/verify_pr7b_core_backfill.py",
    "tests/integration/test_reset_interrupted_outbox_claims.py",
    "src/kyc_tool/ops/reset_interrupted_outbox_claims.py",
    "src/kyc_tool/ops/repair_outbox_sequence.py",
    "tests/integration/test_repair_outbox_sequence.py",
    "tests/unit/test_docs_cutover_parity.py",
    "tests/integration/test_rollback_command.py",
]


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
    """Python blocks the plan introduces as whole files, found by an explicit marker.

    Only these can be linted as files; append/insertion fragments legitimately reference names
    defined in the target file and would report spurious F821.

    Discovery is by an explicit `<!-- complete-file: <path> -->` line immediately above the fence,
    NOT by matching prose. The prose heuristic this replaced looked back three lines for
    "create `<path>`" and was case-sensitive, so a block introduced as "Create
    `tests/integration/test_rollback_command.py`:" was invisible to it — and a block the gate
    skips is a block whose syntax errors ship. A marker cannot be missed by accident, and
    `test_plan_complete_file_paths_are_exactly_expected` pins the resulting set, so a marker that
    goes missing fails loudly instead of silently shrinking this suite.
    """
    blocks, _ = _fenced_blocks(lines)
    out = []
    for start, tag, body in blocks:
        if tag != "```python" or start < 2:
            continue
        match = _MARKER.match(lines[start - 2].strip())
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


def test_plan_complete_file_paths_are_exactly_expected():
    """Guards the gate itself: the discovered set must match `EXPECTED_COMPLETE_FILES` exactly.

    A missing marker silently shrinks every parametrized test below it, so this must be an exact
    comparison, not a floor. Adding a genuinely new whole-file block to the plan is expected to
    fail here once — update the list deliberately.
    """
    found = [path for _, path, _ in _complete_file_blocks(PLAN.read_text().splitlines())]
    assert found == EXPECTED_COMPLETE_FILES, (
        f"complete-file blocks drifted.\n  missing: {sorted(set(EXPECTED_COMPLETE_FILES) - set(found))}"
        f"\n  unexpected: {sorted(set(found) - set(EXPECTED_COMPLETE_FILES))}"
        f"\n  found order: {found}"
    )


def test_plan_complete_file_paths_are_unique():
    """Two blocks claiming one path means one of them is not the file it says it is."""
    found = [path for _, path, _ in _complete_file_blocks(PLAN.read_text().splitlines())]
    assert len(found) == len(set(found)), f"duplicate complete-file paths: {found}"


def test_every_marker_is_followed_by_a_python_fence():
    """A marker whose fence is missing (or is not ```python) discovers nothing and would be
    invisible to the exact-set test only if the path also vanished from the list. Catch the
    orphan directly, so a mis-typed fence tag cannot quietly drop a block from the suite."""
    lines = PLAN.read_text().splitlines()
    orphans = [
        (i, line.strip())
        for i, line in enumerate(lines, 1)
        if _MARKER.match(line.strip()) and (i >= len(lines) or lines[i].strip() != "```python")
    ]
    assert not orphans, f"complete-file marker(s) not followed by a ```python fence: {orphans}"


@pytest.mark.parametrize(
    "markdown,expected",
    [
        pytest.param(["<!-- complete-file: a/b.py -->", "```python", "x = 1", "```"],
                     ["a/b.py"], id="marker-discovers-block"),
        pytest.param(["Create `a/b.py`:", "```python", "x = 1", "```"],
                     [], id="prose-alone-discovers-nothing"),
        pytest.param(["<!-- complete-file: a/b.py -->", "```", "x = 1", "```"],
                     [], id="non-python-fence-ignored"),
        pytest.param(["<!-- complete-file: a/b.py -->", "", "```python", "x = 1", "```"],
                     [], id="marker-must-be-adjacent"),
    ],
)
def test_marker_parser_behaviour(markdown, expected):
    """The discovery rule itself, pinned. The heuristic this replaced was never tested, which is
    why its case-sensitivity bug survived a full review round."""
    assert [p for _, p, _ in _complete_file_blocks(markdown)] == expected


@pytest.mark.parametrize(
    "start,path,body",
    _complete_file_blocks(PLAN.read_text().splitlines()),
    ids=lambda v: v if isinstance(v, str) and "/" in str(v) else "",
)
def test_plan_complete_file_blocks_lint_clean(start, path, body):
    """Lint each COMPLETE file block as the file it claims to be."""
    result = subprocess.run(
        [*RUFF_CMD, "check", "--config", str(RUFF_CONFIG),
         "--stdin-filename", path, "--output-format", "concise", "-"],
        input=body + "\n", capture_output=True, text=True,
        # cwd pinned to the repo: Ruff resolves the `src = ["src", "tests"]` roots and the
        # stdin-filename relative to the working directory, so without this the verdict depends
        # on where pytest was invoked from — which is how the first version of this gate passed
        # locally and failed in CI on a different block than it flagged by hand.
        cwd=str(REPO_ROOT),
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
    limit = 110  # matches [tool.ruff] line-length
    over = [
        (start + offset, len(line))
        for start, tag, body in blocks if tag == "```python"
        for offset, line in enumerate(body.splitlines(), 1)
        if len(line) > limit
    ]
    assert not over, f"python block lines exceeding {limit} chars at {over}"


def test_ruff_is_available():
    """The lint gate must fail loudly rather than silently skip if Ruff cannot be invoked."""
    result = subprocess.run([*RUFF_CMD, "--version"], capture_output=True, text=True)
    assert result.returncode == 0, (
        f"`{' '.join(RUFF_CMD)} --version` failed — this gate cannot silently pass without Ruff:\n"
        f"{result.stdout}{result.stderr}"
    )
    assert sys.version_info >= (3, 11)


# The design artifacts this plan implements. Kept here rather than in a separate module because
# they fail the same way: a stray blank line at EOF trips `git diff --check`, which is a CI gate.
_ARTIFACTS = [
    PLAN,
    REPO_ROOT / ".agents" / "superpowers" / "specs" / (
        "2026-07-22-pr7b-core-outbox-stream-separation-design.md"),
    REPO_ROOT / ".agents" / "superpowers" / "specs" / (
        "2026-07-22-pr7b-activation-platform-ordering-design.md"),
]


# Claims that were asserted in an earlier revision, disproven by review, and corrected. Each is
# banned by regex so it cannot be reasserted — every one of these survived at least one full
# review round as live text contradicting the code, because prose has no compiler and a corrected
# design leaves stale sentences behind in exactly the places nobody re-reads.
_DISPROVEN_CLAIMS = [
    (
        r"NULL digest means\s+\"never delivered\"",
        "the terminal digest is written in the fenced terminal transaction, so a 2xx followed by a "
        "terminal fault leaves NULL while the platform holds the bytes. Use the four-state witness "
        "taxonomy (delivery_witnessed / send_intent_witnessed / legacy_unwitnessed / not_accepted).",
    ),
    (
        r"backfill(?:ed|s)? (?:a )?(?:the )?historical digest|backfilled at 013",
        "013 backfills no historical digest: payload_json is jsonb and normalizes key order, so "
        "pre-013 sent bytes are unrecoverable and a computed digest would fabricate a witness.",
    ),
    (
        r"keeps its\s+body indefinitely|body (?:is )?retained forever",
        "retention redacts delivered and superseded decision-callback bodies past "
        "KYC_RETENTION_DAYS; only the ROW survives indefinitely.",
    ),
    (
        r"derive the (?:wire )?digest from `?payload_json|re-derive[sd]? the digest from",
        "a wire digest may never be derived from stored JSONB — jsonb key order is not wire order.",
    ),
    (
        r"no retention deviation left to govern|KYC_RETENTION_DAYS no longer bounds",
        "the pseudonymous remainder (ids, ordinals, digest) IS a governed retention decision — "
        "AUDIT_FINDINGS D9. Redaction discharged the reviewer-identifier problem, not governance.",
    ),
    (
        # a line RECORDING the rename ("renamed from …", "the earlier name … retired") is the
        # correction, not a reassertion — everything else using the old name is banned.
        r"^(?!.*(?:renamed|retired)).*\battempt_witnessed\b",
        "renamed send_intent_witnessed (re-audit 1f8412e F9): an attempt row proves durably staged "
        "intent, not transmission — the process can die between the commit and the socket.",
    ),
    (
        r"reversible-before-first-supersession downgrade",
        "the downgrade rule is witness-aware (re-audit 1f8412e F3): supersession OR any attempt "
        "row OR any terminal digest refuses; rollback after first witness use is flag/image only.",
    ),
    (
        # a walk that STARTS below the real head omits the top of the chain; the full walk
        # `022 → … → 013 → 012` never matches (its inner pairs are "→ "-preceded).
        r"(?<!→ )\b01[45] → 013 → 012|(?<!→ )\b016 → 015 → ",
        "the documented rollback walk starts at the REAL head (022 → … → 013 → "
        "012); a walk starting mid-chain never executes the refusals production actually hits "
        "(re-audits 0c46443 F5, 15d875d F7).",
    ),
    (
        r"\b01[3-9]`?-COMPATIBLE image|020`?-COMPATIBLE image|"
        r"021`?-COMPATIBLE image|compatible schema after first witness use",
        "on refusal the operator keeps the reviewed 022-compatible image — naming an older "
        "compatibility set restarts a publisher without the receipt/terminal contract against "
        "preserved evidence (re-audits 0c46443 F5, 15d875d F7).",
    ),
    (
        # activation is migration 023; every earlier number it wore was consumed by a
        # 7b-core revision. The proximity window is gone: a stale number three sentences after
        # the word "activation" is the same defect as one three characters after it (re-audit
        # `cbb783b` F8). Lines RECORDING a renumber are the correction, not a reassertion.
        # `013`-`022` (the 7b-core SHIPPED range) and `down_revision='022'` (activation's correct
        # parent) are not stale activation numbers — exclude those two shapes explicitly rather
        # than by proximity, which is what let these contradictions survive before.
        r"^(?!.*(?:renumber|moves to|forced|historical|superseded|`013`-|down_revision))"
        r".*(?:7b-activation|activation)[^\n]{0,80}?`(?:01[4-9]|02[0-2])`",
        "7b-activation is migration `023` (`down_revision='022'`): 7b-core shipped 013-022, and "
        "the lineage test pins pending[0]=head+1 (re-audits 15d875d F7, cbb783b F8).",
    ),
    (
        # "the walk is 017 -> ..." as LIVE guidance: with 022 installed there is no schema
        # downgrade at all, so any live text presenting the walk as the rollback path is wrong.
        r"^(?!.*(?:historical|superseded|unreachable|never reached))"
        r".*rollback is.*`?alembic downgrade",
        "with `022` installed the rollback is the prior reviewed 022-compatible IMAGE on schema "
        "`022` — `alembic downgrade` refuses unconditionally (re-audit `cbb783b` F3).",
    ),
]


@pytest.mark.parametrize("pattern,why", _DISPROVEN_CLAIMS, ids=lambda v: "" if " " in str(v) else v)
@pytest.mark.parametrize("path", _ARTIFACTS, ids=lambda p: p.name)
def test_artifact_does_not_reassert_a_disproven_claim(path, pattern, why):
    """A design artifact must not contain a claim its own unit already disproved."""
    text = path.read_text()
    hits = [
        f"line {i}: {line.strip()}"
        for i, line in enumerate(text.splitlines(), 1)
        if re.search(pattern, line, re.IGNORECASE)
        # the ban list itself, and text explicitly recording the correction, are not violations
        and "must not" not in line.lower() and "does NOT" not in line
    ]
    assert not hits, f"{path.name} reasserts a disproven claim — {why}\n" + "\n".join(hits)


@pytest.mark.parametrize("path", _ARTIFACTS, ids=lambda p: p.name)
def test_artifact_has_no_trailing_blank_line(path):
    """`git diff --check` rejects a new blank line at EOF, so appending a revision note with a
    string that already ends in a newline silently breaks a CI gate. Caught here instead."""
    raw = path.read_text()
    assert raw.endswith("\n"), f"{path.name} must end with exactly one newline"
    assert not raw.endswith("\n\n"), (
        f"{path.name} ends with a blank line — `git diff --check` fails on this"
    )
