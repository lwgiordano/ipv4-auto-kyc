"""The conformance receiver validates the COMPLETE callback schema, not just keys and enums.

Codex F1 (2026-09-19): a correctly signed body with a string score, a bad date, non-boolean
gates and a malformed check passed every row and was recorded in the dedupe ledger, while
`DecisionCallback.model_validate` rejected the same body with ten errors. The receiver must
fail the `body.schema` row for a wrong primitive, a wrong nested field and an unknown field,
and must keep every invalid callback out of the ledger.
"""

import json
import socket
import threading
import time

import pytest

from kyc_tool import conformance, security
from tests.callback_bodies import valid_callback_body

SECRET, KEY_ID, PATH = "test-outbound-secret", "test-outbound-1", "/kyc/decision"


def _rows(body: dict, seen: set) -> list:
    return _raw_rows(json.dumps(body).encode(), seen)


def _raw_rows(raw: bytes, seen: set) -> list:
    stamp = str(time.time())
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


def test_a_scalar_in_a_nested_field_is_a_reported_failure_not_a_crash():
    # `checks: 1` raised TypeError inside the nested inspection before the schema row ran, so
    # the diagnostic dropped the connection instead of printing a failed row (human, 2026-09-19).
    scalars = (1, True, 1.5, "str")
    invalid = {"checks": scalars + ({}, None),  # an empty list is a legal, checkless body
               "gates": scalars + ({}, [], None),
               "enforcement_held": scalars + ({}, [])}  # None is legal: the field is optional
    for field, shapes in invalid.items():
        for bad in shapes:
            failed, seen = _verify(valid_callback_body(**{field: bad}))
            assert "body.schema" in failed, (field, bad)
            assert seen == set(), (field, bad)


def test_a_body_that_is_not_an_object_or_not_json_is_a_reported_failure_not_a_crash():
    deep = b"[" * 200_000 + b"]" * 200_000  # past the JSON decoder's recursion depth
    for raw in (b"", b"null", b"[]", b'"str"', b"123", b"not json", b"{", deep):
        seen: set = set()
        rows = _raw_rows(raw, seen)
        failed = {row.check for row in rows if not row.ok}
        assert {"body.schema", "body.required_keys"} <= failed, raw[:20]
        assert seen == set()
        assert rows[-1].check == "dedupe" and "not recorded" in rows[-1].got


def test_the_schema_row_comes_before_every_nested_row():
    rows = _rows(valid_callback_body(), set())
    names = [row.check for row in rows]
    assert names.index("body.schema") < names.index("body.required_keys")
    assert names.index("body.schema") < names.index("body.checks")


def test_a_scalar_checks_row_names_the_shape_not_a_missing_key():
    rows = _rows(valid_callback_body(checks=1), set())
    checks = next(row for row in rows if row.check == "body.checks")
    assert not checks.ok and checks.got == "not a list of objects: int"


@pytest.fixture()
def receiver(capsys):
    server = conformance.make_server(0, outbound_secret=SECRET, outbound_key_id=KEY_ID,
                                     v1_secret="")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()


def _raw_post(port: int, request: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(request)
        return sock.recv(4096)


def test_the_receiver_answers_a_malformed_content_length_instead_of_dropping_the_connection(
    receiver, capsys
):
    # A non-numeric or negative Content-Length used to raise inside the handler, so the
    # diagnostic printed a traceback and dropped that connection with no PASS/FAIL line.
    for value in (b"abc", b"-5", b""):
        reply = _raw_post(receiver, b"POST /kyc/decision HTTP/1.1\r\nHost: t\r\n"
                          b"Content-Length: " + value + b"\r\n\r\n")
        assert reply.startswith(b"HTTP/1.0 200") or reply.startswith(b"HTTP/1.1 200"), reply[:40]
    out = capsys.readouterr().out
    assert out.count("callback ← /kyc/decision") == 3
    assert "body.schema=FAIL" in out and "signature.v2=FAIL" in out


def test_the_schema_row_names_the_first_failing_field():
    rows = _rows(valid_callback_body(score="not-an-integer"), set())
    schema = next(row for row in rows if row.check == "body.schema")
    assert not schema.ok and "score" in schema.got
    # the ledger row still comes last and reports the hold
    assert rows[-1].check == "dedupe" and "not recorded" in rows[-1].got
