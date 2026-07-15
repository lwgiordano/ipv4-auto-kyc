"""PR 2 (DB): per-run frozen input snapshots, per-case event sequence, and the
concurrency invariant — same-case ingestion and reviewer.manual_approve race
without colliding on a sequence."""

import json
import threading
import uuid

import pytest
from sqlalchemy import text

from kyc_tool.adapters.floqer import FixtureFloqerClient, FloqerAdapter
from kyc_tool.adapters.website_manual_review import WebsiteManualReviewAdapter
from kyc_tool.events.ingest import ingest_event
from kyc_tool.orchestration.broker_gate import BrokerGate
from kyc_tool.orchestration.pipeline import Pipeline
from kyc_tool.storage.object_store import FsStore
from tests.integration.shared import ACME_KYB_WITH_CONTACT

pytestmark = pytest.mark.postgres

EMAIL = {"email": "ops@acme.example", "domain": "acme.example", "verified_at": "2026-01-01T00:00:00Z"}


def _events(engine, case_id):
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT event_type, event_sequence, sequence_backfilled, run_id "
                "FROM events WHERE case_id=:c ORDER BY event_sequence"
            ),
            {"c": case_id},
        ).fetchall()


def _case_seq(engine, case_id):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT event_sequence FROM cases WHERE id=:c"), {"c": case_id}
        ).scalar_one()


def test_events_get_monotonic_live_sequence(engine, post_event):
    post_event("seq-case", "kyb.run_requested", ACME_KYB_WITH_CONTACT)
    post_event("seq-case", "email.verified", EMAIL)
    rows = _events(engine, "seq-case")
    assert [r.event_sequence for r in rows] == [1, 2]
    assert all(r.sequence_backfilled is False for r in rows)  # assigned live, not backfilled
    assert _case_seq(engine, "seq-case") == 2


def test_replay_does_not_consume_a_sequence(engine, post_event):
    _, key = post_event("replay-case", "kyb.run_requested", ACME_KYB_WITH_CONTACT)
    post_event("replay-case", "kyb.run_requested", ACME_KYB_WITH_CONTACT, key=key)  # exact replay
    assert len(_events(engine, "replay-case")) == 1
    assert _case_seq(engine, "replay-case") == 1


def test_key_reuse_different_payload_409_and_no_increment(engine, post_event):
    _, key = post_event("reuse-case", "kyb.run_requested", ACME_KYB_WITH_CONTACT)
    resp, _ = post_event(
        "reuse-case", "kyb.run_requested", ACME_KYB_WITH_CONTACT, key=key, force_new_body=True
    )
    assert resp.status_code == 409
    assert _case_seq(engine, "reuse-case") == 1


def test_run_freezes_inputs_before_later_events(engine, post_event):
    r1, _ = post_event("freeze-case", "kyb.run_requested", ACME_KYB_WITH_CONTACT)
    run1 = r1.json()["run_id"]
    # a second event arrives BEFORE the worker executes run 1 — it moves the case
    # snapshot forward, but must NOT leak into run 1's frozen inputs
    post_event("freeze-case", "email.verified", EMAIL)
    with engine.connect() as conn:
        run1_snapshot = conn.execute(
            text("SELECT input_snapshot_json FROM runs WHERE id=:r"), {"r": run1}
        ).scalar_one()
        case_snapshot = conn.execute(
            text("SELECT submitted_json FROM cases WHERE id='freeze-case'")
        ).scalar_one()
    assert "email" not in run1_snapshot  # frozen at run creation
    assert "email" in case_snapshot  # the case has moved on


def test_manual_approve_gets_a_sequence_but_no_run(engine, post_event):
    post_event("manual-case", "kyb.run_requested", ACME_KYB_WITH_CONTACT)
    post_event(
        "manual-case",
        "reviewer.manual_approve",
        {"reviewer_id": "r1", "note": "ok"},
        actor={"type": "reviewer", "id": "r1"},
    )
    by_type = {r.event_type: r for r in _events(engine, "manual-case")}
    assert by_type["reviewer.manual_approve"].event_sequence == 2
    assert by_type["reviewer.manual_approve"].run_id is None  # record-only, no run
    assert by_type["kyb.run_requested"].run_id is not None
    assert _case_seq(engine, "manual-case") == 2


def test_concurrent_ingest_and_manual_approve_do_not_collide(engine, session_factory, policy):
    """The FOR UPDATE case lock must serialize sequence allocation across a
    normal event and a manual approve arriving at once — distinct, gap-free
    sequences, and UNIQUE(case_id, event_sequence) never trips."""
    case_id = "race-case"

    def envelope(event_type, payload, actor):
        return {
            "event_type": event_type,
            "occurred_at": "2026-01-01T00:00:00Z",
            "actor": actor,
            "payload": payload,
        }

    start = threading.Barrier(2)
    errors: list[Exception] = []

    def writer(event_type, payload, actor):
        try:
            start.wait()
            ingest_event(
                session_factory,
                policy,
                case_id=case_id,
                idempotency_key=uuid.uuid4().hex,
                envelope=envelope(event_type, payload, actor),
            )
        except Exception as exc:  # noqa: BLE001 — surface any collision to the assert
            errors.append(exc)

    kyb_args = ("kyb.run_requested", ACME_KYB_WITH_CONTACT, {"type": "system", "id": "s"})
    approve_args = ("reviewer.manual_approve", {"reviewer_id": "r"}, {"type": "reviewer", "id": "r"})
    threads = [
        threading.Thread(target=writer, args=kyb_args),
        threading.Thread(target=writer, args=approve_args),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert sorted(r.event_sequence for r in _events(engine, case_id)) == [1, 2]
    assert _case_seq(engine, case_id) == 2


def test_recorded_floqer_context_reseeded_on_resume(
    engine, post_event, session_factory, policy, settings
):
    """Codex P1: a run resumed after Floqer already committed (crash/lease expiry
    before the website adapter) must still hand the website task Floqer's
    discovery context — reseeded from the recorded result, not lost."""
    resp, _ = post_event("resume-case", "kyb.run_requested", ACME_KYB_WITH_CONTACT)
    run_id = resp.json()["run_id"]

    floqer = FloqerAdapter(FixtureFloqerClient({}))
    floqer_hash = floqer.input_hash(ACME_KYB_WITH_CONTACT, {})  # what _run_adapters will compute
    floqer_normalized = {
        "discovered": True,
        "company_domain": "acme.example",
        "website": "https://acme.example",
        "linkedin": {},
        "aliases": [],
        "registry_candidates": [],
        "broker_context": {},
    }
    # simulate the prior attempt: Floqer recorded, run left mid-RUN_ADAPTERS
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO adapter_results "
                "(id, run_id, adapter_id, status, input_hash, normalized_json) "
                "VALUES (:id, :r, 'floqer_company_enrichment', 'ok', :h, CAST(:n AS jsonb))"
            ),
            {"id": uuid.uuid4().hex, "r": run_id, "h": floqer_hash, "n": json.dumps(floqer_normalized)},
        )
        conn.execute(text("UPDATE runs SET state='RUN_ADAPTERS' WHERE id=:r"), {"r": run_id})

    # resume with Floqer + website built — Floqer is skipped (already recorded)
    pipeline = Pipeline(
        session_factory,
        policy,
        FsStore(settings.object_store_root),
        settings,
        adapters={
            "floqer_company_enrichment": floqer,
            "website_manual_review": WebsiteManualReviewAdapter(),
        },
        broker_matcher=BrokerGate(),
    )
    pipeline._run_adapters(run_id)

    with engine.connect() as conn:
        context = conn.execute(
            text(
                "SELECT context_json FROM review_tasks "
                "WHERE case_id='resume-case' AND task_type='website'"
            )
        ).scalar_one()
    # the website task carries Floqer's discovery even though Floqer was skipped
    assert context["discovery"]["company_domain"] == "acme.example"
