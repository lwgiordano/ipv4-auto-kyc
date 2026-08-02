"""Re-audit `d569a15..4938840` F1: a REJECTED (401) request must not synchronously open a database
session or persist telemetry. The previous "zero-DB" test was vacuous — its `_boom()` sentinel raised
inside `_bump()` and was swallowed by a blanket `except`, so it passed BECAUSE the forbidden write
happened. This test uses a NON-RAISING session-factory spy and asserts the rejected path opens ZERO
sessions (no diagnostic write, no protected query), while the diagnostic counter still advances
in-process. If the DB-backed rejected bump were reintroduced, `spy.calls` would be > 0 and this fails.
"""

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from kyc_tool.api import auth
from kyc_tool.api.app import create_app
from tests.conftest import sign_headers

pytestmark = pytest.mark.postgres

_PAST_SUNSET = "2020-01-01T00:00:00+00:00"
_FUTURE_SUNSET = "2099-01-01T00:00:00+00:00"


def _read_auth_app(settings, spy, policy, **overrides):
    hardened = settings.model_copy(update={"read_auth_required": True, **overrides})
    return TestClient(create_app(hardened, session_factory=spy, policy=policy))


def _seed_green_witness(session_factory):
    """Observation active + aged past the window + no recent accept ⇒ inbound_v1_zero() is green."""
    with session_factory() as s:
        s.execute(
            text("UPDATE hmac_v1_observation SET observation_started_at = :t WHERE id = 1"),
            {"t": dt.datetime(2024, 1, 1, tzinfo=dt.UTC)},
        )
        s.commit()


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


def test_invalid_v1_opens_no_db_regardless_of_sunset(settings, session_factory, policy, clean_db):
    """Re-audit `8aba2df..2cee937` R3-F1: an invalid/unsigned v1 request must open ZERO sessions
    whether the sunset is future OR past. Previously a PAST sunset drove the durable zero-witness
    SELECT before `security.verify()`, so an unauthenticated flood could amplify DB reads once the
    date passed. Moving the witness read ahead of verification again makes spy.calls > 0 here."""
    for sunset in (_FUTURE_SUNSET, _PAST_SUNSET):
        spy = _SpyFactory(session_factory)
        tc = _read_auth_app(
            settings, spy, policy,
            hmac_v1_inbound_sunset_at=sunset, hmac_v1_observation_window_days=1,
        )
        spy.calls = 0
        r = tc.get("/v1/metrics", headers={"X-KYC-Timestamp": "1", "X-KYC-Signature": "bad"})
        assert r.status_code == 401
        assert spy.calls == 0, f"invalid v1 opened a DB session (sunset={sunset}) — F1 amplifier back"


def test_valid_v1_is_retired_only_after_verification_with_a_green_witness(
    settings, session_factory, policy, clean_db
):
    """A CRYPTOGRAPHICALLY VALID v1 request consults the witness: past sunset + green witness ⇒
    retired. The witness read is reachable only because the signature verified first."""
    _seed_green_witness(session_factory)
    spy = _SpyFactory(session_factory)
    tc = _read_auth_app(
        settings, spy, policy,
        hmac_v1_inbound_sunset_at=_PAST_SUNSET, hmac_v1_observation_window_days=1,
    )
    r = tc.get("/v1/metrics", headers=sign_headers(b""))
    assert r.status_code == 401
    assert "retired" in r.json().get("detail", "")
    assert spy.calls >= 1  # the VALID request did consult the witness


def test_valid_v1_is_accepted_when_not_retired(settings, session_factory, policy, clean_db):
    """Valid v1 + future sunset ⇒ accepted (not retired); the happy path is unchanged."""
    spy = _SpyFactory(session_factory)
    tc = _read_auth_app(
        settings, spy, policy,
        hmac_v1_inbound_sunset_at=_FUTURE_SUNSET, hmac_v1_observation_window_days=1,
    )
    r = tc.get("/v1/metrics", headers=sign_headers(b""))
    assert r.status_code == 200
