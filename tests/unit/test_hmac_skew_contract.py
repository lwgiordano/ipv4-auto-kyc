"""Re-audit `5b0f0b8..b75a320` R4-F1 (consumer layer): `verify`/`verify_v2` fail closed on any skew
outside the governed (0, 300] window, so a widened `hmac_max_skew_seconds` cannot enlarge the
acceptance window even on a direct call. Covers -1, 0, 1, 300, 301 and a year-old signature."""

import pytest

from kyc_tool import security as s
from kyc_tool.security import MAX_HMAC_SKEW_SECONDS

_V2 = dict(key_id="k", direction=s.DIRECTION_INBOUND, method="POST", path_qs="/v1/x", slot="")
_SKEWS = [(-1, False), (0, False), (1, True), (300, True), (301, False)]


@pytest.mark.parametrize("skew,ok", _SKEWS)
def test_verify_v1_refuses_out_of_contract_skew(skew, ok):
    now = 1_000_000.0
    ts = str(now)  # zero-age signature: only the skew contract decides acceptance
    sig = s.sign("secret", ts, b"body")
    assert s.verify("secret", ts, b"body", sig, max_skew_seconds=skew, now=now) is ok


@pytest.mark.parametrize("skew,ok", _SKEWS)
def test_verify_v2_refuses_out_of_contract_skew(skew, ok):
    now = 1_000_000.0
    ts = str(now)
    sig = s.sign_v2("secret", timestamp=ts, body=b"body", **_V2)
    got = s.verify_v2("secret", sig, max_skew_seconds=skew, now=now, timestamp=ts, body=b"body", **_V2)
    assert got is ok


def test_a_301_second_old_signature_fails_at_the_governed_window():
    now = 1_000_000_000.0
    old = now - 301
    for signer, verifier in (
        (lambda: s.sign("secret", str(old), b"b"),
         lambda sig: s.verify("secret", str(old), b"b", sig, max_skew_seconds=300, now=now)),
        (lambda: s.sign_v2("secret", timestamp=str(old), body=b"b", **_V2),
         lambda sig: s.verify_v2("secret", sig, max_skew_seconds=300, now=now,
                                 timestamp=str(old), body=b"b", **_V2)),
    ):
        assert verifier(signer()) is False


def test_a_year_old_signature_never_verifies_at_the_governed_window():
    now = 1_000_000_000.0
    old = now - 365 * 24 * 3600
    assert s.verify("secret", str(old), b"b", s.sign("secret", str(old), b"b"),
                    max_skew_seconds=MAX_HMAC_SKEW_SECONDS, now=now) is False


def test_the_governed_window_is_300_seconds():
    assert MAX_HMAC_SKEW_SECONDS == 300
