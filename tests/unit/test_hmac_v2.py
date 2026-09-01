"""HMAC v2 canonical signing (PR 5a): binds method + path so a captured
signature cannot be redirected to another case. v1 stays intact."""

import hashlib

from kyc_tool import security

FIELDS = dict(
    key_id="kyc-platform-1",
    direction=security.DIRECTION_INBOUND,
    method="POST",
    path_qs="/v1/cases/acme-1/events",
    timestamp="1000.0",
    slot="idem-1",
    body=b'{"event_type":"email.verified"}',
)


def test_canonical_v2_is_exact_and_binds_body_hash():
    c = security.canonical_v2(**FIELDS)
    lines = c.split("\n")
    assert lines[0] == "v2"
    assert lines[1:7] == [
        "kyc-platform-1",
        "platform->tool",
        "POST",
        "/v1/cases/acme-1/events",
        "1000.0",
        "idem-1",
    ]
    assert lines[7] == hashlib.sha256(FIELDS["body"]).hexdigest()


def test_verify_v2_roundtrip_and_skew():
    sig = security.sign_v2("secret", **FIELDS)
    assert security.verify_v2("secret", sig, max_skew_seconds=300, now=1000.0, **FIELDS)
    assert not security.verify_v2("secret", sig, max_skew_seconds=300, now=2000.0, **FIELDS)
    assert not security.verify_v2("wrong", sig, max_skew_seconds=300, now=1000.0, **FIELDS)


def test_non_finite_timestamp_rejected():
    fields = {**FIELDS, "timestamp": "nan"}
    sig = security.sign_v2("secret", **fields)
    assert not security.verify_v2("secret", sig, max_skew_seconds=300, now=1000.0, **fields)


def test_bad_direction_is_rejected_not_crashing():
    fields = {**FIELDS, "direction": "sideways"}
    assert not security.verify_v2("secret", "deadbeef", max_skew_seconds=300, now=1000.0, **fields)


def test_every_binding_dimension_is_load_bearing():
    sig = security.sign_v2("secret", **FIELDS)
    for field, bad in [
        ("method", "GET"),
        ("path_qs", "/v1/cases/acme-2/events"),
        ("direction", security.DIRECTION_OUTBOUND),
        ("key_id", "other"),
        ("timestamp", "1001.0"),
        ("slot", "idem-2"),
        ("body", b"{}"),
    ]:
        tampered = {**FIELDS, field: bad}
        assert not security.verify_v2("secret", sig, max_skew_seconds=300, now=1000.0, **tampered), field
