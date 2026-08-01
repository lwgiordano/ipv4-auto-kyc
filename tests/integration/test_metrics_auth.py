"""Re-audit `d569a15..4938840` F1: a REJECTED (401) request must not synchronously open a database
session or persist telemetry. The previous "zero-DB" test was vacuous — its `_boom()` sentinel raised
inside `_bump()` and was swallowed by a blanket `except`, so it passed BECAUSE the forbidden write
happened. This test uses a NON-RAISING session-factory spy and asserts the rejected path opens ZERO
sessions (no diagnostic write, no protected query), while the diagnostic counter still advances
in-process. If the DB-backed rejected bump were reintroduced, `spy.calls` would be > 0 and this fails.
"""

import pytest
from fastapi.testclient import TestClient

from kyc_tool.api import auth
from kyc_tool.api.app import create_app

pytestmark = pytest.mark.postgres


class _SpyFactory:
    """Counts how many times a DB session is opened, delegating to the real factory."""

    def __init__(self, real):
        self._real = real
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self._real()


def test_rejected_read_auth_opens_no_session_and_persists_nothing(
    settings, session_factory, policy, clean_db
):
    spy = _SpyFactory(session_factory)
    hardened = settings.model_copy(update={"read_auth_required": True})
    tc = TestClient(create_app(hardened, session_factory=spy, policy=policy))

    spy.calls = 0  # ignore any startup/app-construction sessions
    before = auth.diagnostic_counts().get("rejected", 0)

    # 1. unsigned v1 read under required read-auth
    assert tc.get("/v1/metrics").status_code == 401
    # 2. sticky-v2 (any v2 header present) with an invalid signature — no v1 fallback
    assert tc.get(
        "/v1/metrics", headers={"X-KYC-Key-Id": "k", "X-KYC-Signature-V2": "bad"}
    ).status_code == 401

    assert spy.calls == 0, "a rejected request opened a DB session — the F1 write amplifier is back"
    # the diagnostic signal is preserved, but IN-PROCESS (no DB write)
    assert auth.diagnostic_counts().get("rejected", 0) == before + 2
