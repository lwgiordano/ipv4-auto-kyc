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
from pathlib import Path

import pytest
from fastapi import HTTPException

from kyc_tool import security
from kyc_tool.api import auth, hmac_witness

from .test_production_config import hardened

ACTIVE_ID = "kyc-platform-1"
SECRET = "s" * 40
PATH = "/v1/cases/c1/events"
SRC = Path(__file__).resolve().parents[2] / "src" / "kyc_tool"


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


# ── v1: two DIFFERENT outcome classes, named honestly (gate finding 4) ────────────────────────
#
# The previous single test was called `..._is_401_not_500` and then accepted either a 401 or no
# exception at all, which let two genuinely different policies hide behind one name. They are split
# here because they answer different questions.
#
#   * A malformed VERIFICATION input (the legacy secret) means we cannot check the signature. There
#     is no honest answer but 401.
#   * A malformed RETIREMENT input (sunset, observation window) means we cannot prove v1 is retired.
#     ADR-003 is explicit that a scheduled date takes effect only once the witness is green, so
#     "cannot read the evidence" is definitionally not proof, and a VALID v1 request is accepted.
#
# That second behaviour is a deliberate, human-approved policy, not an accident. The reasoning: the
# request is cryptographically valid, so accepting it is not an authentication failure; the only way
# to reach a malformed value at request time is injecting an object into a live Settings, which
# means code execution — an adversary there has no need to extend v1's life; `production_config_
# violations` refuses all of these at BOOT, so a normally-loaded config cannot reach the state at
# all; and failing closed would turn a config typo or a DB blip into an outage on the platform's
# live traffic, which is exactly what ADR-003 exists to prevent.

V1_VERIFICATION_POISON = [
    ("legacy secret is an int", {"platform_hmac_secret": 12345}),
    ("legacy secret is hostile", {"platform_hmac_secret": HostileStr(SECRET)}),
    ("legacy secret is a dict", {"platform_hmac_secret": {"a": 1}}),
]


@pytest.mark.parametrize("label,update", V1_VERIFICATION_POISON,
                         ids=[c[0] for c in V1_VERIFICATION_POISON])
def test_a_malformed_v1_verification_input_is_401(label, update):
    """Cannot verify => refuse. No availability argument applies: without a usable secret there is
    no signature check to be lenient about."""
    settings = hardened(platform_hmac_secret=SECRET).model_copy(update=update)
    with pytest.raises(HTTPException) as excinfo:
        auth.require_valid_signature(settings, _Req(_v1_headers()), b"{}")
    assert excinfo.value.status_code == 401, excinfo.value.detail


class _TrivialSession:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def commit(self):
        return None


@pytest.fixture
def witness(monkeypatch):
    """Stub only the DATABASE layer, so the real `_inbound_v1_zero`/`_record_v1` logic runs.

    Recording the arguments is the point: gate finding 4 was that the observation-window specimen
    passed `session_factory=None`, so `_inbound_v1_zero` returned before consulting the poisoned
    value and the test proved nothing. Capturing what the witness was CALLED WITH turns "the value
    is consumed" from an assumption into an assertion.
    """
    seen = {"windows": [], "recorded": 0, "zero": True}

    def fake_zero(session, window_days, now):
        seen["windows"].append(window_days)
        return seen["zero"]

    def fake_record(session):
        seen["recorded"] += 1

    monkeypatch.setattr(hmac_witness, "inbound_v1_zero", fake_zero)
    monkeypatch.setattr(hmac_witness, "record_v1_accepted", fake_record)
    seen["factory"] = lambda: _TrivialSession()
    return seen


def _req_with_session(headers, witness):
    request = _Req(headers)
    request.app.state.session_factory = witness["factory"]
    return request


V1_RETIREMENT_POISON = [
    ("sunset is hostile", {"hmac_v1_inbound_sunset_at": HostileStr("2020-01-01T00:00:00Z")}),
    ("sunset is an int", {"hmac_v1_inbound_sunset_at": 20200101}),
    ("sunset is a dict", {"hmac_v1_inbound_sunset_at": {"d": 1}}),
    ("window is hostile", {"hmac_v1_observation_window_days": HostileStr("7")}),
    ("window is a dict", {"hmac_v1_observation_window_days": {"d": 1}}),
]


@pytest.mark.parametrize("label,update", V1_RETIREMENT_POISON,
                         ids=[c[0] for c in V1_RETIREMENT_POISON])
def test_unreadable_retirement_evidence_accepts_a_valid_v1_request(label, update, witness):
    """The approved availability policy, asserted POSITIVELY rather than hidden behind an
    either/or, and with the witness layer live so the window value is genuinely consulted."""
    settings = hardened(platform_hmac_secret=SECRET).model_copy(update=update)
    auth.require_valid_signature(settings, _req_with_session(_v1_headers(), witness), b"{}")


def test_the_observation_window_value_is_actually_consulted(witness):
    """Gate finding 4's second half. The old specimen passed `session_factory=None`, so
    `_inbound_v1_zero` returned before reading the window at all — it could not have detected a
    poisoned value because it never looked. Here the witness records what it was called with."""
    settings = hardened(platform_hmac_secret=SECRET,
                        hmac_v1_inbound_sunset_at="2020-01-01T00:00:00Z",
                        hmac_v1_observation_window_days=11)
    witness["zero"] = False  # not retired, so the request is served and recorded
    auth.require_valid_signature(settings, _req_with_session(_v1_headers(), witness), b"{}")
    assert witness["windows"] == [11], (
        "the observation window never reached the witness, so no test of it proves anything"
    )


def test_a_clean_past_sunset_with_a_zero_witness_still_retires_v1(witness):
    """The control that makes the policy above meaningful. Without it, "accepts a valid v1
    request" is indistinguishable from "retirement is broken"."""
    settings = hardened(platform_hmac_secret=SECRET,
                        hmac_v1_inbound_sunset_at="2020-01-01T00:00:00Z")
    witness["zero"] = True
    with pytest.raises(HTTPException) as excinfo:
        auth.require_valid_signature(settings, _req_with_session(_v1_headers(), witness), b"{}")
    assert excinfo.value.status_code == 401
    assert "retired" in str(excinfo.value.detail)


def test_a_past_sunset_with_LIVE_v1_traffic_does_not_retire(witness):
    """ADR-003's actual rule: the date alone never cuts off live traffic. A non-zero witness means
    the window is not clean, so a valid v1 request is still served AND recorded."""
    settings = hardened(platform_hmac_secret=SECRET,
                        hmac_v1_inbound_sunset_at="2020-01-01T00:00:00Z")
    witness["zero"] = False
    auth.require_valid_signature(settings, _req_with_session(_v1_headers(), witness), b"{}")
    assert witness["recorded"] == 1


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


# ── gate round: the toggles that decide whether verification runs at all ──────────────────────
#
# Wave 0 hardened every key, secret and skew field ON the verification path and never touched the
# boolean deciding whether that path runs (gate finding 1). `require_valid_signature` opened with
# `if settings.auth_disabled: return`, so an injected `1`, a truthy `"false"`, or any non-empty
# mapping ACCEPTED AN UNSIGNED REQUEST. That is strictly worse than the 500s Wave 0 fixed: those
# refused service, this grants it.
#
# The rule is a closed grammar rather than a type check: only exact built-in `True` may disable
# authentication, and only exact built-in `False` may select the deliberately-open dev read path.
# Anything else — wrong type, hostile object, or a string that merely looks boolean — takes the
# controlled-deny path. "false" is the specimen that matters: it is truthy in Python and means the
# opposite of what it says.

MALFORMED_TOGGLES = [
    ("int 1", 1),
    ("int 0", 0),
    ("str 'false'", "false"),
    ("str 'true'", "true"),
    ("str empty", ""),
    ("dict truthy", {"x": 1}),
    ("dict empty", {}),
    ("None", None),
    ("hostile __bool__", HostileStr("x")),
]


@pytest.mark.parametrize("label,value", MALFORMED_TOGGLES, ids=[c[0] for c in MALFORMED_TOGGLES])
def test_only_exact_true_disables_signature_checking(label, value):
    """An UNSIGNED request against a malformed `auth_disabled`. Every case must be denied, and
    denied cleanly — a raw exception here is the same availability signal Wave 0 removed."""
    settings = hardened().model_copy(update={"auth_disabled": value})
    with pytest.raises(HTTPException) as excinfo:
        auth.require_valid_signature(settings, _Req({}), b"{}")
    assert excinfo.value.status_code == 401, excinfo.value.detail


def test_exact_true_still_disables_it():
    """Guard the guard: the documented dev escape hatch must keep working, or this is not a
    grammar, it is a removal."""
    settings = hardened().model_copy(update={"auth_disabled": True})
    auth.require_valid_signature(settings, _Req({}), b"{}")


@pytest.mark.parametrize("label,value", MALFORMED_TOGGLES, ids=[c[0] for c in MALFORMED_TOGGLES])
def test_only_exact_false_opens_the_dev_read_path(label, value):
    """`read_auth_required` is the mirror image: it is the value that must be exactly `False` to
    open a surface, so a malformed falsey value must NOT open it."""
    settings = hardened().model_copy(update={"read_auth_required": value})
    with pytest.raises(HTTPException) as excinfo:
        auth.require_read_access(settings, _Req({}), b"{}")
    assert excinfo.value.status_code == 401, excinfo.value.detail


def test_exact_false_still_opens_the_dev_read_path():
    settings = hardened().model_copy(update={"read_auth_required": False})
    auth.require_read_access(settings, _Req({}), b"{}")


ADMIN_POISON = [
    ("token is an int", {"ui_admin_token": 12345}),
    ("token is hostile", {"ui_admin_token": HostileStr("t" * 32)}),
    ("token is a dict", {"ui_admin_token": {"t": 1}}),
    ("token is None", {"ui_admin_token": None}),
]


@pytest.mark.parametrize("label,update", ADMIN_POISON, ids=[c[0] for c in ADMIN_POISON])
def test_a_malformed_admin_gate_denies_rather_than_opening_or_crashing(label, update):
    """The admin gate mutates through the ops console, so a malformed token must never mean
    'open'. Only an exact empty string is the documented unconfigured dev state.

    The CORRECT credential is presented, so a denial here is caused by the malformed configured
    token rather than by a wrong header — the failure being tested is `if not token: return`
    treating a misconfiguration as "no credential required".
    """
    settings = hardened(ui_admin_token="t" * 32).model_copy(update=update)
    with pytest.raises(HTTPException) as excinfo:
        auth.require_admin(settings, {"Authorization": "Bearer " + "t" * 32})
    assert excinfo.value.status_code == 401, excinfo.value.detail


def test_truthy_junk_in_auth_disabled_does_not_open_the_admin_gate():
    """Separate from the token matrix: here the TOKEN is fine and `auth_disabled` is the poison,
    so the credential must still be demanded. Presenting none must be refused."""
    settings = hardened(ui_admin_token="t" * 32).model_copy(update={"auth_disabled": "false"})
    with pytest.raises(HTTPException) as excinfo:
        auth.require_admin(settings, {})
    assert excinfo.value.status_code == 401
    # and a correct credential still works under the same poisoned toggle
    auth.require_admin(settings, {"Authorization": "Bearer " + "t" * 32})


def test_a_hostile_authorization_header_is_gated_before_startswith():
    settings = hardened(ui_admin_token="t" * 32)
    with pytest.raises(HTTPException) as excinfo:
        auth.require_admin(settings, {"Authorization": HostileStr("Bearer x")})
    assert excinfo.value.status_code == 401


def test_the_configured_admin_token_still_authenticates():
    settings = hardened(ui_admin_token="t" * 32)
    auth.require_admin(settings, {"Authorization": "Bearer " + "t" * 32})


# Re-gate finding 1: the empty-token allowance was ENVIRONMENT-BLIND, so a production-shaped
# Settings opened the `/ui` mutation surface with no credential. `/ui` calls `require_admin`
# directly rather than through the ops wrapper, so the wrapper's production check did not cover it.
EMPTY_TOKEN_ENVIRONMENTS = [
    ("production denies", "production", False),
    ("development opens", "development", True),
    ("test opens", "test", True),
]


@pytest.mark.parametrize("label,environment,opens", EMPTY_TOKEN_ENVIRONMENTS,
                         ids=[c[0] for c in EMPTY_TOKEN_ENVIRONMENTS])
def test_an_empty_admin_token_opens_only_in_an_exact_dev_environment(label, environment, opens):
    settings = hardened(ui_admin_token="t" * 32).model_copy(
        update={"ui_admin_token": "", "environment": environment})
    if opens:
        auth.require_admin(settings, {})
        return
    with pytest.raises(HTTPException) as excinfo:
        auth.require_admin(settings, {})
    assert excinfo.value.status_code == 401


EMPTY_TOKEN_POISON = [
    ("production", {"environment": "production"}),
    ("production + ui_enabled", {"environment": "production", "ui_enabled": True}),
    ("malformed auth_disabled", {"auth_disabled": "false"}),
    ("unknown environment", {"environment": "staging"}),
    ("environment is an int", {"environment": 1}),
    ("environment is hostile", {"environment": HostileStr("development")}),
]


@pytest.mark.parametrize("label,update", EMPTY_TOKEN_POISON, ids=[c[0] for c in EMPTY_TOKEN_POISON])
def test_an_empty_token_never_opens_a_non_dev_or_malformed_environment(label, update):
    """Codex's four specimens plus the malformed-environment cases. An environment that is not
    exactly a known dev value is not a dev environment."""
    settings = hardened(ui_admin_token="t" * 32).model_copy(
        update={"ui_admin_token": "", **update})
    with pytest.raises(HTTPException) as excinfo:
        auth.require_admin(settings, {})
    assert excinfo.value.status_code == 401


def test_the_direct_ui_route_uses_the_same_admin_decision():
    """The two surfaces must not drift: `/ui` calls `require_admin` directly, while the ops router
    adds its own production wrapper. If `/ui` ever grew a separate gate this would stop holding."""
    ui = (SRC / "ui" / "routes.py").read_text()
    assert "require_admin" in ui, "the /ui routes no longer use the shared admin gate"
    assert "_require_ops_admin" not in ui, "/ui grew a second, separate admin decision"


# ── gate round: the int subclass the Wave 0 skew test never reached ───────────────────────────
class HostileInt(int):
    """Passes `isinstance(x, int)`. Wave 0's skew case used a hostile STRING, which fails the
    isinstance check and returns before the comparison — so the accepting branch, where the
    dispatch actually happens, was never exercised (gate finding 2)."""

    def __ge__(self, other):
        raise RuntimeError("hostile __ge__")

    def __le__(self, other):
        raise RuntimeError("hostile __le__")


def test_an_int_subclass_cannot_dispatch_from_the_skew_gate():
    now = str(int(time.time()))
    good = dict(key_id=ACTIVE_ID, direction=security.DIRECTION_INBOUND, method="POST",
                path_qs=PATH, timestamp=now, slot="k1", body=b"{}")
    assert security.verify(SECRET, now, b"{}", "x", max_skew_seconds=HostileInt(300)) is False
    assert security.verify_v2(SECRET, "x", max_skew_seconds=HostileInt(300), **good) is False
    settings = hardened(hmac_inbound_key_id=ACTIVE_ID, hmac_inbound_secret=SECRET).model_copy(
        update={"hmac_max_skew_seconds": HostileInt(300)})
    _assert_401(settings, _v2_headers())


def test_an_exact_int_skew_still_works():
    """Guard the guard: exact 300 must still verify, or the gate is a removal."""
    now = str(int(time.time()))
    assert security.verify(
        SECRET, now, b"{}", security.sign(SECRET, now, b"{}"), max_skew_seconds=300) is True


# ── re-gate finding 2: a malformed window must never PROVE zero ───────────────────────────────
#
# The Wave 0 fix hardened the direction that ACCEPTS and left the direction that REFUSES. With a
# past sunset and a real observation row two days old, a window of `True`, `0.5`, `-1` or `0` makes
# `(now - started).days < window` false and `accepted_within_window` false, so the witness returns
# "zero proven" and a valid v1 request is RETIRED. That cuts off the platform's live traffic — the
# outage the approved availability policy exists to prevent, arrived at from the other side.
#
# My hostile-window tests could not have caught it: `hardened()` carries a 2026-09-01 sunset, so
# `_sunset_passed` was False and the poisoned window never reached the witness at all.

PAST_SUNSET = "2020-01-01T00:00:00Z"

MALFORMED_WINDOWS = [
    ("bool True", True),
    ("bool False", False),
    ("float 0.5", 0.5),
    ("negative", -1),
    ("zero", 0),
    ("int subclass", HostileInt(14)),
    ("string", "14"),
    ("None", None),
]


@pytest.mark.parametrize("label,window", MALFORMED_WINDOWS, ids=[c[0] for c in MALFORMED_WINDOWS])
def test_a_malformed_window_never_retires_live_v1(label, window, witness):
    """Past sunset, valid signature, witness reachable. Unreadable retirement evidence is not
    proof, so the request must be SERVED — and must not raise."""
    settings = hardened(platform_hmac_secret=SECRET,
                        hmac_v1_inbound_sunset_at=PAST_SUNSET).model_copy(
        update={"hmac_v1_observation_window_days": window})
    witness["zero"] = True  # the witness would say "zero" if it were consulted at all
    auth.require_valid_signature(settings, _req_with_session(_v1_headers(), witness), b"{}")
    assert witness["windows"] == [], (
        "a malformed window reached the witness; it must be rejected as unreadable evidence "
        "BEFORE it can be turned into proof of zero"
    )


def test_the_sunset_really_has_passed_in_these_specimens():
    """Guard the guard, and the exact reason the Wave 0 version was vacuous: `hardened()` supplies
    a FUTURE sunset, so `_sunset_passed` was False and no poisoned window was ever consulted."""
    from datetime import UTC, datetime

    assert auth._sunset_passed(PAST_SUNSET, datetime.now(UTC)) is True
    assert auth._sunset_passed(hardened().hmac_v1_inbound_sunset_at, datetime.now(UTC)) is False


def test_a_clean_window_still_retires_and_is_consulted(witness):
    """The positive control: a governed window IS passed to the witness and DOES retire."""
    settings = hardened(platform_hmac_secret=SECRET, hmac_v1_inbound_sunset_at=PAST_SUNSET,
                        hmac_v1_observation_window_days=14)
    witness["zero"] = True
    with pytest.raises(HTTPException) as excinfo:
        auth.require_valid_signature(settings, _req_with_session(_v1_headers(), witness), b"{}")
    assert excinfo.value.status_code == 401 and "retired" in str(excinfo.value.detail)
    assert witness["windows"] == [14]


def test_the_witness_predicate_is_total_on_its_own():
    """`hmac_witness.inbound_v1_zero` must refuse a malformed window itself, so a future caller
    that bypasses the API wrapper cannot resurrect this.

    Deliberately NOT using the `witness` fixture: that fixture monkeypatches this very function, so
    requesting it would test the stub instead of the predicate.
    """
    for window in (True, 0.5, -1, 0, "14", None, HostileInt(14)):
        assert hmac_witness.inbound_v1_zero(_TrivialSession(), window, None) is False, window
