"""PR 1 auth surface (offline). The read API and ops-console mutations reject
unauthenticated callers when the corresponding config is on, and /ui is not
mounted by default. All of these reject before any DB access, so the sentinel
session factory (which raises if used) proves the guard fired first — no
Postgres required."""

import pytest
from fastapi.testclient import TestClient

from kyc_tool.api.app import create_app
from kyc_tool.config import Settings


def _boom_session_factory():
    raise AssertionError("auth must reject before touching the database")


def _app(policy, **overrides):
    settings = Settings(environment="development", **overrides)
    return create_app(settings, session_factory=_boom_session_factory, policy=policy)


def test_ui_not_mounted_by_default(policy):
    client = TestClient(_app(policy))  # ui_enabled defaults False now
    assert client.get("/ui").status_code == 404
    assert client.post("/ui/api/send-event", json={}).status_code == 404


def test_read_endpoints_reject_unauthenticated_when_required(policy):
    client = TestClient(_app(policy, read_auth_required=True, platform_hmac_secret="s" * 40))
    assert client.get("/v1/cases/anything").status_code == 401
    assert client.get("/v1/cases/anything/checks").status_code == 401
    assert client.get("/v1/runs/anything").status_code == 401
    assert client.get("/v1/review-tasks").status_code == 401


def test_read_endpoints_open_when_not_required(policy):
    # default read_auth_required=False → guard is a no-op and the request
    # reaches the (sentinel) DB layer, proving auth did NOT reject it.
    client = TestClient(_app(policy, platform_hmac_secret="s" * 40))
    with pytest.raises(AssertionError, match="before touching the database"):
        client.get("/v1/cases/anything")


def test_ui_mutations_require_admin_token(policy):
    client = TestClient(_app(policy, ui_enabled=True, ui_admin_token="admin-secret"))
    assert client.post("/ui/api/requeue/job/1").status_code == 401
    assert (
        client.post("/ui/api/requeue/outbox/1", headers={"Authorization": "Bearer wrong"})
        .status_code
        == 401
    )


def test_ui_mutations_open_when_no_token_configured(policy):
    # empty ui_admin_token → console stays open (dev/test trust model); the
    # request reaches the sentinel DB layer instead of being rejected.
    client = TestClient(_app(policy, ui_enabled=True))
    with pytest.raises(AssertionError, match="before touching the database"):
        client.post("/ui/api/requeue/outbox/1")
