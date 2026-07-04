from kyc_tool import security


def test_sign_verify_roundtrip():
    ts = "1700000000.0"
    body = b'{"x":1}'
    sig = security.sign("secret", ts, body)
    assert security.verify("secret", ts, body, sig, now=1700000000.0)


def test_verify_rejects_bad_signature():
    ts = "1700000000.0"
    assert not security.verify("secret", ts, b"body", "deadbeef", now=1700000000.0)


def test_verify_rejects_wrong_secret():
    ts = "1700000000.0"
    sig = security.sign("other", ts, b"body")
    assert not security.verify("secret", ts, b"body", sig, now=1700000000.0)


def test_verify_rejects_stale_timestamp():
    ts = "1700000000.0"
    sig = security.sign("secret", ts, b"body")
    assert not security.verify(
        "secret", ts, b"body", sig, max_skew_seconds=300, now=1700000000.0 + 301
    )


def test_verify_rejects_garbage_timestamp():
    assert not security.verify("secret", "not-a-number", b"body", "sig")
