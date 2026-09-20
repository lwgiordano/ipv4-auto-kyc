"""Behavioral tests for the external source handoff archive."""

import hashlib
import importlib.util
import json
import runpy
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPORTER_PATH = REPO_ROOT / "scripts" / "handoff" / "export.py"


def _load_exporter():
    assert EXPORTER_PATH.is_file(), "the explicit ZIP exporter has not been implemented"
    spec = importlib.util.spec_from_file_location("handoff_export", EXPORTER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def exporter():
    return _load_exporter()


def test_inventory_is_allowlisted_and_overrides_external_documents(exporter):
    tracked = {
        "src/kyc_tool/config.py",
        "alembic/versions/001.py",
        "KYC_Tool_Build_Package/machine_readable/policy.json",
        "KYC_Tool_Build_Package/01_ARCHITECTURE.md",
        "KYC_Tool_Build_Package/00_AGENT_BRIEF.md",
        "docs/RUNBOOK.md",
        "docs/contracts/internal.py",
        "docs/superpowers/plans/internal.md",
        "scripts/dev.sh",
        "scripts/handoff/README.md",
        "scripts/handoff/START-HERE.md",
        "scripts/handoff/.dockerignore",
        "scripts/handoff/manage.sh",
        "scripts/handoff/test_support.py",
        "scripts/handoff/docs/RUNBOOK.md",
        "tests/golden/test_golden_logic.py",
        "tests/unit/test_contract_rendering.py",
        ".agents/ROADMAP.md",
        ".github/workflows/ci.yml",
        "AGENT_BUS.md",
        "AUDIT_FINDINGS.md",
        "ai-kits/agent-substrate-kit/bootstrap.sh",
    }

    selected = exporter.select_inventory(tracked)

    assert selected["src/kyc_tool/config.py"] == "src/kyc_tool/config.py"
    assert selected["alembic/versions/001.py"] == "alembic/versions/001.py"
    assert selected["KYC_Tool_Build_Package/machine_readable/policy.json"] == (
        "KYC_Tool_Build_Package/machine_readable/policy.json"
    )
    assert "KYC_Tool_Build_Package/01_ARCHITECTURE.md" not in selected
    assert selected["scripts/handoff/docs/RUNBOOK.md"] == "docs/RUNBOOK.md"
    assert "docs/RUNBOOK.md" not in selected
    assert selected["scripts/handoff/README.md"] == "README.md"
    assert selected["scripts/handoff/START-HERE.md"] == "START-HERE.md"
    assert selected["scripts/handoff/.dockerignore"] == ".dockerignore"
    assert selected["scripts/handoff/manage.sh"] == "manage.sh"
    assert selected["scripts/handoff/test_support.py"] == "tests/handoff_support.py"
    assert selected["tests/golden/test_golden_logic.py"] == "tests/golden/test_golden_logic.py"

    destinations = set(selected.values())
    assert not destinations & {
        "AGENT_BUS.md",
        "AUDIT_FINDINGS.md",
        ".agents/ROADMAP.md",
        ".github/workflows/ci.yml",
        "docs/contracts/internal.py",
        "docs/superpowers/plans/internal.md",
        "KYC_Tool_Build_Package/00_AGENT_BRIEF.md",
        "tests/unit/test_contract_rendering.py",
    }


def test_export_transformations_are_narrow_and_python_safe(exporter):
    original = (
        b'"""Codex re-audit found the regression."""\n'
        b"# Claude follow-up\n"
        b"from pathlib import Path\n"
        b'ROOT = Path(".substrate/state/evidence")\n'
        b"VALUE = 7\n"
    )

    packaged, names = exporter.transform_bytes("src/kyc_tool/config.py", original)

    assert b"Codex" not in packaged
    assert b"Claude" not in packaged
    assert b".substrate" not in packaged
    assert b'Path("var/evidence")' in packaged
    assert names == ["commentary_attribution_removed", "local_state_directory_rebased"]

    expected = original.replace(b".substrate/state", b"var")
    # The export may rewrite docstrings, but the executable tree (including the
    # one declared local-path rebase) must remain identical.
    assert exporter._executable_ast(packaged.decode()) == exporter._executable_ast(expected.decode())

    migration = b'# AUDIT_FINDINGS issue label is part of this immutable migration\nrevision = "001"\n'
    policy = b'{"literal": ".substrate/state", "owner": "Codex"}\n'
    assert exporter.transform_bytes("alembic/versions/001.py", migration) == (migration, [])
    assert exporter.transform_bytes("KYC_Tool_Build_Package/machine_readable/policy.json", policy) == (
        policy,
        [],
    )


def test_commentary_cleanup_never_joins_source_lines(exporter):
    original = (
        b'"""A bounded operation (re-audit\n'
        b"    `f2929f8..6a4cd87` F8): malformed input is refused.\n"
        b'Next paragraph stays on its own line."""\n'
    )

    packaged, names = exporter.transform_bytes("src/kyc_tool/example.py", original)

    assert len(packaged.splitlines()) == len(original.splitlines())
    assert b"regression case\n" in packaged
    assert b"regression case    " not in packaged
    assert names == ["commentary_attribution_removed"]


def test_real_config_defaults_are_rebased_without_other_executable_changes(exporter):
    original = (REPO_ROOT / "src" / "kyc_tool" / "config.py").read_bytes()

    packaged, names = exporter.transform_bytes("src/kyc_tool/config.py", original)

    assert b".substrate" not in packaged
    assert b'REPO_ROOT / "var" / "evidence"' in packaged
    assert b'REPO_ROOT / "var" / "poc-emails.log"' in packaged
    assert "local_state_directory_rebased" in names

    expected = original.replace(b'REPO_ROOT / ".substrate" / "state"', b'REPO_ROOT / "var"')
    assert exporter._executable_ast(packaged.decode()) == exporter._executable_ast(expected.decode())


def test_operational_tests_are_rebased_to_the_exported_support_module(exporter):
    original = b"from docs.contracts.authority import hardened, unarrived_sunset\n"

    packaged, names = exporter.transform_bytes("tests/unit/test_config.py", original)

    assert packaged == b"from tests.handoff_support import hardened, unarrived_sunset\n"
    assert names == ["test_support_import_rebased"]


def test_rebased_test_imports_are_sorted_without_touching_application_source(tmp_path, exporter):
    tests = tmp_path / "tests"
    tests.mkdir()
    test_file = tests / "test_config.py"
    test_file.write_text(
        "import pytest\nfrom tests.handoff_support import hardened\nfrom pydantic import ValidationError\n"
    )
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("VALUE=1\n")

    changed = exporter._sort_exported_test_imports(tmp_path)

    assert changed == {"tests/test_config.py"}
    assert test_file.read_text() == (
        "import pytest\nfrom pydantic import ValidationError\nfrom tests.handoff_support import hardened\n"
    )
    assert source.read_text() == "VALUE=1\n"


def test_combined_handoff_uses_the_part_names_referenced_by_start_here(tmp_path, exporter):
    docs = tmp_path / "docs"
    docs.mkdir()
    for name in (
        "PLATFORM_BRIEFING.md",
        "PLATFORM_INTEGRATION.md",
        "DEPLOYMENT.md",
        "RUNBOOK.md",
        "ALERTS.md",
        "SALESFORCE_MAPPING.md",
        "PRODUCTION_READINESS.md",
    ):
        (docs / name).write_text(f"# {name}\n\n## Section\n")

    exporter._write_combined_handoff(tmp_path, label="v1-staging", commit="abc123")

    combined = (docs / "TECHCRAFT_HANDOFF.md").read_text()
    assert "# Part 1: Product and integration briefing" in combined
    assert "# Part 2: Platform integration reference" in combined
    assert "# Part 3: Deployment guide" in combined
    assert "## Contents" in combined
    assert "Section numbers restart in each part" in combined


def test_reviewed_public_document_manifest_matches_current_tree(exporter):
    manifest_path = REPO_ROOT / exporter.DOC_COPY_MANIFEST_PATH
    source_files = {
        path: (REPO_ROOT / path).read_bytes()
        for path in exporter._document_copy_manifest_paths(manifest_path.read_bytes())
    }

    exporter.validate_document_copies(source_files, exporter.select_inventory(source_files))


def test_combined_handoff_builders_share_all_seven_public_parts(tmp_path, exporter):
    docs = tmp_path / "docs"
    docs.mkdir()
    for name in (
        "PLATFORM_BRIEFING.md",
        "PLATFORM_INTEGRATION.md",
        "DEPLOYMENT.md",
        "RUNBOOK.md",
        "ALERTS.md",
        "SALESFORCE_MAPPING.md",
        "PRODUCTION_READINESS.md",
    ):
        (docs / name).write_text(f"# {name}\n\nPublic content for {name}.\n")

    exporter._write_combined_handoff(tmp_path, label="v1-staging", commit="abc123")
    packaged = (docs / "TECHCRAFT_HANDOFF.md").read_text()
    build = runpy.run_path(str(REPO_ROOT / "scripts" / "build_techcraft_handoff.py"))["build"]

    assert build(tmp_path, docs_dir=docs, label="v1-staging", commit="abc123") == packaged
    assert "# Part 7: Production readiness" in packaged
    assert "# PLATFORM_BRIEFING.md" not in packaged


def test_combined_handoff_preserves_indented_code_immediately_after_title(tmp_path, exporter):
    docs = tmp_path / "docs"
    docs.mkdir()
    for name in (
        "PLATFORM_BRIEFING.md",
        "PLATFORM_INTEGRATION.md",
        "DEPLOYMENT.md",
        "RUNBOOK.md",
        "ALERTS.md",
        "SALESFORCE_MAPPING.md",
        "PRODUCTION_READINESS.md",
    ):
        (docs / name).write_text(f"# {name}\n\nPublic content.\n")
    (docs / "PLATFORM_BRIEFING.md").write_text(
        "# Briefing\n\n    python -m kyc_tool\n\tpython -m kyc_worker\n",
        encoding="utf-8",
    )

    exporter._write_combined_handoff(tmp_path, label="v1-staging", commit="abc123")

    combined = (docs / "TECHCRAFT_HANDOFF.md").read_text(encoding="utf-8")
    assert "\n    python -m kyc_tool\n" in combined
    assert "\n\tpython -m kyc_worker\n" in combined
    assert "\npython -m kyc_tool\n" not in combined


def test_archive_build_rejects_stale_reviewed_source_before_output(tmp_path, exporter):
    source_path = "docs/PLATFORM_BRIEFING.md"
    copy_path = "scripts/handoff/docs/PLATFORM_BRIEFING.md"
    public_path = "docs/PLATFORM_BRIEFING.md"
    approved_source = b"# Canonical briefing\n"
    approved_copy = b"# Reviewed public briefing\n"
    manifest = {
        "schema_version": 1,
        "review_workflow": "Review source and public copy together, then update both digests.",
        "documents": [
            {
                "public_path": public_path,
                "copy_path": copy_path,
                "source_paths": [source_path],
                "source_sha256": {source_path: hashlib.sha256(approved_source).hexdigest()},
                "copy_sha256": hashlib.sha256(approved_copy).hexdigest(),
                "rationale": "Public wording removes internal-only context.",
            }
        ],
    }
    source_files = {
        exporter.DOC_COPY_MANIFEST_PATH: (json.dumps(manifest) + "\n").encode(),
        source_path: approved_source + b"A later canonical correction.\n",
        copy_path: approved_copy,
    }
    output = tmp_path / "handoff.zip"

    with pytest.raises(ValueError, match="canonical source digest drift"):
        exporter.write_package(
            source_files=source_files,
            source_modes={path: 0o644 for path in source_files},
            inventory={copy_path: public_path},
            output=output,
            label="v1-staging",
            commit="a" * 40,
        )

    assert not output.exists()


def test_archive_build_requires_review_manifest_closure_over_public_copies(tmp_path, exporter):
    briefing_source = "docs/PLATFORM_BRIEFING.md"
    briefing_copy = "scripts/handoff/docs/PLATFORM_BRIEFING.md"
    readiness_copy = "scripts/handoff/docs/PRODUCTION_READINESS.md"
    source_data = b"# Canonical briefing\n"
    copy_data = b"# Reviewed public briefing\n"
    manifest = {
        "schema_version": 1,
        "review_workflow": "Review source and public copy together, then update both digests.",
        "documents": [
            {
                "public_path": "docs/PLATFORM_BRIEFING.md",
                "copy_path": briefing_copy,
                "source_paths": [briefing_source],
                "source_sha256": {briefing_source: hashlib.sha256(source_data).hexdigest()},
                "copy_sha256": hashlib.sha256(copy_data).hexdigest(),
                "rationale": "Public wording removes internal-only context.",
            }
        ],
    }
    source_files = {
        exporter.DOC_COPY_MANIFEST_PATH: (json.dumps(manifest) + "\n").encode(),
        briefing_source: source_data,
        briefing_copy: copy_data,
        readiness_copy: b"# Production readiness\n",
    }
    output = tmp_path / "handoff.zip"

    with pytest.raises(ValueError, match="does not cover public document copies"):
        exporter.write_package(
            source_files=source_files,
            source_modes={path: 0o644 for path in source_files},
            inventory={
                briefing_copy: "docs/PLATFORM_BRIEFING.md",
                readiness_copy: "docs/PRODUCTION_READINESS.md",
            },
            output=output,
            label="v1-staging",
            commit="a" * 40,
        )

    assert not output.exists()


def test_zip_has_hash_manifest_stable_metadata_and_executable_permissions(tmp_path, exporter):
    files = {
        "Dockerfile": b"FROM scratch\n",
        "scripts/dev.sh": b"#!/bin/sh\necho ok\n",
        "scripts/handoff/manage.sh": b"#!/bin/sh\necho ok\n",
        "src/kyc_tool/__init__.py": b"",
        "tests/policy_driven/test_engine_build_id_guard.py": (
            b'EXPECTED_ENGINE_SOURCE_HASH = "' + b"0" * 64 + b'"\n'
        ),
    }
    modes = {path: (0o755 if path.endswith(".sh") else 0o644) for path in files}
    output = tmp_path / "handoff.zip"

    exporter.write_package(
        source_files=files,
        source_modes=modes,
        output=output,
        label="v0.1-staging",
        commit="a" * 40,
    )

    with zipfile.ZipFile(output) as archive:
        prefix = "kyc-tool-v0.1-staging/"
        names = archive.namelist()
        assert names == sorted(names)
        assert prefix + "MANIFEST.json" in names
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())
        assert stat.S_IMODE(archive.getinfo(prefix + "manage.sh").external_attr >> 16) == 0o755
        assert stat.S_IMODE(archive.getinfo(prefix + "scripts/dev.sh").external_attr >> 16) == 0o755

        manifest = json.loads(archive.read(prefix + "MANIFEST.json"))
        assert manifest["release_stage"] == "staging"
        assert manifest["source_commit"] == "a" * 40
        rows = {row["path"]: row for row in manifest["files"]}
        docker = archive.read(prefix + "Dockerfile")
        assert rows["Dockerfile"]["sha256"] == hashlib.sha256(docker).hexdigest()
        assert rows["manage.sh"]["source_path"] == "scripts/handoff/manage.sh"
        assert (
            "engine_source_hash_rebased"
            in rows["tests/policy_driven/test_engine_build_id_guard.py"]["transformations"]
        )


@pytest.mark.parametrize("label", ["production", "v1-prod", "release", "../staging"])
def test_non_staging_or_unsafe_labels_are_refused(exporter, label):
    with pytest.raises(ValueError, match="staging label"):
        exporter.validate_label(label)


def test_dirty_tracked_files_are_refused_but_untracked_files_are_ignored(tmp_path, exporter):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("committed\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=tmp_path, check=True)

    (tmp_path / ".DS_Store").write_text("junk")
    exporter.require_clean_tracked_tree(tmp_path)

    tracked.write_text("dirty\n")
    with pytest.raises(RuntimeError, match="tracked files are dirty"):
        exporter.require_clean_tracked_tree(tmp_path)


def test_archive_content_scan_rejects_internal_history_and_private_keys(exporter):
    with pytest.raises(ValueError, match="internal build-history reference"):
        exporter.scan_package_text("docs/README.md", b"See the Codex conversation.\n")
    with pytest.raises(ValueError, match="private key"):
        exporter.scan_package_text(
            "secret.pem", b"-----BEGIN PRIVATE KEY-----\nnot-a-key\n-----END PRIVATE KEY-----\n"
        )


def test_archive_content_scan_rejects_internal_pr_labels_in_public_documents(exporter):
    with pytest.raises(ValueError, match="internal build-history reference .*PR 7b-core"):
        exporter.scan_package_text(
            "docs/RUNBOOK.md",
            b"## PR 7b-core cutover: drained maintenance window\n",
        )


def test_excluded_junk_names_never_enter_the_archive(exporter):
    for path in (
        ".DS_Store",
        "src/__pycache__/x.pyc",
        ".env",
        ".venv/bin/python",
        "cache/.pytest_cache/CACHEDIR.TAG",
        "private.pem",
    ):
        assert not exporter.is_safe_archive_path(path)


def test_exported_dockerignore_blocks_local_secrets_and_build_junk():
    rules = (REPO_ROOT / "scripts" / "handoff" / ".dockerignore").read_text().splitlines()
    for required in (".env", ".venv", "dist", "**/__pycache__", "*.pem", "*.key"):
        assert required in rules
    assert "!.env.example" in rules
