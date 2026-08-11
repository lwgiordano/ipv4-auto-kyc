"""The production configuration boundary is TOTAL: it never raises, on any value, on any field.

Re-audit `4f23f23..97deeae` F1. `model_copy(update=...)` bypasses Pydantic entirely, so
`production_config_violations` and everything it calls receive arbitrary objects. Two failure
modes matter and they are opposites:

  * RAISING — a `TypeError` out of `len()` or `.strip()` escapes to a boot path or `/readyz`,
    where a 500 reads as an outage rather than as a refusal. A readiness probe that cannot answer
    is indistinguishable from one that answered "unsafe", so the operator loses the diagnosis.
  * CERTIFYING — worse. A 40-entry dict in `hmac_inbound_secret` has `len` 40 and cleared the
    32-character floor; boot passed, and the dict then reached `sign_v2`, which raised on
    `.encode()` inside signature verification. A clean production start followed by a 500 on every
    authenticated request.

So this file asserts both directions over a hostile-value matrix, field by field, rather than
spot-checking the values that happened to be reported.
"""

import traceback

import pytest
from pydantic import ValidationError

from kyc_tool.api import auth
from kyc_tool.config import (
    ConfigLoadError,
    Settings,
    hmac_extra_key_violations,
    hmac_key_id_violations,
    hmac_secret_violations,
    hmac_sunset_violations,
    production_config_violations,
)

from .test_production_config import hardened

SENTINEL = "REAL_ROTATION_SECRET_0123456789AB"  # 32 chars: clears the floor, must never be printed


class _Hostile:
    """Every dunder the boundary might touch, weaponized. A value like this is not hypothetical —
    it is what a YAML/TOML loader or a test double can put on a field."""

    def __eq__(self, other):
        raise RuntimeError("hostile __eq__")

    def __len__(self):
        raise RuntimeError("hostile __len__")

    def __bool__(self):
        raise RuntimeError("hostile __bool__")

    def __iter__(self):
        raise RuntimeError("hostile __iter__")

    def __str__(self):
        return "<hostile>"

    __repr__ = __str__

    def __hash__(self):
        return 0


# Every value a field can hold that is not what the field's type says.
HOSTILE_VALUES = [
    None, 0, 1, -1, 3.5, True, b"bytes", [], ["x"], (), {"k": "v"}, {1, 2}, object(), _Hostile(),
]

# Fields whose declared type is str and which flow into len/strip/encode/parse.
STRING_FIELDS = [
    "platform_hmac_secret", "platform_callback_url", "hmac_inbound_secret",
    "hmac_outbound_secret", "hmac_inbound_key_id", "hmac_outbound_key_id",
    "hmac_v1_inbound_sunset_at", "hmac_v1_outbound_sunset_at", "ui_admin_token",
    "object_store", "s3_bucket", "ocr_engine", "email_provider", "adapters_profile",
]


@pytest.mark.parametrize("field", STRING_FIELDS)
@pytest.mark.parametrize("value", HOSTILE_VALUES, ids=lambda v: type(v).__name__)
def test_the_boundary_never_raises_on_any_field(field, value):
    """The matrix. Not `pytest.raises`, not a needle — just: it returns a list, and it says no."""
    smuggled = hardened().model_copy(update={field: value})
    violations = production_config_violations(smuggled)
    assert isinstance(violations, list)
    assert violations, f"{field}={value!r} was certified safe for production"
    assert all(isinstance(item, str) for item in violations)


@pytest.mark.parametrize("field", ["hmac_inbound_extra_keys", "adapter_rate_limits"])
@pytest.mark.parametrize("value", HOSTILE_VALUES, ids=lambda v: type(v).__name__)
def test_mapping_fields_never_raise_either(field, value):
    smuggled = hardened().model_copy(update={field: value})
    violations = production_config_violations(smuggled)
    assert isinstance(violations, list)
    if value == {} if isinstance(value, dict) else False:  # pragma: no cover — {} is the legal empty
        return
    assert violations, f"{field}={value!r} was certified safe for production"


def test_numeric_fields_never_raise():
    """Numerics take the non-numeric half of the matrix: `1`/`True`/`3.5` are legal values for
    some of these fields, so only values that cannot be a number must produce a violation."""
    non_numeric = [None, "x", b"bytes", [], ["x"], (), {"k": "v"}, {1, 2}, object(), _Hostile()]
    for field in ("job_lease_seconds", "outbox_max_attempts", "outbox_http_timeout_seconds",
                  "retention_days", "hmac_max_skew_seconds"):
        for value in non_numeric:
            smuggled = hardened().model_copy(update={field: value})
            violations = production_config_violations(smuggled)
            assert isinstance(violations, list)
            assert violations, f"{field}={value!r} was certified safe"


# ── the two exact repros Codex reported ───────────────────────────────────────────────────────────
def test_a_dict_secret_does_not_clear_the_length_floor():
    """`len({...40 entries...})` is 40, so the old `len(secret) < 32` check passed it."""
    forty = {f"k{i}": "v" for i in range(40)}
    assert len(forty) == 40
    smuggled = hardened().model_copy(update={"hmac_inbound_secret": forty})
    violations = production_config_violations(smuggled)
    assert any("must be a string" in v for v in violations), violations


def test_an_all_whitespace_secret_does_not_clear_the_length_floor():
    """32 spaces is 32 characters. The floor is measured on the trimmed value."""
    smuggled = hardened().model_copy(update={"hmac_inbound_secret": " " * 32})
    assert any("blank" in v for v in production_config_violations(smuggled))


def test_a_hostile_eq_does_not_take_down_the_rotation_check():
    """`extra == {}` ran before the isinstance check, so a raising `__eq__` killed the aggregate."""
    assert hmac_extra_key_violations(_Hostile(), "active")
    assert production_config_violations(
        hardened().model_copy(update={"hmac_inbound_extra_keys": _Hostile()}))


# ── the validators are individually total ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("value", HOSTILE_VALUES, ids=lambda v: type(v).__name__)
def test_each_validator_is_total(value):
    for validator in (hmac_secret_violations, hmac_key_id_violations, hmac_sunset_violations):
        problems = validator(value, "field")
        assert isinstance(problems, list) and problems, f"{validator.__name__} accepted {value!r}"
    assert hmac_extra_key_violations(value, "active") or value == {}


@pytest.mark.parametrize("secret", ["", "   ", "\t\n", "short", " " * 40, "x" * 31])
def test_weak_and_blank_secrets_are_refused(secret):
    assert hmac_secret_violations(secret, "s")


@pytest.mark.parametrize("secret", ["x" * 32, "x" * 64, "a-b_c" * 16])
def test_real_secrets_are_accepted(secret):
    assert hmac_secret_violations(secret, "s") == []


def test_a_padded_secret_is_refused_because_the_padding_is_part_of_the_key():
    assert any("whitespace" in v for v in hmac_secret_violations(" " + "x" * 32 + " ", "s"))


@pytest.mark.parametrize("iso", ["", "   ", "not-a-date", "2026-09-01T00:00:00", 5, None, {}])
def test_bad_sunsets_are_refused(iso):
    assert hmac_sunset_violations(iso, "sunset")


def test_a_real_sunset_is_accepted():
    assert hmac_sunset_violations("2026-09-01T00:00:00Z", "sunset") == []


# ── the user-visible consequence: 401, never 500 ───────────────────────────────────────────────────
@pytest.mark.parametrize("value", HOSTILE_VALUES, ids=lambda v: type(v).__name__)
def test_a_malformed_active_secret_yields_401_not_500(value):
    """The whole point of failing closed in `_inbound_secret`: verification refuses rather than
    raising out of `.encode()` with the request already authenticated as far as the caller knows."""
    from fastapi import HTTPException

    settings = hardened().model_copy(update={"hmac_inbound_secret": value})

    class _App:
        class state:
            session_factory = None

    class _Req:
        headers = {"X-KYC-Key-Id": "kyc-platform-1", "X-KYC-Signature-V2": "deadbeef",
                   "X-KYC-Timestamp": "1754400000", "Idempotency-Key": "k"}
        method = "POST"
        app = _App()
        scope = {"path": "/v1/cases/c/events", "query_string": b"",
                 "raw_path": b"/v1/cases/c/events"}

        class url:
            path = "/v1/cases/c/events"
            query = ""

    with pytest.raises(HTTPException) as excinfo:
        auth.require_valid_signature(settings, _Req(), b"{}")
    assert excinfo.value.status_code == 401


# ── active key ids use the grammar at CONSTRUCTION, not only at the boundary ───────────────────────
@pytest.mark.parametrize("key_id", [" padded ", "\t", "  ", "kéy", "k" * 129, "with\x00null"])
@pytest.mark.parametrize("field", ["hmac_inbound_key_id", "hmac_outbound_key_id"])
def test_active_key_ids_are_refused_at_construction(field, key_id):
    """Re-audit F7. These constructed cleanly and were caught only in production — so a rotation
    rehearsed successfully in staging failed at the production cutover, which is backwards."""
    with pytest.raises(ValidationError):
        hardened(**{field: key_id})


@pytest.mark.parametrize("field", ["hmac_inbound_key_id", "hmac_outbound_key_id"])
def test_the_unconfigured_default_still_constructs(field):
    """Empty is the unset default outside production; the boundary refuses it there."""
    Settings(**{field: ""})
    assert production_config_violations(hardened(**{field: ""}))


# ── no credential reaches any rendering of a validation failure ────────────────────────────────────
def test_no_rendering_of_a_load_failure_carries_the_secret(monkeypatch):
    """`hide_input_in_errors=True` cleans `str` and `repr`. It does NOT clean `errors()` or
    `json()` (re-audit F6), so the loader converts the failure into a sanitized error and drops
    the original from the exception chain."""
    monkeypatch.setenv("KYC_HMAC_INBOUND_KEY_ID", " padded ")
    monkeypatch.setenv("KYC_HMAC_INBOUND_EXTRA_KEYS", f'{{" padded ": "{SENTINEL}"}}')

    from kyc_tool.config import get_settings

    with pytest.raises(ConfigLoadError) as excinfo:
        get_settings()

    error = excinfo.value
    assert SENTINEL not in str(error)
    assert SENTINEL not in repr(error)
    assert SENTINEL not in str(error.args)
    # The formatted traceback is what actually reaches a log, CI annotation, or incident
    # transcript — assert on that rather than on the chain fields, since `raise ... from None`
    # leaves `__context__` populated and suppresses only its PRINTING.
    rendered = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    assert SENTINEL not in rendered, "the credential reaches a printed traceback"
    assert error.__cause__ is None
    assert error.__suppress_context__ is True
    # and it still says something useful
    assert "hmac_inbound" in str(error)


def test_the_raw_validation_error_would_have_leaked_it(monkeypatch):
    """The guard's own failure mode, demonstrated: this is what escapes without the loader."""
    monkeypatch.setenv("KYC_HMAC_INBOUND_KEY_ID", "active")
    monkeypatch.setenv("KYC_HMAC_INBOUND_EXTRA_KEYS", f'{{" padded ": "{SENTINEL}"}}')
    with pytest.raises(ValidationError) as excinfo:
        Settings()
    # str/repr are clean thanks to hide_input_in_errors...
    assert SENTINEL not in str(excinfo.value)
    # ...and this is the hole F6 reported, which is why nothing may surface a raw one
    assert SENTINEL in excinfo.value.json()
