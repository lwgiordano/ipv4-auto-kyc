"""The conformance receiver validates the COMPLETE callback schema, not just keys and enums.

Codex F1 (2026-09-19): a correctly signed body with a string score, a bad date, non-boolean
gates and a malformed check passed every row and was recorded in the dedupe ledger, while
`DecisionCallback.model_validate` rejected the same body with ten errors. The receiver must
fail the `body.schema` row for a wrong primitive, a wrong nested field and an unknown field,
and must keep every invalid callback out of the ledger.
"""

import json
import time

from kyc_tool import conformance, security
from tests.callback_bodies import valid_callback_body

SECRET, KEY_ID, PATH = "test-outbound-secret", "test-outbound-1", "/kyc/decision"


def _rows(body: dict, seen: set) -> list:
    raw, stamp = json.dumps(body).encode(), str(time.time())
    headers = {
        "X-KYC-Timestamp": stamp,
        "X-KYC-Key-Id": KEY_ID,
        "X-KYC-Signature-V2": security.sign_v2(
            SECRET, key_id=KEY_ID, direction=security.DIRECTION_OUTBOUND, method="POST",
            path_qs=PATH, timestamp=stamp, slot="", body=raw),
    }
    return conformance.verify_callback(
        PATH, headers, raw, outbound_secret=SECRET, outbound_key_id=KEY_ID, v1_secret="",
        seen=seen)


def _verify(body: dict) -> tuple[set[str], set]:
    seen: set = set()
    rows = _rows(body, seen)
    return {row.check for row in rows if not row.ok}, seen


def test_a_valid_body_passes_every_row_and_is_recorded():
    failed, seen = _verify(valid_callback_body())
    assert failed == set()
    assert seen == {("c1", "r1")}


def test_a_wrong_primitive_type_fails_only_the_schema_row_and_is_not_recorded():
    failed, seen = _verify(valid_callback_body(score="not-an-integer", decided_at="not-a-date"))
    assert failed == {"body.schema"}
    assert seen == set()


def test_a_coercible_wrong_type_still_fails_because_validation_is_strict():
    # Lax mode would read "10", 10.0 and "true" as the contract's int and bool. The tool never
    # emits those shapes, so a receiver built to accept them is not conforming.
    for body in (valid_callback_body(score="10"), valid_callback_body(score=10.0),
                 valid_callback_body(gates=dict.fromkeys(conformance.CALLBACK_GATES, "true"))):
        failed, seen = _verify(body)
        assert failed == {"body.schema"}
        assert seen == set()


def test_the_flag_on_production_shape_with_event_sequence_validates():
    # The shape `callback_include_event_sequence=True` emits. Pinned so the unknown-field test
    # below can never be "fixed" by loosening the model.
    failed, seen = _verify(valid_callback_body(event_sequence=1))
    assert failed == set()
    assert seen == {("c1", "r1")}


def test_a_wrong_nested_field_fails_only_the_schema_row_and_is_not_recorded():
    body = valid_callback_body(
        gates=dict.fromkeys(conformance.CALLBACK_GATES, "not-a-bool"),
        checks=[{"type": "x", "status": {"bad": "type"}, "points": "bad", "source": "x",
                 "reason_codes": "bad"}],
    )
    failed, seen = _verify(body)
    assert failed == {"body.schema"}  # every key is present, so only the schema sees it
    assert seen == set()


def test_an_unknown_field_fails_the_schema_row_and_is_not_recorded():
    # `decision_sequence` joins the model only with migration 025; until then it is a defect to
    # surface (the model is extra="forbid"), not a value to accept.
    failed, seen = _verify(valid_callback_body(decision_sequence=1))
    assert failed == {"body.schema"}
    assert seen == set()


def test_the_schema_row_names_the_first_failing_field():
    rows = _rows(valid_callback_body(score="not-an-integer"), set())
    schema = next(row for row in rows if row.check == "body.schema")
    assert not schema.ok and "score" in schema.got
    # the ledger row still comes last and reports the hold
    assert rows[-1].check == "dedupe" and "not recorded" in rows[-1].got
