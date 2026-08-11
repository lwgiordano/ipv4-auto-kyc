"""The REQUEST-time HMAC boundary is total against hostile inputs (re-audit `4c3015a..cccd5f7` F1).

The production aggregate was hardened first, and that was mistaken for the boundary being closed.
It is not the same boundary: `production_config_violations` runs at BOOT, while
`require_valid_signature` runs per request against whatever object is on `app.state`. A
`model_copy(update=...)` that never passed a validator reaches the request path intact, and Codex
reproduced four escapes there — a hostile active-key-id `__eq__` raising `RuntimeError`, a rotation
`str` subclass whose `encode()` raises inside `sign_v2`, an integer legacy secret raising
`AttributeError`, and a correctly signed v1 request reaching a hostile sunset value.

Every one produced a 500 on a request that deserved a 401. That distinction matters beyond tidiness:
a 500 is an availability signal an unauthenticated caller can drive, and it discards the honest
answer (this signature is not valid) in favour of an accident.

`isinstance` cannot fix this, which is the whole point. A `str` subclass passes `isinstance` and is
exactly the adversary; the check itself dispatches to its `__eq__`/`__hash__`/`strip`. Only exact
type identity is safe, so `type(x) is str` is used deliberately throughout and this file exists to
prove it holds at the boundary rather than one layer behind it.
"""

import time

import pytest
from fastapi import HTTPException

from kyc_tool import security
from kyc_tool.api import auth

from .test_production_config import hardened

ACTIVE_ID = "kyc-platform-1"
SECRET = "s" * 40
PATH = "/v1/cases/c1/events"


class HostileStr(str):
    """Passes `isinstance(x, str)`; every operation a checker might perform explodes."""

    def __eq__(self, other):
        raise RuntimeError("hostile __eq__")

    def __hash__(self):
        raise RuntimeError("hostile __hash__")

    def __bool__(self):
        raise RuntimeError("hostile __bool__")

    def strip(self, *args, **kwargs):
        raise RuntimeError("hostile strip")

    def encode(self, *args, **kwargs):
        raise RuntimeError("hostile encode")


class HostileKeyStr(str):
    """Hostile everywhere EXCEPT `__hash__`, which has to work for the object to become a dict key
    at all. This is the realistic shape of a poisoned mapping key: it got into the dict, so it is
    hashable, and everything the lookup does afterwards is still attacker-controlled."""

    def __eq__(self, other):
        raise RuntimeError("hostile __eq__")

    __hash__ = str.__hash__

    def strip(self, *args, **kwargs):
        raise RuntimeError("hostile strip")

    def encode(self, *args, **kwargs):
        raise RuntimeError("hostile encode")


class HostileDict(dict):
    """Passes `isinstance(x, dict)`; lookup and iteration explode."""

    def get(self, *args, **kwargs):
        raise RuntimeError("hostile get")

    def items(self):
        raise RuntimeError("hostile items")

    def __getitem__(self, key):
        raise RuntimeError("hostile __getitem__")

    def __contains__(self, key):
        raise RuntimeError("hostile __contains__")


class _Req:
    def __init__(self, headers, method="POST"):
        self.headers = headers
        self.method = method
        self.scope = {"path": PATH, "query_string": b""}

        class _App:
            class state:
                session_factory = None

        self.app = _App()

        class _Url:
            path = PATH

        self.url = _Url()


def _v2_headers(secret=SECRET, key_id=ACTIVE_ID, body=b"{}", slot="k1"):
    """A GENUINELY signed v2 request. The mutations below poison configuration, not the signature,
    so a rejection can never be explained away as 'the signature was wrong anyway'."""
    timestamp = str(int(time.time()))
    signature = security.sign_v2(
        secret, key_id=key_id, direction=security.DIRECTION_INBOUND, method="POST",
        path_qs=PATH, timestamp=timestamp, slot=slot, body=body,
    )
    return {"X-KYC-Key-Id": key_id, "X-KYC-Signature-V2": signature,
            "X-KYC-Timestamp": timestamp, "Idempotency-Key": slot}


def _v1_headers(secret=SECRET, body=b"{}"):
    timestamp = str(int(time.time()))
    return {"X-KYC-Timestamp": timestamp,
            "X-KYC-Signature": security.sign(secret, timestamp, body)}


def _assert_401(settings, headers, body=b"{}"):
    """A controlled 401 and nothing else. `pytest.raises(HTTPException)` alone would not do: the
    escapes being tested raise RuntimeError/AttributeError/TypeError, and this asserts those never
    surface — the failure mode is an uncaught exception, not a wrong status code."""
    with pytest.raises(HTTPException) as excinfo:
        auth.require_valid_signature(settings, _Req(headers), body)
    assert excinfo.value.status_code == 401, excinfo.value.detail


# ── the baseline: a real signed request is accepted ───────────────────────────────────────────
def test_a_correctly_signed_v2_request_is_accepted():
    """Guard the guard. Without this every rejection below could be the harness failing, and a
    boundary that refuses everything would pass the entire rest of this file."""
    settings = hardened(hmac_inbound_key_id=ACTIVE_ID, hmac_inbound_secret=SECRET)
    auth.require_valid_signature(settings, _Req(_v2_headers()), b"{}")


def test_a_correctly_signed_v1_request_is_accepted():
    settings = hardened(platform_hmac_secret=SECRET)
    auth.require_valid_signature(settings, _Req(_v1_headers()), b"{}")


# ── Codex's four reproductions, and the fields they generalise to ─────────────────────────────
# Poison that the ACTIVE-key path consults. The request is signed with the active key id.
ACTIVE_POISON = [
    ("active key id, hostile __eq__", {"hmac_inbound_key_id": HostileStr(ACTIVE_ID)}),
    ("active secret, hostile encode", {"hmac_inbound_secret": HostileStr(SECRET)}),
    ("active secret is an int", {"hmac_inbound_secret": 12345}),
    ("active secret is a dict", {"hmac_inbound_secret": {"not": "a secret"}}),
    ("active secret is None", {"hmac_inbound_secret": None}),
    ("skew is not an int", {"hmac_max_skew_seconds": HostileStr("300")}),
]

# Poison that only the ROTATION path consults. These MUST be presented under a non-active key id:
# an earlier draft signed every case with the active id, so `_inbound_secret` returned the active
# secret and the poisoned mapping was never touched — the tests passed without exercising anything.
ROTATION_KEY_ID = "kyc-platform-0"
EXTRAS_POISON = [
    ("extras is a hostile mapping",
     {"hmac_inbound_extra_keys": HostileDict({ROTATION_KEY_ID: SECRET})}),
    ("extras is not a mapping", {"hmac_inbound_extra_keys": ["not", "a", "map"]}),
    ("extras is None", {"hmac_inbound_extra_keys": None}),
    ("extras key is hostile",
     {"hmac_inbound_extra_keys": {HostileKeyStr(ROTATION_KEY_ID): SECRET}}),
    ("extras value is hostile",
     {"hmac_inbound_extra_keys": {ROTATION_KEY_ID: HostileStr(SECRET)}}),
    ("extras value is an int", {"hmac_inbound_extra_keys": {ROTATION_KEY_ID: 999}}),
]


@pytest.mark.parametrize("label,update", ACTIVE_POISON, ids=[c[0] for c in ACTIVE_POISON])
def test_hostile_active_key_config_cannot_turn_v2_verification_into_a_500(label, update):
    """`model_copy(update=...)` bypasses every validator, so these objects genuinely reach the
    request path. The signature presented is real and correct for the UNPOISONED config."""
    settings = hardened(
        hmac_inbound_key_id=ACTIVE_ID, hmac_inbound_secret=SECRET,
    ).model_copy(update=update)
    _assert_401(settings, _v2_headers())


@pytest.mark.parametrize("label,update", EXTRAS_POISON, ids=[c[0] for c in EXTRAS_POISON])
def test_hostile_rotation_map_cannot_turn_v2_verification_into_a_500(label, update):
    """Signed under the ROTATION key id, so resolution actually walks the poisoned mapping."""
    settings = hardened(
        hmac_inbound_key_id=ACTIVE_ID, hmac_inbound_secret=SECRET,
    ).model_copy(update=update)
    _assert_401(settings, _v2_headers(key_id=ROTATION_KEY_ID))


def test_the_rotation_path_is_genuinely_exercised_by_those_cases():
    """Guard the guard, again. A clean rotation entry under the same key id must AUTHENTICATE, so
    a rejection above is caused by the poison rather than by the key id being unknown."""
    settings = hardened(
        hmac_inbound_key_id=ACTIVE_ID, hmac_inbound_secret=SECRET,
        hmac_inbound_extra_keys={ROTATION_KEY_ID: "r" * 40},
    )
    auth.require_valid_signature(
        settings, _Req(_v2_headers(secret="r" * 40, key_id=ROTATION_KEY_ID)), b"{}")


def test_a_hostile_presented_key_id_is_gated_before_it_is_compared():
    """The header value itself, not just the configured one. `_inbound_secret` compared the
    presented id against the active id, so a hostile object on EITHER side ran during the
    comparison."""
    settings = hardened(hmac_inbound_key_id=ACTIVE_ID, hmac_inbound_secret=SECRET)
    assert auth._inbound_secret(settings, HostileStr(ACTIVE_ID)) == ""
    assert auth._inbound_secret(settings, 1234) == ""
    assert auth._inbound_secret(settings, None) == ""


V1_POISON = [
    ("legacy secret is an int", {"platform_hmac_secret": 12345}),
    ("legacy secret is hostile", {"platform_hmac_secret": HostileStr(SECRET)}),
    ("legacy secret is a dict", {"platform_hmac_secret": {"a": 1}}),
    ("sunset is hostile", {"hmac_v1_inbound_sunset_at": HostileStr("2020-01-01T00:00:00Z")}),
    ("sunset is an int", {"hmac_v1_inbound_sunset_at": 20200101}),
    ("observation window is hostile", {"hmac_v1_observation_window_days": HostileStr("7")}),
]


@pytest.mark.parametrize("label,update", V1_POISON, ids=[c[0] for c in V1_POISON])
def test_a_valid_v1_signature_reaching_hostile_config_is_401_not_500(label, update):
    """The nastiest of the four: the request is CRYPTOGRAPHICALLY VALID, so it is past the point
    where a caller could be dismissed as unauthenticated, and the poisoned value is consulted
    afterwards. A raw exception here is a 500 on an authenticated request."""
    settings = hardened(platform_hmac_secret=SECRET).model_copy(update=update)
    try:
        auth.require_valid_signature(settings, _Req(_v1_headers()), b"{}")
    except HTTPException as exc:
        assert exc.status_code == 401, exc.detail
    # Reaching here means the request was ACCEPTED, which is correct for a valid v1 signature whose
    # poisoned sunset simply cannot be read as "passed". What must never happen is anything else.


# ── the primitives refuse on their own ────────────────────────────────────────────────────────
def test_verify_refuses_every_non_exact_input():
    """Defense in depth. The callers gate these too; a verification primitive that trusts its
    inputs makes every future caller a potential 500."""
    now = str(int(time.time()))
    assert security.verify(SECRET, now, b"{}", security.sign(SECRET, now, b"{}")) is True
    assert security.verify(HostileStr(SECRET), now, b"{}", "x") is False
    assert security.verify(SECRET, HostileStr(now), b"{}", "x") is False
    assert security.verify(SECRET, now, b"{}", HostileStr("x")) is False
    assert security.verify(12345, now, b"{}", "x") is False
    assert security.verify(SECRET, now, bytearray(b"{}"), "x") is False
    assert security.verify(SECRET, now, "{}", "x") is False


def test_verify_v2_refuses_every_non_exact_field():
    now = str(int(time.time()))
    good = dict(key_id=ACTIVE_ID, direction=security.DIRECTION_INBOUND, method="POST",
                path_qs=PATH, timestamp=now, slot="k1", body=b"{}")
    assert security.verify_v2(SECRET, security.sign_v2(SECRET, **good), **good) is True
    for field in ("key_id", "direction", "method", "path_qs", "timestamp", "slot"):
        poisoned = {**good, field: HostileStr(good[field])}
        assert security.verify_v2(SECRET, "x", **poisoned) is False, field
    assert security.verify_v2(SECRET, "x", **{**good, "body": "{}"}) is False
    assert security.verify_v2(HostileStr(SECRET), "x", **good) is False
    # a missing or unexpected field would reach canonical_v2 as a TypeError
    assert security.verify_v2(SECRET, "x", **{k: v for k, v in good.items() if k != "slot"}) is False
    assert security.verify_v2(SECRET, "x", **{**good, "extra": "x"}) is False


def test_no_hostile_method_is_ever_dispatched():
    """The property underneath all of the above: a gated object must be REJECTED without any of
    its own methods running. Each hostile method raises, so reaching a verdict at all proves none
    of them was called."""
    settings = hardened(hmac_inbound_key_id=ACTIVE_ID, hmac_inbound_secret=SECRET).model_copy(
        update={"hmac_inbound_extra_keys": {HostileKeyStr("old"): HostileStr(SECRET)}})
    assert auth._inbound_secret(settings, "old") == ""
    assert auth._sunset_passed(HostileStr("2020-01-01T00:00:00Z"), None) is False
