"""Ephemeral PostgreSQL cluster for tests.

Preference order:
1. KYC_TEST_DATABASE_URL env var (CI provides a service container)
2. A throwaway local cluster via initdb/pg_ctl — run through `su <user>` when
   the test process is root (postgres refuses root), directly otherwise.
"""

import contextlib
import os
import socket
import subprocess
import time
import uuid
from pathlib import Path

PG_BINDIRS = ("/usr/lib/postgresql/16/bin", "/usr/lib/postgresql/15/bin", "/usr/local/bin", "/usr/bin")


def _find_pg_bin() -> str:
    for candidate in PG_BINDIRS:
        if (Path(candidate) / "initdb").exists():
            return candidate
    raise RuntimeError("PostgreSQL binaries not found; set KYC_TEST_DATABASE_URL instead")


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
        self._sh(f"{self._bin}/initdb -D {data} -U kyc -A trust")
        self._sh(
            f"{self._bin}/pg_ctl -D {data} -o '-p {self._port} -k {self._dir} -c fsync=off' "
            f"-l {self._dir}/log start"
        )
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                self._sh(f"{self._bin}/pg_isready -h 127.0.0.1 -p {self._port} -U kyc")
                break
            except subprocess.CalledProcessError:
                time.sleep(0.2)
        self._sh(f"{self._bin}/createdb -h 127.0.0.1 -p {self._port} -U kyc kyc_test")
        self.url = f"postgresql+psycopg://kyc@127.0.0.1:{self._port}/kyc_test"
        return self.url

    def stop(self) -> None:
        if self.external_url or self._dir is None:
            return
        with contextlib.suppress(subprocess.CalledProcessError):
            self._sh(f"{self._bin}/pg_ctl -D {self._dir}/data -m immediate stop")
        subprocess.run(["rm", "-rf", str(self._dir)], check=False)
