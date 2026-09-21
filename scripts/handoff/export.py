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
import zipfile
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path, PurePosixPath

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
    "tests/unit/test_handoff_renderer.py",
    "tests/unit/test_migration_lineage.py",
    "tests/unit/test_package_handoff.py",
    "tests/unit/test_plan_artifact_static.py",
    "tests/unit/test_receiver_state_machine.py",
    "tests/unit/test_restore_cli_contract.py",
    "tests/unit/test_techcraft_handoff_doc.py",
}

TEXT_SUFFIXES = {".cjs", ".html", ".ini", ".json", ".md", ".py", ".sh", ".toml", ".txt"}
IMMUTABLE_PREFIXES = ("alembic/", "KYC_Tool_Build_Package/")
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

    changes: list[str] = []
    if commentary_changed:
        changes.append("commentary_attribution_removed")
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
    # Migrations are an executable, immutable ledger. A historical issue-file
    # label in one migration comment remains byte-exact; model names and hidden
    # collaboration paths are still forbidden there.
    for pattern, kind in PROHIBITED_TEXT:
        if path.startswith("alembic/") and kind == "review history":
            continue
        match = pattern.search(text)
        if match:
            raise ValueError(f"internal build-history reference ({kind}: {match.group(0)!r}) found in {path}")
    if not path.startswith("alembic/") and re.search(r"\bAUDIT_FINDINGS(?:\.md)?\b", text, re.I):
        raise ValueError(f"internal build-history reference (issue file) found in {path}")
    if path.startswith("docs/") and PurePosixPath(path).suffix in {".md", ".html"}:
        match = re.search(r"\bPR[ \t]+\d+[a-z]?(?:-[a-z0-9]+)?\b", text, re.I)
        if match:
            raise ValueError(
                f"internal build-history reference (PR label: {match.group(0)!r}) found in {path}"
            )


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
        if destination in {"README.md", "START-HERE.md"}:
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
                else:
                    sources[path] = "generated:release-documents"
                    transformations[path] = ["generated_from_export_documents"]

    immutable_residuals = [
        {
            "path": path,
            "classification": "byte-exact migration commentary",
        }
        for path, data in packaged.items()
        if path.startswith("alembic/")
        and (b"AUDIT_FINDINGS" in data or re.search(rb"re-audits?", data, re.IGNORECASE))
    ]
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
                "transformations": transformations[path],
            }
        )
    manifest = {
        "schema_version": 1,
        "package": prefix,
        "release_stage": "staging",
        "source_commit": commit,
        "files": rows,
        "declared_residuals": immutable_residuals,
        "source_tests_not_packaged": {
            "reason": "requires development-only document or planning inputs",
            "paths": sorted(NON_OPERATIONAL_TESTS),
        },
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
