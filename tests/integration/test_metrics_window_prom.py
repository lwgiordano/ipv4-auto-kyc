"""PR 10a: bounded metrics windows + the Prometheus exposition endpoint.

The latency aggregates cover a FIXED, declared 24h window (unbounded history made the percentiles
stale and the scans unbounded on never-pruned tables), and /v1/metrics.prom exposes the same gauges
in text exposition format behind the same read-auth gate."""

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.postgres


def test_latency_window_is_declared_and_excludes_old_rows(client, session_factory, clean_db):
    with session_factory() as s:
        # one adapter result INSIDE the window, one far outside it
        s.execute(text("INSERT INTO cases (id) VALUES ('mw')"))
        s.execute(
            text(
                "INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
                "actor_json, payload_json, event_sequence) VALUES "
                "('mw-e','mw','mw-k','h','x','{}'::jsonb,'{}'::jsonb,1)"
            )
        )
        s.execute(
            text(
                "INSERT INTO runs (id, case_id, triggering_event_id, state) "
                "VALUES ('mw-r','mw','mw-e','PUBLISH_DECISION')"
            )
        )
        s.execute(
            text(
                "INSERT INTO adapter_results (id, run_id, adapter_id, status, normalized_json, "
                "input_hash, latency_ms, fetched_at) VALUES "
                "('mw-in','mw-r','gleif','ok','{}'::jsonb,'h-in', 100, now()), "
                "('mw-out','mw-r','gleif','ok','{}'::jsonb,'h-out', 9000, now() - interval '10 days')"
            )
        )
        s.commit()

    payload = client.get("/v1/metrics").json()
    assert payload["latency_window"] == "24h"
    gleif = next(r for r in payload["adapter_latency"] if r["adapter_id"] == "gleif")
    assert gleif["calls"] == 1  # the 10-day-old row is outside the declared window
    assert gleif["p95_ms"] == pytest.approx(100.0)


def test_prometheus_endpoint_renders_the_same_gauges(client, session_factory, clean_db):
    body = client.get("/v1/metrics.prom").text
    assert "kyc_event_to_decision_seconds_p95" in body
    assert "kyc_hmac_v1_accepted" in body
    assert client.get("/v1/metrics.prom").headers["content-type"].startswith("text/plain")


def test_prometheus_endpoint_is_read_auth_gated(settings, session_factory, policy):
    from fastapi.testclient import TestClient

    from kyc_tool.api.app import create_app

    hardened = settings.model_copy(update={"read_auth_required": True})
    tc = TestClient(create_app(hardened, session_factory=session_factory, policy=policy))
    assert tc.get("/v1/metrics.prom").status_code == 401  # unauthenticated scrape refused
