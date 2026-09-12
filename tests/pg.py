"""Ephemeral PostgreSQL cluster for tests.

Preference order:
1. KYC_TEST_DATABASE_URL env var (CI provides a service container)
2. A throwaway local cluster via initdb/pg_ctl — run through `su <user>` when
   the test process is root (postgres refuses root), directly otherwise.
"""

import contextlib
import os
import shlex
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path

REQUIRED_PG_BINARIES = ("initdb", "pg_ctl", "pg_isready", "createdb")
PG_CONFIG_TIMEOUT_SECONDS = 1
PG_BINDIRS = (
    "/opt/homebrew/opt/postgresql@16/bin",
    "/opt/homebrew/bin",
    "/usr/local/opt/postgresql@16/bin",
    "/usr/lib/postgresql/16/bin",
    "/usr/lib/postgresql/15/bin",
    "/usr/local/bin",
    "/usr/bin",
)


def _usable_pg_bindir(candidate: str | Path) -> str | None:
    bindir = Path(candidate)
    if all(
        (binary := bindir / name).is_file() and os.access(binary, os.X_OK) for name in REQUIRED_PG_BINARIES
    ):
        return str(bindir)
    return None


def _find_pg_bin() -> str:
    pg_config = shutil.which("pg_config")
    if pg_config:
        try:
            result = subprocess.run(
                [pg_config, "--bindir"],
                check=True,
                capture_output=True,
                text=True,
                timeout=PG_CONFIG_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass
        else:
            if bindir := _usable_pg_bindir(result.stdout.strip()):
                return bindir

    for candidate in os.environ.get("PATH", "").split(os.pathsep):
        if bindir := _usable_pg_bindir(candidate or os.curdir):
            return bindir

    for candidate in PG_BINDIRS:
        if bindir := _usable_pg_bindir(candidate):
            return bindir
    raise RuntimeError(
        "PostgreSQL test tools not found: install one directory containing executable "
        "initdb, pg_ctl, pg_isready, and createdb, or set KYC_TEST_DATABASE_URL"
    )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class EphemeralPostgres:
    def __init__(self) -> None:
        self.external_url = os.environ.get("KYC_TEST_DATABASE_URL")
        self.url = self.external_url or ""
        self._dir: Path | None = None
        self._bin = ""
        self._port = 0
        self._as_user = ""

    def _sh(self, command: str) -> None:
        if self._as_user:
            full = ["su", "-s", "/bin/bash", self._as_user, "-c", command]
        else:
            full = ["/bin/bash", "-c", command]
        subprocess.run(full, check=True, capture_output=True, text=True)

    def start(self) -> str:
        if self.external_url:
            return self.external_url
        self._bin = _find_pg_bin()
        self._port = _free_port()
        self._dir = Path(f"/tmp/kyc-test-pg-{uuid.uuid4().hex[:8]}")
        self._dir.mkdir(parents=True)
        if os.geteuid() == 0:
            self._as_user = "ubuntu"
            subprocess.run(["chown", "-R", "ubuntu:ubuntu", str(self._dir)], check=True)
        data = self._dir / "data"
        initdb = shlex.quote(str(Path(self._bin) / "initdb"))
        pg_ctl = shlex.quote(str(Path(self._bin) / "pg_ctl"))
        pg_isready = shlex.quote(str(Path(self._bin) / "pg_isready"))
        createdb = shlex.quote(str(Path(self._bin) / "createdb"))
        quoted_data = shlex.quote(str(data))
        self._sh(f"{initdb} -D {quoted_data} -U kyc -A trust")
        server_options = shlex.quote(f"-p {self._port} -k {shlex.quote(str(self._dir))} -c fsync=off")
        self._sh(
            f"{pg_ctl} -D {quoted_data} -o {server_options} -l {shlex.quote(str(self._dir / 'log'))} start"
        )
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                self._sh(f"{pg_isready} -h 127.0.0.1 -p {self._port} -U kyc")
                break
            except subprocess.CalledProcessError:
                time.sleep(0.2)
        self._sh(f"{createdb} -h 127.0.0.1 -p {self._port} -U kyc kyc_test")
        self.url = f"postgresql+psycopg://kyc@127.0.0.1:{self._port}/kyc_test"
        return self.url

    def stop(self) -> None:
        if self.external_url or self._dir is None:
            return
        with contextlib.suppress(subprocess.CalledProcessError):
            pg_ctl = shlex.quote(str(Path(self._bin) / "pg_ctl"))
            data = shlex.quote(str(self._dir / "data"))
            self._sh(f"{pg_ctl} -D {data} -m immediate stop")
        shutil.rmtree(self._dir, ignore_errors=True)
