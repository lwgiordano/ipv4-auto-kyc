"""The conformance kit's expectations ARE the published registry's, not a second reading of it.

Runtime may not import the document layer (test_contract_registry_authority.py's F13 gate), so the
kit derives what it can from the executable authorities and this file binds the rest — the worked
vector, the ingest path, the statuses, the sensitive-actor rule, the callback keys — to the claims
TechCraft actually reads. If a claim moves and the kit does not, these fail.
"""

import json
from typing import get_args

import httpx
from docs.contracts.wire import EVENT_TABLE, INGEST_HEADERS, SIGNATURE_VECTOR, WIRE

from kyc_tool import conformance
from kyc_tool.api.schemas import CheckSummary, DecisionCallback, EnforcementHeld, GatesBody
from kyc_tool.events.review_guard import reviewer_actor_reason


def test_vector_mode_reproduces_the_published_vector_with_no_network():
    published = {key: SIGNATURE_VECTOR[key] for key in conformance.PUBLISHED_VECTOR}
    assert published == conformance.PUBLISHED_VECTOR
    rows = conformance.vector_rows()
    assert [row for row in rows if not row.ok] == []
    assert [row.expected for row in rows] == [SIGNATURE_VECTOR["body_sha256"],
                                              SIGNATURE_VECTOR["signature"]]


def test_the_kit_posts_to_the_published_ingest_path_within_the_published_skew():
    method, _, target = WIRE.value("WIRE.INGEST.PATH").partition(" ")
    assert (method, target) == ("POST", conformance.INGEST_PATH)
    assert WIRE.value("WIRE.SIGN.SKEW_SECONDS") == conformance.SKEW_SECONDS


def test_walk_is_every_event_table_row_once_with_its_required_payload():
    steps = conformance.walk()
    assert [name for name, _, _ in steps] == [row.name for row in EVENT_TABLE]
    for (name, status, body), row in zip(steps, EVENT_TABLE, strict=True):
        envelope = json.loads(body)
        assert set(envelope) == {"event_type", "occurred_at", "actor", "payload"}  # §3
        assert envelope["event_type"] == name
        assert set(envelope["payload"]) == set(row.required)
        assert status in dict(WIRE.value("WIRE.INGEST.STATUS")), f"{name}: undocumented status"


def test_sensitive_events_carry_the_documented_reviewer_actor():
    sensitive = WIRE.value("WIRE.ACTOR.SENSITIVE")
    seen = set()
    for name, _, body in conformance.walk():
        envelope = json.loads(body)
        if name in sensitive["events"]:
            seen.add(name)
            assert envelope["actor"]["type"] == sensitive["actor_type"]
            # the guard that decides it, not a re-reading of the rule
            assert reviewer_actor_reason(envelope["actor"], envelope["payload"]) is None
        else:
            assert envelope["actor"]["type"] == "system"
    assert seen == set(sensitive["events"])


def test_every_expected_status_is_a_code_the_ingest_table_publishes():
    assert set(conformance.EXPECTED_STATUS) <= {row.name for row in EVENT_TABLE}
    codes = set(conformance.EXPECTED_STATUS.values()) | {
        conformance.ACCEPTED, conformance.STORED_REPLAY, conformance.BAD_SIGNATURE,
        conformance.TASK_NOT_FOUND, conformance.INVALID_PAYLOAD,
    }
    assert codes <= set(dict(WIRE.value("WIRE.INGEST.STATUS")))


def test_a_signed_event_carries_exactly_the_documented_ingest_headers():
    client = httpx.Client(base_url="https://tool.test")
    request = conformance.v2_request(client, "POST", "/v1/cases/c-1/events", b"{}",
                                     secret="not-a-real-secret", key_id="k-1", slot="idem-1")
    documented = {header.name for header in INGEST_HEADERS}
    assert all(name in request.headers for name in documented)  # httpx keys are case-insensitive
    assert {name for name in request.headers if name.startswith("x-kyc")} == {
        name.lower() for name in documented if name.lower().startswith("x-kyc")}


def test_callback_checks_name_the_same_keys_the_publisher_emits():
    assert set(conformance.CALLBACK_FIELDS) == set(WIRE.value("WIRE.CALLBACK.FIELDS")) == {
        name for name, field in DecisionCallback.model_fields.items() if field.is_required()}
    assert set(conformance.CALLBACK_GATES) == set(WIRE.value("WIRE.CALLBACK.GATES")) == set(
        GatesBody.model_fields)
    assert set(conformance.CALLBACK_DECISIONS) == set(WIRE.value("WIRE.CALLBACK.DECISIONS"))
    assert set(conformance.CHECK_KEYS) == set(CheckSummary.model_fields)
    assert set(conformance.HELD_KEYS) == set(EnforcementHeld.model_fields)
    assert set(conformance.BUY_ENABLEMENT) == set(
        get_args(DecisionCallback.model_fields["buy_enablement"].annotation))


def test_the_read_keys_are_the_ones_the_route_actually_serves():
    import inspect

    from kyc_tool.api import routes_read
    served = inspect.getsource(routes_read.get_case)
    assert all(f'"{key}"' in served for key in conformance.READ_KEYS)
    # §7: status, score, the latest decision, the live checks, information_requested
    assert len(conformance.READ_KEYS) == 5


def test_each_status_constant_is_the_registry_code_with_that_meaning():
    """Membership in the ingest table is not enough: 202→200 would still be a published code.
    Each constant is the code whose published MEANING the kit relies on."""
    meanings = dict(WIRE.value("WIRE.INGEST.STATUS"))
    assert "accepted and queued" in meanings[conformance.ACCEPTED]
    assert "replay of a seen Idempotency-Key" in meanings[conformance.STORED_REPLAY]
    assert "signature invalid" in meanings[conformance.BAD_SIGNATURE]
    assert "review task not found" in meanings[conformance.TASK_NOT_FOUND]
    assert "malformed" in meanings[conformance.INVALID_PAYLOAD]
    assert conformance.EXPECTED_STATUS == {
        "reviewer.manual_approve": conformance.STORED_REPLAY,  # "an inline reviewer.manual_approve"
        "website.review_completed": conformance.TASK_NOT_FOUND,  # no open task in a conformance run
    }
    for name, status, _ in conformance.walk():
        assert status == conformance.EXPECTED_STATUS.get(name, conformance.ACCEPTED)

