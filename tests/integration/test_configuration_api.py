"""Configuration HTTP authority, bounded JSON, and committed save receipts."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text

from kyc_tool.api.app import create_app
from kyc_tool.configuration.models import MAX_REQUEST_BYTES
from tests.integration.test_configuration_repo import baseline, counts

pytestmark = pytest.mark.usefixtures("clean_db")
AUTH = {"Authorization": "Bearer disposable-admin"}


def make_client(settings, session_factory, policy, **updates):
    cfg = settings.model_copy(
        update={"ui_admin_token": "disposable-admin", "enforce_bundle_pinning": True, **updates}
    )
    return TestClient(create_app(cfg, session_factory=session_factory, policy=policy))


def payload(base, **updates):
    return {
        "expected_revision": str(base.revision),
        "request_id": str(uuid4()),
        "value": base.mappings | {"KYC_Score__c": "Saved_Score__c"},
        **updates,
    }


@pytest.mark.parametrize(
    "token,header,disabled",
    [
        ("", None, False),
        ("   ", None, True),
        ("disposable-admin", None, True),
        ("disposable-admin", "Bearer wrong", True),
        ("disposable-admin", "Bearer wrong", False),
    ],
)
def test_configuration_requires_actual_admin_even_in_development(
    settings, session_factory, policy, token, header, disabled
):
    base = baseline(session_factory, policy)
    client = make_client(settings, session_factory, policy, ui_admin_token=token, auth_disabled=disabled)
    before = counts(session_factory)
    response = client.put(
        "/ui/api/configuration/mappings",
        json=payload(base),
        headers={"Authorization": header} if header else {},
    )
    assert response.status_code == 401
    assert set(response.json()) >= {"error", "detail"}
    assert counts(session_factory) == before


def test_unactivated_read_is_explicit_and_router_is_optional(settings, session_factory, policy):
    client = make_client(settings, session_factory, policy)
    response = client.get("/ui/api/configuration")
    assert response.status_code == 200
    assert response.json()["active"] is False
    assert response.json()["revision"] is None
    assert response.json()["can_edit"] is False
    assert response.json()["reason"] == "Live configuration has not been activated."
    assert response.json()["points"] == {i.check_type: i.points for i in policy.rubric.items}
    hidden = make_client(settings, session_factory, policy, ui_enabled=False)
    assert hidden.get("/ui/api/configuration").status_code == 404


def test_configuration_read_preserves_read_auth(settings, session_factory, policy):
    client = make_client(settings, session_factory, policy, read_auth_required=True)
    response = client.get("/ui/api/configuration")
    assert response.status_code == 401
    assert set(response.json()) >= {"error", "detail"}


@pytest.mark.parametrize(
    "origin", ["https://foreign.example", "null", "http://testserver.evil", "http://testserver/path"]
)
def test_foreign_origin_refuses_without_forwarded_header_bypass(settings, session_factory, policy, origin):
    base = baseline(session_factory, policy)
    client = make_client(settings, session_factory, policy)
    response = client.put(
        "/ui/api/configuration/mappings",
        json=payload(base),
        headers=AUTH | {"Origin": origin, "X-Forwarded-Host": "foreign.example"},
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    "body,content_type",
    [
        (b"{", "application/json"),
        (b"{}", "text/plain"),
        (
            b'{"expected_revision":"1","expected_revision":"2","request_id":"x","value":{}}',
            "application/json",
        ),
        (b'{"expected_revision":"1","request_id":"x","value":{"a":1,"a":2}}', "application/json"),
    ],
)
def test_malformed_or_duplicate_json_refuses(settings, session_factory, policy, body, content_type):
    baseline(session_factory, policy)
    client = make_client(settings, session_factory, policy)
    response = client.put(
        "/ui/api/configuration/mappings", content=body, headers=AUTH | {"Content-Type": content_type}
    )
    assert response.status_code == 422
    assert set(response.json()) >= {"error", "detail"}


def test_auth_precedes_body_receive_and_size_is_bounded(settings, session_factory, policy):
    baseline(session_factory, policy)
    client = make_client(settings, session_factory, policy)

    def unreadable():
        raise AssertionError("unauthenticated body was read")
        yield b""

    assert (
        client.put(
            "/ui/api/configuration/mappings",
            content=unreadable(),
            headers={"Content-Type": "application/json"},
        ).status_code
        == 401
    )
    response = client.put(
        "/ui/api/configuration/mappings",
        content=b" " * (MAX_REQUEST_BYTES + 1),
        headers=AUTH | {"Content-Type": "application/json"},
    )
    assert response.status_code == 422


def test_save_conflict_lost_response_replay_and_reconstructed_app(settings, session_factory, policy):
    base = baseline(session_factory, policy)
    client = make_client(settings, session_factory, policy, auth_disabled=True)
    body = payload(base)
    first = client.put(
        "/ui/api/configuration/mappings", json=body, headers=AUTH | {"Origin": "http://testserver"}
    )
    assert first.status_code == 200
    result = first.json()
    assert result["configuration"]["revision"] == result["current_revision"] == result["revision"]
    assert result["configuration"]["can_edit"] is True
    second_client = make_client(settings, session_factory, policy)
    current = second_client.get("/ui/api/configuration").json()
    assert current["mappings"]["KYC_Score__c"] == "Saved_Score__c"
    later_body = {
        "expected_revision": current["revision"],
        "request_id": str(uuid4()),
        "value": current["mappings"] | {"KYC_Score__c": "Later_Score__c"},
    }
    later = second_client.put("/ui/api/configuration/mappings", json=later_body, headers=AUTH).json()
    conflict = client.put("/ui/api/configuration/mappings", json=payload(base), headers=AUTH)
    assert conflict.status_code == 409
    assert conflict.json()["expected_revision"] == str(base.revision)
    assert conflict.json()["current_revision"] == later["revision"]
    before = counts(session_factory)
    replay = client.put("/ui/api/configuration/mappings", json=body, headers=AUTH).json()
    assert replay["replayed"] is True
    assert replay["revision"] == result["revision"]
    assert replay["current_revision"] == replay["configuration"]["revision"] == later["revision"]
    assert replay["configuration"]["mappings"]["KYC_Score__c"] == "Later_Score__c"
    assert counts(session_factory) == before


@pytest.mark.parametrize(
    "update",
    [
        {"expected_revision": True},
        {"expected_revision": 1},
        {"expected_revision": "01"},
        {"request_id": "no"},
        {"extra": "no"},
    ],
)
def test_closed_write_envelope(settings, session_factory, policy, update):
    base = baseline(session_factory, policy)
    client = make_client(settings, session_factory, policy)
    assert (
        client.put("/ui/api/configuration/mappings", json=payload(base, **update), headers=AUTH).status_code
        == 422
    )


def test_failed_commit_emits_no_success_or_partial_write(settings, session_factory, policy):
    base = baseline(session_factory, policy)
    client = make_client(settings, session_factory, policy)
    before = counts(session_factory)

    def before_commit(session):
        session.execute(
            text(
                "CREATE TEMP TABLE failing_save_commit (id int PRIMARY KEY, "
                "parent int REFERENCES failing_save_commit(id) DEFERRABLE INITIALLY DEFERRED) ON COMMIT DROP"
            )
        )
        session.execute(text("INSERT INTO failing_save_commit VALUES (1,2)"))

    def failing_factory():
        session = session_factory()
        event.listen(session, "before_commit", before_commit)
        return session

    client.app.state.session_factory = failing_factory
    response = client.put("/ui/api/configuration/mappings", json=payload(base), headers=AUTH)
    assert response.status_code == 503
    assert counts(session_factory) == before


def test_active_wrong_mode_and_missing_pointer_are_unavailable(settings, session_factory, policy):
    base = baseline(session_factory, policy)
    client = make_client(settings, session_factory, policy, enforce_bundle_pinning=False)
    assert client.get("/ui/api/configuration").json()["can_edit"] is False
    assert client.put("/ui/api/configuration/mappings", json=payload(base), headers=AUTH).status_code == 503
    assert client.get("/readyz").status_code == 503
    client = make_client(settings, session_factory, policy)
    assert client.get("/readyz").json()["checks"]["configuration"]["ok"] is True
    with session_factory() as session:
        session.execute(text("DELETE FROM configuration_state"))
        session.commit()
    assert client.get("/readyz").status_code == 503
    assert client.get("/ui/api/configuration").status_code == 503


def test_active_projection_policy_metadata_sources_and_overlap_classes(settings, session_factory, policy):
    from kyc_tool.db.session import uow
    from tests.integration.test_configuration_runtime import broker, save_section, send

    baseline(session_factory, policy)
    client = make_client(settings, session_factory, policy)
    send(client)
    before = client.get("/ui/api/cases/acme/full").json()
    mappings = {key: key for key in before["field_sources"]} | {"KYC_Status__c": "Saved_Status__c"}
    saved = save_section(session_factory, "mappings", mappings)
    points = {i.check_type: i.points for i in policy.rubric.items} | {"website_verified": 777}
    save_section(session_factory, "points", points)
    save_section(
        session_factory,
        "brokers",
        [
            broker(domains=["ACME.EXAMPLE"]),
            broker("blocked", id="blocked", name="Different Name", domains=["acme.example"]),
            broker(id="other-class", name="Unrelated Name", email_domains=["acme.example"]),
        ],
    )
    with uow(session_factory) as s:
        s.execute(text("DELETE FROM broker_entities"))
    current = client.get("/ui/api/configuration").json()
    assert current["broker_overlaps"] == [
        {
            "entity_ids": ["acme-broker", "blocked"],
            "identifier_classes": ["domains"],
            "blocked_precedence": True,
        }
    ]
    full = client.get("/ui/api/cases/acme/full").json()
    assert "KYC_Status__c" not in full["salesforce"]
    assert full["salesforce"]["Saved_Status__c"] == before["salesforce"]["KYC_Status__c"]
    assert full["field_sources"]["Saved_Status__c"] == before["field_sources"]["KYC_Status__c"]
    assert full["mapping_revision"] == current["revision"]
    assert int(full["mapping_revision"]) >= int(saved["revision"])
    policy_view = client.get("/ui/api/policy").json()
    assert policy_view["configuration_revision"] == current["revision"]
    assert next(i["points"] for i in policy_view["rubric"] if i["check_type"] == "website_verified") == 777
    assert len(policy_view["broker_entities"]) == 3
    assert policy_view["field_sources"]["Saved_Status__c"] == before["field_sources"]["KYC_Status__c"]
    assert policy_view["mapping_revision"] == current["revision"]
    assert client.get("/ui/api/overview").json()["policy"]["bundle_hash"] == current["bundle_hash"]
    sources = client.get("/ui/api/integrations").json()
    broker_source = next(a for a in sources["adapters"] if a["adapter_id"] == "broker_policy")
    assert broker_source["configuration_revision"] == current["revision"]
    assert broker_source["total"] == 3
    assert "versioned configuration" in broker_source["kind"]
    assert "SQL" not in (broker_source["todo"] or "")


def test_hostile_content_length_is_a_safe_validation_error(settings, session_factory, policy):
    client = make_client(settings, session_factory, policy)
    response = client.put(
        "/ui/api/configuration/mappings",
        content=b"{}",
        headers=AUTH | {"Content-Type": "application/json", "Content-Length": "9" * 5000},
    )
    assert response.status_code == 422


@pytest.mark.parametrize("value", [True, 1.5, "1", -1, 1001])
def test_actual_points_route_rejects_coercions(settings, session_factory, policy, value):
    base = baseline(session_factory, policy)
    client = make_client(settings, session_factory, policy)
    points = {i.check_type: i.points for i in policy.rubric.items} | {"website_verified": value}
    before = counts(session_factory)
    response = client.put("/ui/api/configuration/points", json=payload(base, value=points), headers=AUTH)
    assert response.status_code == 422
    assert counts(session_factory) == before


def test_actual_routes_serialize_two_editors(settings, session_factory, policy):
    from concurrent.futures import ThreadPoolExecutor

    base = baseline(session_factory, policy)
    clients = [make_client(settings, session_factory, policy) for _ in range(2)]
    bodies = [payload(base, value=base.mappings | {"KYC_Score__c": f"Editor{i}__c"}) for i in range(2)]

    def submit(i):
        return clients[i].put("/ui/api/configuration/mappings", json=bodies[i], headers=AUTH)

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(submit, range(2)))
    assert sorted(r.status_code for r in results) == [200, 409]
    winner = next(r.json() for r in results if r.status_code == 200)
    assert clients[0].get("/ui/api/configuration").json()["mappings"] == winner["configuration"]["mappings"]


def test_save_first_authority_read_has_bounded_lock_wait(settings, session_factory, policy):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from time import monotonic

    base = baseline(session_factory, policy)
    client = make_client(settings, session_factory, policy)
    before = counts(session_factory)
    pool = ThreadPoolExecutor(1)
    locker = session_factory()
    future = None
    exceeded_bound = False
    try:
        locker.execute(text("LOCK TABLE configuration_state IN ACCESS EXCLUSIVE MODE"))
        started = monotonic()
        future = pool.submit(client.put, "/ui/api/configuration/mappings", json=payload(base), headers=AUTH)
        try:
            response = future.result(timeout=7)
        except TimeoutError:
            exceeded_bound = True
        elapsed = monotonic() - started
    finally:
        # Cleanup must release the lock before waiting for a regressed request thread.
        locker.rollback()
        locker.close()
        if future is not None:
            future.result(timeout=10)
        pool.shutdown(wait=True)
    assert not exceeded_bound, "first save authority read remained blocked beyond the 5s limit + 2s margin"
    assert elapsed < 7
    assert response.status_code == 503
    assert response.json()["error"] == "configuration_unavailable"
    assert counts(session_factory) == before
