#!/usr/bin/env python3
"""Build the explicit, staging-only TechCraft source handoff ZIP.

Document-copy digests record a completed review; they are not an automatic sync
mechanism. When a canonical source changes, read its diff, update and review the
corresponding public copy, then refresh both digests in ``document-copies.json``.
"""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import importlib.util
import io
import json
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tokenize
import zipfile
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path, PurePosixPath

from markdown_it import MarkdownIt

REPO_ROOT = Path(__file__).resolve().parents[2]
ZIP_TIME = (1980, 1, 1, 0, 0, 0)

ROOT_FILES = {
    ".env.example",
    "Dockerfile",
    "alembic.ini",
    "pyproject.toml",
    "requirements.lock",
}
DEMO_FILES = {"scripts/dev.sh", "scripts/dev_receiver.py", "scripts/devproxy.py"}
PUBLIC_DOCS = {
    "docs/ALERTS.md",
    "docs/DEPLOYMENT.md",
    "docs/PLATFORM_BRIEFING.md",
    "docs/PLATFORM_INTEGRATION.md",
    "docs/RUNBOOK.md",
    "docs/SALESFORCE_MAPPING.md",
    "docs/artifacts/kyc-signer-example.py",
}
# These suites govern the repository's internal document registry, plans, or
# collaboration records. The external archive keeps the product behavior,
# policy, golden, and database tests instead.
NON_OPERATIONAL_TESTS = {
    "tests/roadmap.py",
    "tests/integration/test_rollback_command.py",
    "tests/unit/test_conformance_parity.py",
    "tests/unit/test_contract_registry_authority.py",
    "tests/unit/test_contract_rendering.py",
    "tests/unit/test_document_model.py",
    "tests/unit/test_docs_cutover_parity.py",
    "tests/unit/test_handoff_renderer.py",
    "tests/unit/test_migration_lineage.py",
    "tests/unit/test_package_handoff.py",
    "tests/unit/test_plan_artifact_static.py",
    "tests/unit/test_outbox_ceiling_contract.py",
    "tests/unit/test_receiver_state_machine.py",
    "tests/unit/test_restore_cli_contract.py",
    "tests/unit/test_restore_wording_parity.py",
    "tests/unit/test_runbook_requeue_governance.py",
    "tests/unit/test_techcraft_handoff_doc.py",
}

TEXT_SUFFIXES = {".cjs", ".html", ".ini", ".json", ".md", ".py", ".sh", ".toml", ".txt"}
# Ledgers ship byte-identical: migrations and the frozen migration contracts are pinned by their
# exact bytes (tests/unit/test_migration_contract_v013.py), so no export rewrite may touch them.
LEDGER_PREFIXES = ("alembic/", "src/kyc_tool/migration_contracts/")
IMMUTABLE_PREFIXES = (*LEDGER_PREFIXES, "KYC_Tool_Build_Package/")
DOC_COPY_MANIFEST_PATH = "scripts/handoff/document-copies.json"
DOC_COPY_PREFIX = "scripts/handoff/docs/"
COMMENTARY_REPLACEMENTS = (
    (re.compile(r"\bCodex(?:['’]s)?\b", re.IGNORECASE), "independent review"),
    (re.compile(r"\bClaude(?:['’]s)?\b", re.IGNORECASE), "independent review"),
    (re.compile(r"`?AUDIT_FINDINGS(?:\.md)?`?", re.IGNORECASE), "internal issue record"),
    (
        re.compile(
            r"\b(?:independent review[ \t]+)?re-audits?\b"
            r"(?:[ \t]*\(`?[0-9a-f.]+`?\))?"
            r"(?:[ \t]+`?[0-9a-f]{7,}(?:\.\.[0-9a-f]{7,})?`?)?"
            r"(?:[ \t]+(?:R\d+-)?F\d+(?:/F?\d+)*)?",
            re.IGNORECASE,
        ),
        "regression case",
    ),
    (
        re.compile(
            r"\bindependent review(?:['’]s)?(?:[ \t]+(?:exact|synthetic))?\b",
            re.IGNORECASE,
        ),
        "regression case",
    ),
)
# Review-history labels in shipped comments and docstrings: release work packages ("PR 5a
# §6", "PR 7b-core"), review findings ("re-gate-3 finding 1", "R5-F11", "R-audit-8", "Wave 0")
# and commit ranges. They mean nothing outside the review, so the export removes them from
# comments and docstrings only; code and every other string stay byte-identical, which the
# executable-AST check in `transform_bytes` still proves. The repository keeps its history.
_SP = r"[ \t]+"
_HASH = r"`[0-9a-f]{7,}(?:\.\.[0-9a-f]{7,})?`"
_F = r"F\d+(?:/F?\d+)*"
_PR_LABEL = (
    r"PR ?\d+[a-z]?(?:-(?:core|inputs))?"
    r"(?:" + _SP + r"slice(?:" + _SP + r"\d+)?)?"
    r"(?:[ \t]*\((?:Task|item)" + _SP + r"[\w.]+\))?"
    r"(?:" + _SP + r"§[\w.]+(?:" + _SP + r"(?:step" + _SP + r"[\d.]+|item" + _SP + r"[\w.]+))?)?"
    r"(?:" + _SP + r"supply-chain)?"
)
_FINDING_LABEL = (
    r"(?:"
    r"(?:Wave" + _SP + r"\d+" + _SP + r")?(?:re-gate(?:-\d+)?(?:" + _SP + r"finding" + _SP + r"\d+)?"
    r"|gate" + _SP + r"(?:round|finding" + _SP + r"\d+))"
    r"|R-audit(?:-\d+)?(?:" + _SP + r"finding" + _SP + r"\d+)?"
    r"|R\d+-" + _F
    + r"|Wave" + _SP + r"\d+(?:" + _SP + _F + r")?"
    r"|(?:(?:gate" + _SP + r")?audit" + _SP + r"(?:" + _HASH + _SP + r")?finding" + _SP + r"\d+)"
    r"|regression case(?:-\d+(?:" + _SP + r"finding" + _SP + r"\d+)?|" + _SP + r"(?:finding" + _SP + r"\d+|"
    + _F + r"))(?:" + _SP + r"\(" + _HASH + r"\))?"
    r"|(?:" + _HASH + _SP + r")?(?:finding" + _SP + r"\d+|" + _F + r")(?:" + _SP + r"\(" + _HASH + r"\))?"
    r"(?=[)\s:,.;—-]|$)"
    r"|" + _HASH + r")"
)
_ITEM_LABEL = r"(?:remediation" + _SP + r")?items?" + _SP + r"\d+[A-Z]?(?:[–-]\d+)?"
_ANY_LABEL = rf"(?:{_PR_LABEL}|{_FINDING_LABEL}|{_ITEM_LABEL}|G1\d|regression case)"
_LEAD_LABEL = rf"(?:{_PR_LABEL}|{_FINDING_LABEL}|regression case)"
_LABEL_SEP = r"[ \t]*[,;/&+][ \t]*|" + _SP + r"(?:and|\+)" + _SP
_LABEL_RUN = rf"(?:{_ANY_LABEL})(?:(?:{_LABEL_SEP})(?:{_ANY_LABEL}))*"


def _starts_sentence(match: re.Match) -> bool:
    before = match.string[: match.start()].rstrip()
    return not before or before.endswith((".", "#", '"""', "─", "!", "?"))


def _label_case(match: re.Match) -> str:
    return "Regression case" if _starts_sentence(match) else "regression case"


def _cutover_subject(match: re.Match) -> str:
    return "The callback cutover" if _starts_sentence(match) else "the callback cutover"


def _earlier_pass(match: re.Match) -> str:
    before = match.string[: match.start()]
    if re.search(r"\bthe[ \t]+$", before, re.IGNORECASE):
        return "earlier"
    return "An earlier pass" if _starts_sentence(match) else "an earlier pass"


def _repro(match: re.Match) -> str:
    return "Repro" if _starts_sentence(match) else "repro"


REVIEW_LABEL_RULES = (
    # "F1/F2/F3 suites" → "regression suites"
    (re.compile(r"\b" + _F + r"(?=" + _SP + r"suites?\b)"), "regression"),
    # A label parenthetical opening a line or comment, with trailing punctuation or dash.
    (re.compile(rf"(?m)(^[ \t]*(?:#[ \t]*)?)\({_LABEL_RUN}\)[ \t]*[.,;:—–-]*[ \t]*", re.IGNORECASE), r"\1"),
    # The tail of a parenthetical opened on the previous line: "# `hash` R5-F7): …"
    (re.compile(rf"(?m)(^[ \t]*(?:#[ \t]*)?){_LABEL_RUN}\)", re.IGNORECASE), r"\1regression case)"),
    (re.compile(r"[ \t]*\(G1\d" + _SP + r"semantics\)"), ""),
    # A parenthetical that is only labels, with its leading space.
    (re.compile(rf"[ \t]*\({_LABEL_RUN}\)", re.IGNORECASE), ""),
    (re.compile(r"\bpre-PR ?\d+[a-z]?\b"), "earlier"),
    # A label leading a comment, docstring or line, followed by ":", "." or " —".
    (re.compile(rf"(?<=[#\"─—-] ){_LEAD_LABEL}(?:[:.]| —)[ \t]+", re.IGNORECASE), ""),
    (re.compile(rf'(?<="""){_LEAD_LABEL}(?:[:.]| —)[ \t]+', re.IGNORECASE), ""),
    (re.compile(rf"(?m)^([ \t]*){_LEAD_LABEL}(?:[:.]| —)[ \t]+", re.IGNORECASE), r"\1"),
    # A label opening or closing a parenthetical list.
    (re.compile(rf"\({_LEAD_LABEL}(?:{_LABEL_SEP})", re.IGNORECASE), "("),
    (re.compile(rf"(?:{_LABEL_SEP}){_LEAD_LABEL}\)", re.IGNORECASE), ")"),
    # A bare finding marker before a lowercase word is a list marker ("; F2 the permit").
    (re.compile(r"(?<![-\w])(?:finding" + _SP + r"\d+|" + _F + r")" + _SP + r"(?=[a-z])"), ""),
    (re.compile(r"\bWave" + _SP + r"\d+\b(?!" + _SP + r"(?:gate|F\d))"), _earlier_pass),
    # A label naming the finding a repro belongs to: "(R4-F4 repro a)" → "(repro a)".
    (re.compile(rf"(?:{_FINDING_LABEL}|regression case)" + _SP + r"repro(?=s?\b)", re.IGNORECASE), _repro),
    # Whatever remains in running text.
    (re.compile(_FINDING_LABEL, re.IGNORECASE), _label_case),
    (re.compile(r"\b" + _PR_LABEL + r"\b,?[ \t]*"), ""),
    # The bare work-package name for the callback cutover (DEPLOYMENT §11).
    (re.compile(r"\b7b-core(?=" + _SP + r"never\b)"), _cutover_subject),
    (re.compile(r"\b7b-core\b"), "callback-cutover"),
    (re.compile(r"\bpre-7b\b"), "pre-cutover"),
    (re.compile(r"\bG1\d\b"), ""),
    # Tidy what removal leaves behind.
    (
        re.compile(
            r"(?i)\b(regression case)(?:[ \t]*(?:[+,/;&]|\band\b|\bunder\b)?[ \t]*regression case\b)+"
        ),
        r"\1",
    ),
    (re.compile(r"(?i)\b(regression case)[ \t]*\(regression case(?:[ \t]+repros?)?\)"), r"\1"),
    (re.compile(r"^#[ \t]*[.,;:]*[ \t]*$"), "#"),
)
_TOOL_DIRECTIVE = re.compile(r"\b(?:noqa|type:|pragma)\b")
# Matches `[tool.ruff] line-length`: a rewrite may lengthen a line ("R5-F7" → "regression case"),
# and the package's own lint must still pass.
_COMMENT_WIDTH = 110


def _wrap(line: str, continuation: str) -> list[str]:
    """Split one over-long line at word boundaries; continuation lines start with `continuation`."""
    lines = []
    while len(line) > _COMMENT_WIDTH:
        cut = line.rfind(" ", len(continuation) + 1, _COMMENT_WIDTH + 1)
        if cut <= len(continuation):
            break
        lines.append(line[:cut].rstrip())
        line = continuation + line[cut:].lstrip()
    return [*lines, line]


def remove_review_labels(text: str) -> str:
    """Apply `REVIEW_LABEL_RULES` to comments and docstrings only. A comment carrying a tool
    directive (`noqa`, `type:`, `pragma`) is code to its tool and is left exactly as written."""
    docstrings = set()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                docstrings.add(first.value.lineno)
    line_starts = [0]
    for line in text.splitlines(keepends=True):
        line_starts.append(line_starts[-1] + len(line))
    source_lines = text.splitlines()
    edits = []
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        is_comment = token.type == tokenize.COMMENT
        if not (is_comment or (token.type == tokenize.STRING and token.start[0] in docstrings)):
            continue
        if is_comment and _TOOL_DIRECTIVE.search(token.string):
            continue
        rewritten = token.string
        for pattern, replacement in REVIEW_LABEL_RULES:
            rewritten = pattern.sub(replacement, rewritten)
        if rewritten == token.string:
            continue
        line = source_lines[token.start[0] - 1]
        if is_comment:
            # A comment is one physical line: an overflow continues as a comment at the same column,
            # or at the code's indentation when the comment trails code.
            prefix = line[: token.start[1]]
            indent = prefix if not prefix.strip() else re.match(r"[ \t]*", line).group(0)
            wrapped = _wrap(prefix + rewritten, indent + "# ")
            rewritten = "\n".join([wrapped[0][len(prefix):], *wrapped[1:]])
        else:
            # A docstring line that overflows continues at its own indentation.
            first_column = token.start[1]
            out = []
            for number, piece in enumerate(rewritten.split("\n")):
                lead = " " * first_column if number == 0 else ""
                indent = re.match(r"[ \t]*", piece).group(0) if number else " " * first_column
                pieces = _wrap(lead + piece, indent)
                out.append("\n".join([pieces[0][len(lead):], *pieces[1:]]))
            rewritten = "\n".join(out)
        start = line_starts[token.start[0] - 1] + token.start[1]
        end = line_starts[token.end[0] - 1] + token.end[1]
        edits.append((start, end, rewritten))
    for start, end, rewritten in reversed(edits):
        text = text[:start] + rewritten + text[end:]
    return text



PROHIBITED_TEXT = (
    (re.compile(r"\b(?:Codex|Claude|ChatGPT)\b", re.IGNORECASE), "model name"),
    (
        re.compile(
            r"\b(?:GPT|LLM|language[\s-]+model|(?:AI|machine)[\s-]+generated|"
            r"AI[\s-]+(?:assisted|written)|rival[\s-]+model)\b",
            re.IGNORECASE,
        ),
        "authorship attribution",
    ),
    (
        re.compile(
            r"\b(?:slopmonster|de[\s-]+slop|cleanse|copy[\s-]+pass|reviewed[\s-]+by|"
            r"style[\s-]+checked|(?:tool|model)[\s-]+credits?|review[\s-]+notes?)\b",
            re.IGNORECASE,
        ),
        "copy process",
    ),
    (
        re.compile(r"\.(?:agents|claude|substrate)(?=[/'\"`\s]|$)", re.IGNORECASE),
        "internal path",
    ),
    (re.compile(r"\b(?:AGENT_BUS|CLAUDE\.md|AGENTS\.md)\b", re.IGNORECASE), "internal file"),
    (re.compile(r"\b(?:agent[- ]substrate|superpowers)\b", re.IGNORECASE), "internal tooling"),
    (re.compile(r"\b(?:AI|agent) conversations?\b", re.IGNORECASE), "conversation history"),
    (re.compile(r"\b(?:independent review|re-audits?)\b", re.IGNORECASE), "review history"),
    # Labels `remove_review_labels` strips from comments; anything left is in code or prose that
    # the export must not ship. `roadmap_unit` values are internal identifiers, never displayed.
    (
        re.compile(
            r'(?<!roadmap_unit=")\b(?:PR ?\d+[a-z]?(?:-[a-z0-9]+)?\b|re-gate|[Gg]ate[ \t]+finding'
            r"|R-audit|R\d+-F\d+"
            r"|Wave[ \t]+\d|remediation[ \t]+item|[Rr]egression case-\d|7b-core|pre-7b)"
        ),
        "review history",
    ),
)
PRIVATE_KEY = re.compile(rb"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")


def _is_test_file(path: str) -> bool:
    if not path.startswith("tests/") or path in NON_OPERATIONAL_TESTS:
        return False
    return path.endswith((".py", ".json")) and "/__pycache__/" not in path


def select_inventory(tracked_paths: Iterable[str]) -> dict[str, str]:
    """Return source-path -> archive-path for the explicit external inventory."""
    tracked = set(tracked_paths)
    selected: dict[str, str] = {}
    for path in sorted(tracked):
        include = (
            path in ROOT_FILES
            or path in DEMO_FILES
            or path in PUBLIC_DOCS
            or path.startswith("src/")
            or path.startswith("alembic/")
            or path.startswith("KYC_Tool_Build_Package/machine_readable/")
            or _is_test_file(path)
        )
        if include and is_safe_archive_path(path):
            selected[path] = path

    handoff_files = {
        "scripts/handoff/.dockerignore": ".dockerignore",
        "scripts/handoff/README.md": "README.md",
        "scripts/handoff/START-HERE.md": "START-HERE.md",
        "scripts/handoff/INTEGRATION-SHEET.md": "INTEGRATION-SHEET.md",
        "scripts/handoff/manage.sh": "manage.sh",
        "scripts/handoff/test_support.py": "tests/handoff_support.py",
    }
    for source, destination in handoff_files.items():
        if source in tracked:
            selected[source] = destination

    # Every handoff document is an external-facing copy. It wins over a file
    # with the same destination under docs/.
    prefix = "scripts/handoff/docs/"
    for source in sorted(path for path in tracked if path.startswith(prefix)):
        destination = "docs/" + source.removeprefix(prefix)
        for old_source, old_destination in list(selected.items()):
            if old_destination == destination:
                del selected[old_source]
        if is_safe_archive_path(destination):
            selected[source] = destination

    return selected


def is_safe_archive_path(path: str) -> bool:
    pure = PurePosixPath(path)
    parts = pure.parts
    if not parts or pure.is_absolute() or ".." in parts:
        return False
    if path == ".env" or path.endswith("/.env"):
        return False
    if any(part in {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache"} for part in parts):
        return False
    return not any(part == ".DS_Store" or part.endswith((".pyc", ".pyo", ".pem", ".key")) for part in parts)


class _DocstringStripper(ast.NodeTransformer):
    @staticmethod
    def _strip(body: list[ast.stmt]) -> list[ast.stmt]:
        if body and isinstance(body[0], ast.Expr):
            value = body[0].value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return body[1:]
        return body

    def visit_Module(self, node: ast.Module):  # noqa: N802
        node.body = self._strip(node.body)
        return self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef):  # noqa: N802
        node.body = self._strip(node.body)
        return self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef):  # noqa: N802
        node.body = self._strip(node.body)
        return self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):  # noqa: N802
        node.body = self._strip(node.body)
        return self.generic_visit(node)


def _executable_ast(text: str) -> str:
    tree = _DocstringStripper().visit(copy.deepcopy(ast.parse(text)))
    return ast.dump(ast.fix_missing_locations(tree), include_attributes=False)


def transform_bytes(path: str, data: bytes) -> tuple[bytes, list[str]]:
    """Apply declared export-only text changes, never touching immutable inputs."""
    if path.startswith(IMMUTABLE_PREFIXES) or PurePosixPath(path).suffix not in TEXT_SUFFIXES:
        return data, []
    try:
        original = data.decode("utf-8")
    except UnicodeDecodeError:
        return data, []

    state_rebased = original.replace(".substrate/state", "var").replace(
        'REPO_ROOT / ".substrate" / "state"', 'REPO_ROOT / "var"'
    )
    support_rebased = state_rebased
    if path.startswith("tests/"):
        support_rebased = support_rebased.replace("docs.contracts.authority", "tests.handoff_support")
    transformed = support_rebased
    commentary_changed = False
    for pattern, replacement in COMMENTARY_REPLACEMENTS:
        changed = pattern.sub(replacement, transformed)
        commentary_changed = commentary_changed or changed != transformed
        transformed = changed

    labels_removed = remove_review_labels(transformed) if path.endswith(".py") else transformed

    changes: list[str] = []
    if commentary_changed:
        changes.append("commentary_attribution_removed")
    if labels_removed != transformed:
        changes.append("review_labels_removed")
        transformed = labels_removed
    if state_rebased != original:
        changes.append("local_state_directory_rebased")
    if support_rebased != state_rebased:
        changes.append("test_support_import_rebased")
    if not changes:
        return data, []

    if path.endswith(".py") and _executable_ast(transformed) != _executable_ast(support_rebased):
        raise ValueError(f"commentary cleanup changed executable Python AST: {path}")
    return transformed.encode("utf-8"), changes


def _framed_source_hash(files: Mapping[str, bytes]) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in files if p.startswith("src/kyc_tool/") and p.endswith(".py")):
        relative = path.removeprefix("src/kyc_tool/").encode()
        data = files[path]
        digest.update(relative + b"\0" + str(len(data)).encode() + b"\0" + data)
    return digest.hexdigest()


def _repin_engine_guard(files: dict[str, bytes], transformations: dict[str, list[str]]) -> None:
    guard = "tests/policy_driven/test_engine_build_id_guard.py"
    if guard not in files or not any(path.startswith("src/kyc_tool/") for path in files):
        return
    digest = _framed_source_hash(files)
    text = files[guard].decode("utf-8")
    replaced, count = re.subn(
        r'EXPECTED_ENGINE_SOURCE_HASH = "[0-9a-f]{64}"',
        f'EXPECTED_ENGINE_SOURCE_HASH = "{digest}"',
        text,
    )
    if count != 1:
        raise ValueError(f"expected one engine source hash pin in {guard}, found {count}")
    if replaced != text:
        files[guard] = replaced.encode("utf-8")
        transformations.setdefault(guard, []).append("engine_source_hash_rebased")


def validate_label(label: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", label):
        raise ValueError("staging label must use only letters, numbers, dots, underscores, or hyphens")
    lowered = label.lower()
    if "staging" not in lowered or "prod" in lowered:
        raise ValueError("staging label must contain 'staging' and must not contain 'prod'")
    return label


def scan_package_text(path: str, data: bytes) -> None:
    if PRIVATE_KEY.search(data):
        raise ValueError(f"private key material found in {path}")
    if PurePosixPath(path).suffix not in TEXT_SUFFIXES:
        return
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return
    # Migrations and frozen migration contracts are an immutable ledger. Historical
    # labels in their comments remain byte-exact; model names and hidden
    # collaboration paths are still forbidden there.
    for pattern, kind in PROHIBITED_TEXT:
        if path.startswith(LEDGER_PREFIXES) and kind == "review history":
            continue
        match = pattern.search(text)
        if match:
            raise ValueError(f"internal build-history reference ({kind}: {match.group(0)!r}) found in {path}")
    if not path.startswith("alembic/") and re.search(r"\bAUDIT_FINDINGS(?:\.md)?\b", text, re.I):
        raise ValueError(f"internal build-history reference (issue file) found in {path}")
    if PurePosixPath(path).suffix in {".md", ".txt", ".html"}:
        match = re.search(r"\bPR[ \t]+\d+[a-z]?(?:-[a-z0-9]+)?\b", text, re.I)
        if match:
            raise ValueError(
                f"internal build-history reference (PR label: {match.group(0)!r}) found in {path}"
            )
        if re.search(r"\bthis\s+is\s+(?:the|a)\s+handoff\b", text, re.I):
            raise ValueError(f"internal build-history reference (handoff narration) found in {path}")


def _parse_document_copy_manifest(data: bytes) -> dict:
    try:
        manifest = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid reviewed document-copy manifest: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ValueError("reviewed document-copy manifest root must be an object")
    if manifest.get("schema_version") != 1:
        raise ValueError("reviewed document-copy manifest must use schema_version 1")
    if not isinstance(manifest.get("review_workflow"), str) or not manifest["review_workflow"].strip():
        raise ValueError("reviewed document-copy manifest requires a review_workflow")
    if not isinstance(manifest.get("documents"), list):
        raise ValueError("reviewed document-copy manifest requires a documents list")
    return manifest


def _document_copy_manifest_paths(data: bytes) -> set[str]:
    manifest = _parse_document_copy_manifest(data)
    paths = {DOC_COPY_MANIFEST_PATH}
    for row in manifest["documents"]:
        if not isinstance(row, dict):
            raise ValueError("reviewed document-copy manifest entries must be objects")
        copy_path = row.get("copy_path")
        source_paths = row.get("source_paths")
        if not isinstance(copy_path, str) or not isinstance(source_paths, list):
            raise ValueError("reviewed document-copy manifest entry paths are invalid")
        paths.add(copy_path)
        paths.update(path for path in source_paths if isinstance(path, str))
    return paths


def validate_document_copies(source_files: Mapping[str, bytes], inventory: Mapping[str, str]) -> None:
    """Reject stale or unreviewed public document copies before archive output."""
    public_copies = {
        source: destination
        for source, destination in inventory.items()
        if source.startswith(DOC_COPY_PREFIX) and destination.startswith("docs/")
    }
    if not public_copies:
        return
    manifest_data = source_files.get(DOC_COPY_MANIFEST_PATH)
    if manifest_data is None:
        raise ValueError("reviewed document-copy manifest is missing")
    manifest = _parse_document_copy_manifest(manifest_data)
    rows: dict[str, dict] = {}
    for row in manifest["documents"]:
        if not isinstance(row, dict):
            raise ValueError("reviewed document-copy manifest entries must be objects")
        copy_path = row.get("copy_path")
        if not isinstance(copy_path, str) or copy_path in rows:
            raise ValueError("reviewed document-copy manifest has an invalid or duplicate copy_path")
        rows[copy_path] = row

    if set(rows) != set(public_copies):
        missing = sorted(set(public_copies) - set(rows))
        extra = sorted(set(rows) - set(public_copies))
        raise ValueError(
            "reviewed document-copy manifest does not cover public document copies: "
            f"missing={missing}, extra={extra}"
        )

    for copy_path, public_path in sorted(public_copies.items()):
        row = rows[copy_path]
        if row.get("public_path") != public_path:
            raise ValueError(f"reviewed public path mismatch for {copy_path}")
        if not isinstance(row.get("rationale"), str) or not row["rationale"].strip():
            raise ValueError(f"review rationale is missing for {copy_path}")
        source_paths = row.get("source_paths")
        source_hashes = row.get("source_sha256")
        if (
            not isinstance(source_paths, list)
            or not source_paths
            or not all(isinstance(path, str) for path in source_paths)
        ):
            raise ValueError(f"source anchors are missing for {copy_path}")
        if not isinstance(source_hashes, dict) or set(source_hashes) != set(source_paths):
            raise ValueError(f"source digest inventory is invalid for {copy_path}")
        for source_path in source_paths:
            data = source_files.get(source_path)
            if data is None:
                raise ValueError(f"canonical source is missing for {copy_path}: {source_path}")
            if hashlib.sha256(data).hexdigest() != source_hashes[source_path]:
                raise ValueError(f"canonical source digest drift for {copy_path}: {source_path}")
        copy_data = source_files.get(copy_path)
        if copy_data is None:
            raise ValueError(f"reviewed public copy is missing: {copy_path}")
        if hashlib.sha256(copy_data).hexdigest() != row.get("copy_sha256"):
            raise ValueError(f"reviewed public copy digest drift for {copy_path}")


def _zip_info(path: str, mode: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(path, date_time=ZIP_TIME)
    info.create_system = 3
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (stat.S_IFREG | stat.S_IMODE(mode)) << 16
    return info


def _load_renderer(path: Path | None) -> Callable[[Path], None] | None:
    if path is None or not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("handoff_render_docs", path)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load document renderer: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    render = getattr(module, "render", None)
    if not callable(render):
        raise RuntimeError(f"document renderer must expose render(root: Path): {path}")
    return render


def _write_combined_handoff(root: Path, *, label: str, commit: str) -> None:
    docs = root / "docs"
    if not docs.is_dir():
        return
    path = Path(__file__).resolve().with_name("assembly.py")
    spec = importlib.util.spec_from_file_location("handoff_assembly", path)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load handoff assembly: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.write_combined_handoff(docs, label=label, commit=commit)


def plain_text_document(source: str) -> str:
    """Render headings, lists and labelled table records without Markdown markup."""
    tokens = MarkdownIt("commonmark", {"html": False}).enable("table").parse(source)

    def inline(children):
        result = []
        links = []
        for token in children or []:
            if token.type in {"text", "code_inline"}:
                result.append(token.content)
            elif token.type in {"softbreak", "hardbreak"}:
                result.append(" " if token.type == "softbreak" else "\n")
            elif token.type == "link_open":
                links.append((token.attrGet("href"), len(result)))
            elif token.type == "link_close":
                url, start = links.pop()
                if url != "".join(result[start:]):
                    result.append(f" ({url})")
            elif token.type == "image":
                result.append(f"{inline(token.children)} ({token.attrGet('src')})")
        return "".join(result)

    chunks = []
    lists = []
    item_prefix = ""
    headers = []
    row = None
    in_header = False
    heading = None
    for token in tokens:
        kind = token.type
        if kind == "heading_open":
            heading = token.tag
        elif kind == "table_open":
            headers = []
        elif kind == "thead_open":
            in_header = True
        elif kind == "thead_close":
            in_header = False
        elif kind == "tr_open":
            row = []
        elif kind == "tr_close":
            if in_header:
                headers = row
            else:
                chunks.append(
                    "\n".join(
                        f"{label}: {value}" if label else value
                        for label, value in zip(headers, row, strict=True)
                    )
                )
            row = None
        elif kind in {"bullet_list_open", "ordered_list_open"}:
            # `start` is 0 for a list that begins at step 0; `or 1` renumbered it and shifted every
            # "step N" reference in the exported text. Only an absent attribute means 1.
            start = token.attrGet("start") if kind == "ordered_list_open" else None
            lists.append((1 if start is None else int(start)) if kind == "ordered_list_open" else None)
        elif kind in {"bullet_list_close", "ordered_list_close"}:
            lists.pop()
        elif kind == "list_item_open":
            number = lists[-1]
            item_prefix = "  " * (len(lists) - 1) + (f"{number}. " if number is not None else "- ")
            if number is not None:
                lists[-1] += 1
        elif kind == "list_item_close":
            item_prefix = ""
        elif kind == "inline":
            content = inline(token.children)
            if row is not None:
                row.append(content)
            elif heading:
                chunks.append(content + "\n" + ("=" if heading == "h1" else "-") * len(content))
                heading = None
            else:
                chunks.append(item_prefix + content)
                item_prefix = ""
        elif kind in {"fence", "code_block"}:
            # Preserve shell continuations, indentation and literal markup exactly.
            chunks.append(token.content.removesuffix("\n"))
        elif kind == "hr":
            chunks.append("----------------------------------------")
    return "\n\n".join(chunks) + "\n"


def _prepare_public_documents(root: Path) -> None:
    names = {path.name for path in root.rglob("*.md")}
    # Only known document names, never a broad extension substitution in code.
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in {".md", ".sh", ".toml"}:
            continue
        text = path.read_text(encoding="utf-8")
        for name in sorted(names, key=len, reverse=True):
            text = re.sub(rf"(?<![\w.-]){re.escape(name)}(?![\w-])", name[:-3] + ".txt", text)
        path.write_text(text, encoding="utf-8")


def _sort_exported_test_imports(root: Path) -> set[str]:
    tests = root / "tests"
    if not tests.is_dir():
        return set()
    before = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).digest()
        for path in tests.rglob("*.py")
    }
    bundled = Path(sys.executable).with_name("ruff")
    ruff = bundled if bundled.is_file() else Path(shutil.which("ruff") or "")
    if not ruff.is_file():
        raise RuntimeError("ruff is required to sort exported test imports")
    result = subprocess.run(
        [str(ruff), "check", "--select", "I", "--fix", "--no-cache", str(tests)],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(f"could not sort exported test imports: {result.stdout}{result.stderr}")
    return {
        path
        for path, digest in before.items()
        if hashlib.sha256((root / path).read_bytes()).digest() != digest
    }


def write_package(
    *,
    source_files: Mapping[str, bytes],
    source_modes: Mapping[str, int],
    output: Path,
    label: str,
    commit: str,
    inventory: Mapping[str, str] | None = None,
    renderer: Callable[[Path], None] | None = None,
) -> Path:
    label = validate_label(label)
    prefix = f"kyc-tool-{label}"
    inventory = inventory or select_inventory(source_files)
    validate_document_copies(source_files, inventory)
    packaged: dict[str, bytes] = {}
    modes: dict[str, int] = {}
    sources: dict[str, str] = {}
    transformations: dict[str, list[str]] = {}

    for source, destination in inventory.items():
        if source not in source_files:
            raise ValueError(f"inventory source is missing: {source}")
        if destination in packaged:
            raise ValueError(f"duplicate archive destination: {destination}")
        data, changes = transform_bytes(destination, source_files[source])
        if destination in {"README.md", "START-HERE.md", "INTEGRATION-SHEET.md"}:
            substituted = data.replace(b"__VERSION__", label.encode()).replace(b"__COMMIT__", commit.encode())
            if substituted != data:
                changes.append("release_metadata_substituted")
                data = substituted
        packaged[destination] = data
        modes[destination] = source_modes.get(source, 0o644)
        sources[destination] = source
        transformations[destination] = changes

    _repin_engine_guard(packaged, transformations)

    with tempfile.TemporaryDirectory(prefix="kyc-handoff-") as temporary:
        root = Path(temporary) / prefix
        for path, data in packaged.items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(stat.S_IMODE(modes[path]))

        _write_combined_handoff(root, label=label, commit=commit)
        _prepare_public_documents(root)
        sorted_test_paths: set[str] = set()
        if any("test_support_import_rebased" in changes for changes in transformations.values()):
            sorted_test_paths = _sort_exported_test_imports(root)
        if renderer is not None:
            renderer(root)
        after = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
        for path in sorted(after):
            target = root / path
            if not target.is_file() or target.is_symlink() or not is_safe_archive_path(path):
                raise ValueError(f"document build produced an unsafe path: {path}")
            current = target.read_bytes()
            if packaged.get(path) != current:
                packaged[path] = current
                modes[path] = stat.S_IMODE(target.stat().st_mode)
                if path in sorted_test_paths:
                    transformations[path].append("test_imports_sorted")
                elif path in sources:
                    transformations[path].append("public_document_references_updated")
                else:
                    sources[path] = "generated:release-documents"
                    transformations[path] = ["generated_from_export_documents"]

    # Markdown is an authoring input only. Rendered guides have already been
    # built from it; the archive contains readable plain text instead.
    for path in list(packaged):
        if not path.endswith(".md"):
            continue
        destination = path[:-3] + ".txt"
        if destination in packaged:
            raise ValueError(f"plain-text document destination already exists: {destination}")
        packaged[destination] = plain_text_document(packaged.pop(path).decode("utf-8")).encode("utf-8")
        modes[destination] = modes.pop(path)
        sources[destination] = sources.pop(path)
        transformations[destination] = transformations.pop(path) + ["plain_text_document"]

    for path, data in packaged.items():
        if not is_safe_archive_path(path):
            raise ValueError(f"unsafe archive path: {path}")
        scan_package_text(path, data)

    rows = []
    for path in sorted(packaged):
        data = packaged[path]
        source = sources[path]
        source_data = source_files.get(source)
        rows.append(
            {
                "path": path,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
                "mode": f"{stat.S_IMODE(modes[path]):04o}",
                "source_path": source,
                "source_sha256": hashlib.sha256(source_data).hexdigest() if source_data is not None else None,
            }
        )
    manifest = {
        "schema_version": 2,
        "package": prefix,
        "release_stage": "staging",
        "source_commit": commit,
        "files": rows,
    }
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    scan_package_text("MANIFEST.json", manifest_bytes)

    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    entries = {f"{prefix}/{path}": (data, modes[path]) for path, data in packaged.items()}
    entries[f"{prefix}/MANIFEST.json"] = (manifest_bytes, 0o644)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(entries):
            data, mode = entries[path]
            archive.writestr(_zip_info(path, mode), data)
    return output


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True).stdout


def require_clean_tracked_tree(repo: Path) -> None:
    dirty = _git(repo, "status", "--porcelain=v1", "--untracked-files=no").decode().strip()
    if dirty:
        raise RuntimeError(f"tracked files are dirty; commit the handoff source first:\n{dirty}")


def _tracked_commit_files(repo: Path) -> tuple[dict[str, bytes], dict[str, int]]:
    files: dict[str, bytes] = {}
    modes: dict[str, int] = {}
    listing = _git(repo, "ls-tree", "-r", "HEAD").decode().splitlines()
    tree_modes: dict[str, int] = {}
    for line in listing:
        metadata, path = line.split("\t", 1)
        mode, kind, _object = metadata.split()
        if kind == "blob" and mode in {"100644", "100755"}:
            tree_modes[path] = int(mode[-3:], 8)
    if DOC_COPY_MANIFEST_PATH not in tree_modes:
        raise RuntimeError(f"reviewed document-copy manifest is not tracked: {DOC_COPY_MANIFEST_PATH}")
    manifest_data = _git(repo, "show", f"HEAD:{DOC_COPY_MANIFEST_PATH}")
    validation_sources = _document_copy_manifest_paths(manifest_data)
    missing_sources = sorted(validation_sources - set(tree_modes))
    if missing_sources:
        raise RuntimeError(f"reviewed document-copy inputs are not tracked: {missing_sources}")
    selected_sources = sorted(set(select_inventory(tree_modes)) | validation_sources)
    if not selected_sources:
        return files, modes
    archive_bytes = _git(repo, "archive", "--format=tar", "HEAD", "--", *selected_sources)
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:") as archive:
        for member in archive.getmembers():
            if not member.isfile() or member.name not in tree_modes:
                continue
            extracted = archive.extractfile(member)
            if extracted is None:
                raise RuntimeError(f"could not read committed file: {member.name}")
            files[member.name] = extracted.read()
            modes[member.name] = tree_modes[member.name]
    return files, modes


def build_from_repo(repo: Path, *, label: str, output: Path | None = None) -> Path:
    require_clean_tracked_tree(repo)
    commit = _git(repo, "rev-parse", "HEAD").decode().strip()
    files, modes = _tracked_commit_files(repo)
    inventory = select_inventory(files)
    output = output or repo / "dist" / f"kyc-tool-{label}.zip"
    renderer = _load_renderer(repo / "scripts" / "handoff" / "render_docs.py")
    return write_package(
        source_files=files,
        source_modes=modes,
        inventory=inventory,
        output=output,
        label=label,
        commit=commit,
        renderer=renderer,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("label", help="staging release label, for example v0.1.2-staging")
    parser.add_argument("--output", type=Path, help="ZIP path (default: dist/kyc-tool-LABEL.zip)")
    args = parser.parse_args()
    try:
        result = build_from_repo(REPO_ROOT, label=args.label, output=args.output)
    except (RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"error: {exc}\n")
    print(result)


if __name__ == "__main__":
    main()
