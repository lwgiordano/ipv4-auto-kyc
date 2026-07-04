"""Shared fixtures: ephemeral Postgres, migrated schema, app client with HMAC
signing, synchronous pipeline worker, and a callback-capturing outbox publisher.

Policy values are NEVER copied into tests — the `policy` fixture loads the same
normative JSONs the service loads (test-plan principle #1).
"""

import json
import time
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from fastapi.testclient import TestClient
from sqlalchemy import text

from kyc_tool import security
from kyc_tool.api.app import create_app
from kyc_tool.config import REPO_ROOT, Settings
from kyc_tool.db.session import make_engine, make_session_factory
from kyc_tool.orchestration.pipeline import Pipeline
from kyc_tool.outbox.publisher import OutboxPublisher
from kyc_tool.policy.loader import load_policy
from kyc_tool.queue.worker import Worker
from kyc_tool.storage.object_store import FsStore
from tests.pg import EphemeralPostgres

TEST_SECRET = "test-hmac-secret"

_ALL_TABLES = (
    "outbox",
    "poc_tokens",
    "review_tasks",
    "decisions",
    "checks",
    "adapter_results",
    "jobs",
    "runs",
    "events",
    "audit_log",
    "broker_entities",
    "cases",
)


@pytest.fixture(scope="session")
def pg() -> str:
    cluster = EphemeralPostgres()
    url = cluster.start()
    yield url
    cluster.stop()


@pytest.fixture(scope="session")
def migrated(pg: str) -> str:
    cfg = AlembicConfig(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", pg)
    alembic_command.upgrade(cfg, "head")
    return pg


@pytest.fixture(scope="session")
def engine(migrated: str):
    engine = make_engine(migrated)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def session_factory(engine):
    return make_session_factory(engine)


@pytest.fixture(scope="session")
def policy():
    return load_policy(REPO_ROOT / "KYC_Tool_Build_Package" / "machine_readable")


@pytest.fixture()
def clean_db(engine, policy):
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(_ALL_TABLES)} RESTART IDENTITY CASCADE"))
        for entity in policy.broker_policy.entities:  # re-seed (migration 005 data)
            conn.execute(
                text(
                    "INSERT INTO broker_entities (id, name, policy) VALUES (:id, :name, :policy)"
                ),
                {"id": uuid.uuid4().hex, "name": entity.name, "policy": entity.policy},
            )
    return None


@pytest.fixture()
def settings(migrated: str, tmp_path) -> Settings:
    return Settings(
        database_url=migrated,
        platform_hmac_secret=TEST_SECRET,
        auth_disabled=False,
        platform_callback_url="http://platform.test",
        object_store_root=tmp_path / "evidence",
    )


@pytest.fixture()
def app(settings, session_factory, policy, clean_db):
    return create_app(settings, session_factory=session_factory, policy=policy)


@pytest.fixture()
def client(app) -> TestClient:
    return TestClient(app)


def sign_headers(body: bytes, *, key: str | None = None) -> dict[str, str]:
    timestamp = str(time.time())
    return {
        "Content-Type": "application/json",
        "Idempotency-Key": key or uuid.uuid4().hex,
        "X-KYC-Timestamp": timestamp,
        "X-KYC-Signature": security.sign(TEST_SECRET, timestamp, body),
    }


def envelope(event_type: str, payload: dict, actor: dict | None = None) -> dict:
    return {
        "event_type": event_type,
        "occurred_at": datetime.now(UTC).isoformat(),
        "actor": actor or {"type": "system", "id": "test"},
        "payload": payload,
    }


@pytest.fixture()
def post_event(client):
    """POST a signed platform event; returns (response, idempotency_key).

    A replay (same key) re-sends the exact same bytes, as the platform would —
    the envelope is cached per idempotency key."""
    bodies: dict[str, bytes] = {}

    def _post(
        case_id: str,
        event_type: str,
        payload: dict,
        *,
        key: str | None = None,
        actor=None,
        force_new_body: bool = False,
    ):
        key = key or uuid.uuid4().hex
        if force_new_body or key not in bodies:
            bodies[key] = json.dumps(envelope(event_type, payload, actor)).encode()
        body = bodies[key]
        headers = sign_headers(body, key=key)
        response = client.post(f"/v1/cases/{case_id}/events", content=body, headers=headers)
        return response, key

    return _post


@pytest.fixture()
def pipeline(session_factory, policy, settings, tmp_path):
    return Pipeline(
        session_factory, policy, FsStore(tmp_path / "evidence"), settings, adapters={}
    )


@pytest.fixture()
def worker(session_factory, settings, pipeline) -> Worker:
    return Worker(
        session_factory,
        {"run_transition": pipeline.handle_job},
        lease_seconds=settings.job_lease_seconds,
        backoff_base_seconds=0,
        on_dead_letter=pipeline.on_dead_letter,
    )


class CallbackCapture:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.status_code = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(
            {
                "url": str(request.url),
                "headers": dict(request.headers),
                "body": json.loads(request.content),
            }
        )
        return httpx.Response(self.status_code)


@pytest.fixture()
def callback_capture() -> CallbackCapture:
    return CallbackCapture()


@pytest.fixture()
def email_sender():
    from kyc_tool.outbox.emails import LoggingEmailSender

    return LoggingEmailSender()


@pytest.fixture()
def publisher(session_factory, settings, callback_capture, email_sender) -> OutboxPublisher:
    transport = httpx.MockTransport(callback_capture.handler)
    return OutboxPublisher(
        session_factory,
        settings,
        http_client=httpx.Client(transport=transport),
        email_sender=email_sender,
    )


@pytest.fixture()
def evidence_store(settings) -> FsStore:
    return FsStore(settings.object_store_root)
