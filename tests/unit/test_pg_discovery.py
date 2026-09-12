import os
import shutil
import time
from pathlib import Path

import pytest

from tests import pg

REQUIRED_BINARIES = ("initdb", "pg_ctl", "pg_isready", "createdb")


def _executable(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


def _complete_bindir(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    for name in REQUIRED_BINARIES:
        _executable(path / name, body)
    return path


def test_find_pg_bin_discovers_complete_path_installation(tmp_path, monkeypatch):
    bindir = _complete_bindir(tmp_path / "path-postgres" / "bin")
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setattr(pg, "PG_BINDIRS", ())

    assert pg._find_pg_bin() == str(bindir)


def test_find_pg_bin_uses_pg_config_bindir(tmp_path, monkeypatch):
    bindir = _complete_bindir(tmp_path / "configured postgres" / "bin")
    tools = tmp_path / "tools"
    _executable(tools / "pg_config", f"#!/bin/sh\nprintf '%s\\n' '{bindir}'\n")
    monkeypatch.setenv("PATH", str(tools))
    monkeypatch.setattr(pg, "PG_BINDIRS", ())

    assert pg._find_pg_bin() == str(bindir)


def test_find_pg_bin_skips_incomplete_pg_config_bindir_and_uses_fallback(tmp_path, monkeypatch):
    incomplete = tmp_path / "incomplete" / "bin"
    _executable(incomplete / "initdb")
    tools = tmp_path / "tools"
    _executable(tools / "pg_config", f"#!/bin/sh\nprintf '%s\\n' '{incomplete}'\n")
    fallback = _complete_bindir(tmp_path / "fallback" / "bin")
    monkeypatch.setenv("PATH", str(tools))
    monkeypatch.setattr(pg, "PG_BINDIRS", (str(fallback),))

    assert pg._find_pg_bin() == str(fallback)


def test_find_pg_bin_rejects_incomplete_fallback_directory(tmp_path, monkeypatch):
    incomplete = tmp_path / "incomplete" / "bin"
    for name in REQUIRED_BINARIES[:-1]:
        _executable(incomplete / name)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(pg, "PG_BINDIRS", (str(incomplete),))

    with pytest.raises(RuntimeError, match="initdb, pg_ctl, pg_isready, and createdb"):
        pg._find_pg_bin()


def test_find_pg_bin_rejects_path_binaries_split_across_directories(tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"
    for name in REQUIRED_BINARIES[:2]:
        _executable(first / name)
    for name in REQUIRED_BINARIES[2:]:
        _executable(second / name)
    monkeypatch.setenv("PATH", os.pathsep.join((str(first), str(second))))
    monkeypatch.setattr(pg, "PG_BINDIRS", ())

    with pytest.raises(RuntimeError, match="initdb, pg_ctl, pg_isready, and createdb"):
        pg._find_pg_bin()


def test_find_pg_bin_skips_earlier_partial_path_installation(tmp_path, monkeypatch):
    partial = tmp_path / "partial"
    _executable(partial / "initdb")
    complete = _complete_bindir(tmp_path / "complete")
    monkeypatch.setenv("PATH", os.pathsep.join((str(partial), str(complete))))
    monkeypatch.setattr(pg, "PG_BINDIRS", ())

    assert pg._find_pg_bin() == str(complete)


def test_find_pg_bin_bounds_pg_config_probe(tmp_path, monkeypatch):
    marker = tmp_path / "pg-config-ran"
    tools = tmp_path / "tools"
    _executable(
        tools / "pg_config",
        f"#!/bin/sh\n/usr/bin/touch '{marker}'\nexec /bin/sleep 10\n",
    )
    fallback = _complete_bindir(tmp_path / "fallback" / "bin")
    monkeypatch.setenv("PATH", str(tools))
    monkeypatch.setattr(pg, "PG_BINDIRS", (str(fallback),))

    started = time.monotonic()
    assert pg._find_pg_bin() == str(fallback)
    elapsed = time.monotonic() - started

    assert marker.exists()
    assert elapsed < 3


def test_find_pg_bin_reports_one_actionable_error_when_no_installation_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(pg, "PG_BINDIRS", ())

    with pytest.raises(RuntimeError) as caught:
        pg._find_pg_bin()

    message = str(caught.value)
    assert "initdb, pg_ctl, pg_isready, and createdb" in message
    assert "KYC_TEST_DATABASE_URL" in message


def test_external_database_url_bypasses_local_discovery(tmp_path, monkeypatch):
    external_url = "postgresql+psycopg://external.example/kyc_test"
    monkeypatch.setenv("KYC_TEST_DATABASE_URL", external_url)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(pg, "PG_BINDIRS", ())

    cluster = pg.EphemeralPostgres()

    assert cluster.start() == external_url
    cluster.stop()


def test_cluster_commands_quote_executable_directory_with_spaces(tmp_path, monkeypatch):
    log = tmp_path / "commands.log"
    original_path = os.environ.get("PATH", "")
    bindir = _complete_bindir(
        tmp_path / "Postgres 16" / "bin",
        f"#!/bin/sh\nprintf '%s\\n' \"$0\" >> '{log}'\nexit 0\n",
    )
    monkeypatch.delenv("KYC_TEST_DATABASE_URL", raising=False)
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setattr(pg, "PG_BINDIRS", ())
    monkeypatch.setattr(os, "geteuid", lambda: 1000)

    cluster = pg.EphemeralPostgres()
    try:
        assert cluster.start().startswith("postgresql+psycopg://kyc@127.0.0.1:")
    finally:
        try:
            cluster.stop()
            assert cluster._dir is not None
            assert not cluster._dir.exists()
        finally:
            monkeypatch.setenv("PATH", original_path)
            if cluster._dir is not None:
                shutil.rmtree(cluster._dir, ignore_errors=True)

    invoked = log.read_text().splitlines()
    assert invoked == [
        str(bindir / "initdb"),
        str(bindir / "pg_ctl"),
        str(bindir / "pg_isready"),
        str(bindir / "createdb"),
        str(bindir / "pg_ctl"),
    ]
