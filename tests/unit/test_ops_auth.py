"""PR 1 auth surface (offline). The read API and ops-console mutations reject
unauthenticated callers when the corresponding config is on, and /ui is not
mounted by default. All of these reject before any DB access, so the sentinel
session factory (which raises if used) proves the guard fired first — no
Postgres required.

PR 6: create_app() now seeds+verifies the policy bundle at construction time
(kyc_tool.policy_store.repo.seed_and_verify/attest) — a real, intentional DB
write that happens once, before any request. That is not what this file is
about, so seed_and_verify/attest are stubbed out here; the sentinel session
factory keeps proving exactly what it always proved: per-request auth guards
reject before route handlers touch the database."""

import pytest
from fastapi.testclient import TestClient

import kyc_tool.api.app as app_module
from kyc_tool.api.app import create_app
from kyc_tool.config import Settings


def _boom_session_factory():
    raise AssertionError("auth must reject before touching the database")


def _app(policy, monkeypatch, **overrides):
    # return the SERVED policy's own hash so create_app's startup identity check
    # (PR 6 audit) passes without a real DB write — the sentinel factory stays untouched
    monkeypatch.setattr(app_module, "seed_and_verify", lambda *a, **k: policy.bundle_hash)
    monkeypatch.setattr(app_module, "attest", lambda **k: None)
    settings = Settings(environment="development", **overrides)
    return create_app(settings, session_factory=_boom_session_factory, policy=policy)


def test_ui_not_mounted_by_default(policy, monkeypatch):
    client = TestClient(_app(policy, monkeypatch))  # ui_enabled defaults False now
    assert client.get("/ui").status_code == 404
    assert client.post("/ui/api/send-event", json={}).status_code == 404


def test_read_endpoints_reject_unauthenticated_when_required(policy, monkeypatch):
    client = TestClient(_app(policy, monkeypatch, read_auth_required=True, platform_hmac_secret="s" * 40))
    assert client.get("/v1/cases/anything").status_code == 401
    assert client.get("/v1/cases/anything/checks").status_code == 401
    assert client.get("/v1/cases/anything/salesforce-projection").status_code == 401
    assert client.get("/v1/runs/anything").status_code == 401
    assert client.get("/v1/review-tasks").status_code == 401


def test_read_endpoints_open_when_not_required(policy, monkeypatch):
    # default read_auth_required=False → guard is a no-op and the request
    # reaches the (sentinel) DB layer, proving auth did NOT reject it.
    client = TestClient(_app(policy, monkeypatch, platform_hmac_secret="s" * 40))
    with pytest.raises(AssertionError, match="before touching the database"):
        client.get("/v1/cases/anything")


def test_ui_mutations_require_admin_token(policy, monkeypatch):
    client = TestClient(_app(policy, monkeypatch, ui_enabled=True, ui_admin_token="admin-secret"))
    assert client.post("/ui/api/requeue/job/1").status_code == 401
    assert (
        client.post("/ui/api/requeue/outbox/1", headers={"Authorization": "Bearer wrong"}).status_code == 401
    )


def test_ui_mutations_open_when_no_token_configured(policy, monkeypatch):
    # empty ui_admin_token → console stays open (dev/test trust model); the
    # request reaches the sentinel DB layer instead of being rejected.
    client = TestClient(_app(policy, monkeypatch, ui_enabled=True))
    with pytest.raises(AssertionError, match="before touching the database"):
        client.post("/ui/api/requeue/outbox/1")
