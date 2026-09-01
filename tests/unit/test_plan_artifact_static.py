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
from tests import roadmap

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


# Operator-facing runbooks. They are NOT design artifacts, so they are deliberately not run
# through the whole ban list below; they are checked for the facts an operator acts on during
# an incident — which image is compatible with the live schema, and which refusal sentinels to
# expect. Both were wrong when these checks were written: the image was named a full revision
# behind the schema, and no upgrade-side sentinel was documented anywhere.
_OPERATOR_DOCS = [
    REPO_ROOT / "docs" / "RUNBOOK.md",
    REPO_ROOT / "docs" / "DEPLOYMENT.md",
]

# --- revision numbers are DERIVED from ROADMAP §C, never transcribed --------------------------
# Two of the bans below name migration numbers, and both were hand-maintained lists. Both went
# stale on the release that shipped `023`: the activation ban still permitted `023` — the number
# activation had just vacated — and its message named the wrong revisions. A gate written to catch
# stale migration numbers, itself one revision behind, through a full audit round.
#
# The numbers now come from §C, which `tests/unit/test_migration_lineage.py` pins against the real
# Alembic chain (every authored revision is `shipped`, no `pending` revision exists yet, the live
# head is the highest `shipped`). A release updates one table and these bans follow it.
_RECORDS = roadmap.records()
_HEAD = roadmap.head(_RECORDS)                                  # live schema == live image
_CORE = roadmap.unit_revisions(_RECORDS, "PR 7b-core")          # the whole 7b-core family
_ACTIVATION = roadmap.unit_revisions(_RECORDS, "PR 7b-activation")[0]


def _alternation(numbers) -> str:
    """`(?:014|015|…)` — an explicit alternation, not a character-class range. `01[4-9]` reads
    like a range but silently stops extending at the decade boundary, which is why the previous
    hand-written ban needed a separate `020`/`021` clause bolted onto it every release."""
    return "(?:" + "|".join(f"{n:03d}" for n in sorted(numbers)) + ")"


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
        # `NNN → … → 013 → 012` never matches (its inner pairs are "→ "-preceded).
        r"(?<!→ )\b01[45] → 013 → 012|(?<!→ )\b016 → 015 → ",
        f"the documented rollback walk starts at the REAL head ({_HEAD:03d} → … → 013 → "
        "012); a walk starting mid-chain never executes the refusals production actually hits "
        "(re-audits 0c46443 F5, 15d875d F7).",
    ),
    (
        # The NUMERIC half of this ban is now `test_compatible_image_names_the_live_head`, which
        # is strictly stronger: it rejects ANY number that is not the head (including one typed
        # ahead of a release) instead of only the numbers someone remembered to enumerate, and it
        # covers the operator docs too — where the stale `022` actually survived. What stays here
        # is the non-numeric claim, which no head number can express.
        r"compatible schema after first witness use",
        "after first witness use no downgrade is reachable, so what the operator keeps is a "
        "compatible IMAGE, not a compatible SCHEMA — naming a schema implies a walk that "
        "refuses (re-audits 0c46443 F5, 15d875d F7).",
    ),
    (
        # Every number below activation's own reservation, down to the first 7b-core revision,
        # was consumed by a 7b-core revision — so near the word "activation" each is a number
        # activation used to wear. The proximity window is gone: a stale number three sentences
        # after the word "activation" is the same defect as one three characters after it
        # (re-audit `cbb783b` F8). Lines RECORDING a renumber are the correction, not a
        # reassertion. The 7b-core SHIPPED range (`013`-`0NN`) and `down_revision='0NN'`
        # (activation's correct parent) are not stale activation numbers — those two shapes are
        # excluded explicitly rather than by proximity, which is what let these survive before.
        rf"^(?!.*(?:renumber|moves to|forced|historical|superseded|`{_CORE[0]:03d}`-"
        rf"|down_revision))"
        rf".*(?:7b-activation|activation)[^\n]{{0,80}}?"
        rf"`{_alternation(range(_CORE[0] + 1, _ACTIVATION))}`",
        f"7b-activation is migration `{_ACTIVATION:03d}` "
        f"(`down_revision='{_ACTIVATION - 1:03d}'`): 7b-core shipped "
        f"{_CORE[0]:03d}-{_CORE[-1]:03d}, and the lineage test pins pending[0]=head+1 "
        "(re-audits 15d875d F7, cbb783b F8).",
    ),
    (
        # "the walk is 017 -> ..." as LIVE guidance: with the forward-only revisions installed
        # there is no schema downgrade at all, so any live text presenting the walk as the
        # rollback path is wrong.
        r"^(?!.*(?:historical|superseded|unreachable|never reached))"
        r".*rollback is.*`?alembic downgrade",
        f"with `{_HEAD:03d}` installed the rollback is the prior reviewed "
        f"{_HEAD:03d}-compatible IMAGE on the schema it is already on — `alembic downgrade` "
        "refuses unconditionally (re-audit `cbb783b` F3).",
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


# --- facts an operator acts on, checked against their real source ----------------------------

# "`022`-compatible image", "**023-compatible** image", "image … 023-COMPATIBLE" — any way of
# naming the compatible IMAGE, in either word order. Scoped to the image claim on purpose:
# "schema-012-compatible" describes a pre-window diagnostic CLI that really does run against
# schema 012, and is not a rollback-image claim at all. `(?<!\d)` keeps a year ("2022-compatible")
# from reading as revision 022.
_COMPAT = r"(?<!\d)(?<!schema-)(\d{3})`?\s*-\s*compatible"
_COMPAT_IMAGE = re.compile(
    rf"{_COMPAT}[^\n]{{0,40}}?image|image[^\n]{{0,40}}?{_COMPAT}", re.IGNORECASE
)


def _compat_number(match: re.Match) -> int:
    """The revision from whichever word order matched."""
    return int(match.group(1) or match.group(2))


@pytest.mark.parametrize("path", [*_ARTIFACTS, *_OPERATOR_DOCS], ids=lambda p: p.name)
def test_compatible_image_names_the_live_head(path):
    """The reviewed image an operator keeps on a refused downgrade must be named for the LIVE
    head, everywhere it is named.

    This replaces an enumerated ban on older numbers, which could only catch numbers someone
    remembered to add and only in the three design artifacts — so when `023` shipped, both
    runbooks went on telling an operator to keep a `022`-compatible image. Comparing against
    the derived head instead fails on any wrong number in either direction, and covers the
    runbooks, which are what gets read at 3am.
    """
    wrong = [
        f"line {i}: {line.strip()}"
        for i, line in enumerate(path.read_text().splitlines(), 1)
        for match in _COMPAT_IMAGE.finditer(line)
        if _compat_number(match) != _HEAD
    ]
    assert not wrong, (
        f"{path.name} names a compatibility set that is not the live head {_HEAD:03d} — an "
        "older image lacks the receipt/terminal contract and must not run against preserved "
        f"evidence; a newer one does not exist yet\n" + "\n".join(wrong)
    )


# digits are legal INSIDE a sentinel suffix — the old charset (`[A-Z][A-Z_]+`) silently
# truncated at the first digit, so a hypothetical `..._V2` sentinel indexed as its own prefix
_SENTINEL = re.compile(r"MIGRATION_\d{3}_[A-Z][A-Z0-9_]*[A-Z0-9]")
# `MIGRATION_0{18,19}_…` — a shell-brace contraction. It reads fine and greps for nothing.
_CONTRACTED_SENTINEL = re.compile(r"MIGRATION_\d*\{")
_RUNBOOK = REPO_ROOT / "docs" / "RUNBOOK.md"
# The one deliberate migration stop that is not MIGRATION_-prefixed: 013's fail-closed
# missing-mapping refusal, shared with the pre-window diagnostic CLI.
_EXTRA_SENTINELS = {"BLOCKED_NO_AUTHORITATIVE_MAPPING"}


def _resolve_module_raises(source_text: str, imported: dict[str, str]) -> tuple[set[str], int]:
    """(sentinels credited to a raise, count of unsentinelled raises) for ONE module's source.

    Sentinels are credited to a `raise` only from string LITERALS or from module-level string
    constants a raise interpolates — never from a name REBOUND in any non-module scope, since
    without full dataflow we cannot know which binding that raise sees (re-audits `45cc215` F8,
    `f495de8`/`8377440` F9). Factored out so the binding-shadow regressions can drive crafted
    source through the exact resolver the live inventory uses.
    """
    import ast

    tree = ast.parse(source_text)
    # MODULE-LEVEL string constants only (tree.body, not ast.walk).
    consts = dict(imported)
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            consts[node.targets[0].id] = node.value.value
    # Any name bound by ANY form inside a non-module scope is TAINTED everywhere: parameter,
    # Assign, AnnAssign, walrus, comprehension/for target, or local import. The one exception is
    # a local import of a REAL sentinel from the contract module (013 does exactly this:
    # `from ...v013_backfill import BLOCKED_SENTINEL` inside upgrade()) — that binds the name to
    # the genuine sentinel value, so it is CREDITED, not tainted; every other local import rebinds
    # the name to something else and is a shadow.
    tainted: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            continue
        args = node.args
        for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg):
            if arg is not None:
                tainted.add(arg.arg)
        for sub in ast.walk(node):
            if isinstance(sub, ast.Assign):
                tainted.update(t.id for t in sub.targets if isinstance(t, ast.Name))
            elif isinstance(sub, ast.AnnAssign | ast.NamedExpr) and isinstance(sub.target, ast.Name):
                tainted.add(sub.target.id)
            elif isinstance(sub, ast.comprehension):
                tainted.update(n.id for n in ast.walk(sub.target) if isinstance(n, ast.Name))
            elif isinstance(sub, ast.ImportFrom):
                for alias in sub.names:
                    bound = alias.asname or alias.name
                    if alias.name in imported and (sub.module or "").startswith(
                        "kyc_tool.migration_contracts"
                    ):
                        consts.setdefault(bound, imported[alias.name])  # the real sentinel
                    else:
                        tainted.add(bound)
            elif isinstance(sub, ast.Import):
                tainted.update((a.asname or a.name.split(".")[0]) for a in sub.names)
            elif isinstance(sub, ast.For):
                tainted.update(n.id for n in ast.walk(sub.target) if isinstance(n, ast.Name))
    raised: set[str] = set()
    plain = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Raise):
            continue
        parts: list[str] = []
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                parts.append(sub.value)
            elif isinstance(sub, ast.Name) and sub.id in consts and sub.id not in tainted:
                parts.append(consts[sub.id])
        found = (
            set(_SENTINEL.findall(" ".join(parts)))
            | (set(parts) & _EXTRA_SENTINELS)
            | {p for p in parts for x in _EXTRA_SENTINELS if x in p}
        )
        if found:
            raised |= found
        else:
            plain += 1
    return raised, plain


def _migration_raise_inventory() -> tuple[set[str], dict[str, int]]:
    """(sentinels that appear in an actual `raise`, unsentinelled-raise count per file), by AST.

    The lexical scan this replaces proved a token EXISTS in the file, not that any refusal
    raises it: an unused constant minted a phantom "documented refusal", and a deliberate
    RuntimeError with no sentinel at all was invisible (re-audit `45cc215` F8).
    """
    from kyc_tool.migration_contracts import v013_backfill

    imported = {
        name: value
        for name, value in vars(v013_backfill).items()
        if isinstance(value, str) and (_SENTINEL.fullmatch(value) or value in _EXTRA_SENTINELS)
    }
    raised: set[str] = set()
    plain: dict[str, int] = {}
    for source in sorted((REPO_ROOT / "alembic" / "versions").glob("*.py")):
        module_raised, module_plain = _resolve_module_raises(source.read_text(), imported)
        raised |= module_raised
        if module_plain:
            plain[source.name] = module_plain
    return raised, plain


# Frozen migrations that predate the sentinel discipline, with their EXACT unsentinelled
# deliberate-raise counts. Equality is the contract: a new unsentinelled raise ANYWHERE fails
# (add a sentinel instead), and so does any change to these counts — these files are published
# and must not move.
_FROZEN_PLAIN_RAISES = {
    "010_hmac_v2_per_case_idempotency.py": 1,
    "011_policy_bundle_pinning.py": 1,
    "013_outbox_stream_separation.py": 5,
}


def test_every_migration_refusal_carries_a_sentinel_except_the_frozen_three():
    _, plain = _migration_raise_inventory()
    assert plain == _FROZEN_PLAIN_RAISES, (
        f"unsentinelled migration raise counts changed: {plain} != {_FROZEN_PLAIN_RAISES}. A new "
        "deliberate refusal must raise a stable MIGRATION_NNN_* sentinel (and be indexed in the "
        "runbook); the frozen three are published and may not change"
    )


# A module-level sentinel constant, then a raise that a LOCAL binding shadows. Each variant binds
# `_SENTINEL` to plain text inside a function by a different form; the resolver must NOT credit
# the module value — the raise must be classified plain (re-audit `8377440` F9).
_SHADOW_PROLOGUE = "_SENTINEL = 'MIGRATION_099_REFUSED_FROZEN'\n"


@pytest.mark.parametrize("shadow", [
    "    _SENTINEL = 'plain'.strip()",                 # local Assign
    "    _SENTINEL: str = 'plain'",                    # AnnAssign
    "    if (_SENTINEL := 'plain'):\n        pass",    # walrus
    "    for _SENTINEL in ['plain']:\n        pass",   # for-target
    "    _x = [_SENTINEL for _SENTINEL in ['plain']]", # comprehension target
    "    from os import getcwd as _SENTINEL",          # local import
], ids=["assign", "annassign", "walrus", "for", "comprehension", "import"])
def test_a_local_shadow_of_a_sentinel_name_is_never_credited(shadow):
    src = (
        f"{_SHADOW_PROLOGUE}"
        "def upgrade():\n"
        f"{shadow}\n"
        "    raise RuntimeError(_SENTINEL)\n"
    )
    raised, plain = _resolve_module_raises(src, {})
    assert "MIGRATION_099_REFUSED_FROZEN" not in raised, (
        "a name rebound in a local scope must not credit the module sentinel"
    )
    assert plain == 1, "the shadowed raise must be classified plain (no literal sentinel)"


def test_a_parameter_shadow_of_a_sentinel_name_is_never_credited():
    src = (
        f"{_SHADOW_PROLOGUE}"
        "def _helper(_SENTINEL):\n"
        "    raise RuntimeError(_SENTINEL)\n"
    )
    raised, plain = _resolve_module_raises(src, {})
    assert "MIGRATION_099_REFUSED_FROZEN" not in raised
    assert plain == 1


def test_a_literal_sentinel_at_the_raise_is_credited():
    """The positive control: an actual string literal at the raise site IS a documented refusal."""
    src = "def upgrade():\n    raise RuntimeError('MIGRATION_099_REFUSED_FROZEN: bad')\n"
    raised, plain = _resolve_module_raises(src, {})
    assert raised == {"MIGRATION_099_REFUSED_FROZEN"} and plain == 0


def test_a_module_constant_a_raise_interpolates_is_credited():
    """And a module-level constant a raise references IS credited (the common real shape)."""
    src = (
        "_S = 'MIGRATION_099_REFUSED_FROZEN'\n"
        "def upgrade():\n    raise RuntimeError(f'{_S}: bad')\n"
    )
    raised, _ = _resolve_module_raises(src, {})
    assert raised == {"MIGRATION_099_REFUSED_FROZEN"}


def test_runbook_indexes_every_migration_refusal_sentinel():
    """The runbook's sentinel index covers every sentinel a migration ACTUALLY RAISES.

    Sentinels exist so a refused upgrade/downgrade reads as a designed stop rather than as a
    broken migration — which only works if the operator can find the string. When this check
    was written, ALL of the upgrade-side sentinels (manifest mismatches, the live-claim
    preflights, the unsendable-rows preflight) appeared in no operator document at all: a
    refusal an operator could hit in a maintenance window with nothing to look up.
    """
    raised, _ = _migration_raise_inventory()
    runbook = _RUNBOOK.read_text()
    documented = set(_SENTINEL.findall(runbook)) | {s for s in _EXTRA_SENTINELS if s in runbook}
    missing = sorted(raised - documented)
    assert not missing, (
        "RUNBOOK.md § 'Migration refusal sentinels' does not index every refusal a migration "
        f"can raise — an operator who hits one of these has nothing to look up:\n{missing}"
    )


@pytest.mark.parametrize("path", _OPERATOR_DOCS, ids=lambda p: p.name)
def test_operator_docs_name_no_unraisable_sentinel(path):
    """The converse: a sentinel no migration RAISES is a typo, a leftover, or an unused
    constant's phantom — each sends the operator looking for a string that will never appear.
    Raised-in-a-raise (AST), not merely present-in-the-file, is what makes an unused constant
    unable to license a doc mention."""
    raised, _ = _migration_raise_inventory()
    phantom = sorted(set(_SENTINEL.findall(path.read_text())) - raised)
    assert not phantom, (
        f"{path.name} names sentinel(s) no migration raises: {phantom}"
    )


@pytest.mark.parametrize("path", _OPERATOR_DOCS, ids=lambda p: p.name)
def test_operator_docs_spell_sentinels_out(path):
    """`MIGRATION_0{18,19,20,21,22}_DOWNGRADE_REFUSED_FORWARD_ONLY` is five sentinels an
    operator cannot grep for, and it is invisible to both checks above — which is how
    DEPLOYMENT.md came to document the forward-only refusals without naming any of them."""
    contracted = [
        f"line {i}: {line.strip()}"
        for i, line in enumerate(path.read_text().splitlines(), 1)
        if _CONTRACTED_SENTINEL.search(line)
    ]
    assert not contracted, (
        f"{path.name} contracts sentinel names with braces; spell each one out so a refused "
        "command's output can be grepped against this document\n" + "\n".join(contracted)
    )


# Governance documents whose LIVE text states the 7b-core range. The three design artifacts get
# the full ban list; these get the one derived fact that drifted across ALL of them when `023`
# shipped: the range "`013`-`0NN`" kept saying `022` (re-audit `45cc215` F6).
_RANGE_DOCS = [
    REPO_ROOT / ".agents" / "ROADMAP.md",
    REPO_ROOT / "docs" / "architecture-decisions.md",
    REPO_ROOT / "AUDIT_FINDINGS.md",
    *_ARTIFACTS,
    *_OPERATOR_DOCS,
]
# `013`-`022`, 013-022, `013` - `022` … — any spelling of the core range.
_CORE_RANGE = re.compile(r"`?013`?\s*[-–]\s*`?0(\d\d)`?")
# Everything above a spec's first revision-note heading is its LIVE claims; below is history.
_REVISION_HISTORY = re.compile(r"^#{1,3}\s*Revision note", re.MULTILINE)


def _live_section(text: str) -> str:
    marker = _REVISION_HISTORY.search(text)
    return text[: marker.start()] if marker else text


@pytest.mark.parametrize("path", _RANGE_DOCS, ids=lambda p: p.name)
def test_core_range_claims_name_the_live_head(path):
    """Everywhere live text states the 7b-core range as `013`-`0NN`, NN is the live head.

    Enumerated bans could never keep up with this one: the range appears in ROADMAP prose, the
    ADR, AUDIT_FINDINGS, both specs, the plan and both runbooks, and every release moves it.
    Historical text below a spec's first revision-note heading is exempt — history is allowed
    to say what was true."""
    live = _live_section(path.read_text())
    wrong = [
        f"line {i}: {line.strip()[:110]}"
        for i, line in enumerate(live.splitlines(), 1)
        for m in _CORE_RANGE.finditer(line)
        if int(m.group(1)) != _HEAD
    ]
    assert not wrong, (
        f"{path.name} states the 7b-core range with an end other than the live head "
        f"{_HEAD:03d}\n" + "\n".join(wrong)
    )


def test_derived_bans_track_the_roadmap():
    """The derivation itself, pinned: the bans must move with §C.

    Without this, a §C parse that silently returned nothing would produce an alternation that
    matches nothing and a gate that passes by vacuity — the same failure mode as the stale
    hand-written list, minus the evidence.
    """
    assert _CORE and list(range(_CORE[0], _CORE[-1] + 1)) == _CORE, (
        f"7b-core's §C reservations are not a contiguous range: {_CORE}"
    )
    assert _CORE[-1] == _HEAD, (
        f"live head {_HEAD:03d} is not 7b-core's last revision {_CORE[-1]:03d} — a later unit "
        "shipped, so the bans below need re-reading, not just re-deriving"
    )
    assert _ACTIVATION == _HEAD + 1, (
        f"activation reserves {_ACTIVATION:03d}, not head+1 ({_HEAD + 1:03d})"
    )
    activation_ban = next(p for p, _why in _DISPROVEN_CLAIMS if "7b-activation|activation" in p)
    # the number activation just vacated is banned; its current one is not
    assert re.search(activation_ban, f"7b-activation is migration `{_HEAD:03d}`", re.IGNORECASE)
    assert not re.search(
        activation_ban, f"7b-activation is migration `{_ACTIVATION:03d}`", re.IGNORECASE
    )
    assert _COMPAT_IMAGE.search(f"the reviewed `{_HEAD - 1:03d}`-compatible image")
    assert _compat_number(_COMPAT_IMAGE.search(f"**{_HEAD:03d}-COMPATIBLE** image")) == _HEAD
    assert _compat_number(_COMPAT_IMAGE.search(f"image is {_HEAD:03d}-compatible")) == _HEAD
    assert not _COMPAT_IMAGE.search("schema-012-compatible, SHARE-locked diagnostic")


@pytest.mark.parametrize("path", _ARTIFACTS, ids=lambda p: p.name)
def test_artifact_has_no_trailing_blank_line(path):
    """`git diff --check` rejects a new blank line at EOF, so appending a revision note with a
    string that already ends in a newline silently breaks a CI gate. Caught here instead."""
    raw = path.read_text()
    assert raw.endswith("\n"), f"{path.name} must end with exactly one newline"
    assert not raw.endswith("\n\n"), (
        f"{path.name} ends with a blank line — `git diff --check` fails on this"
    )
