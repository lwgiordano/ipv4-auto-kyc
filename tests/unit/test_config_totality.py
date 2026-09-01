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

import contextlib
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
    # The CHAIN, not just the printed form. `raise ... from None` inside the handler suppresses
    # printing while leaving the raw error attached to `__context__`, and structured collectors
    # walk that chain (re-audit `4f23f23..122cc67` finding 7). So: nothing attached at all.
    assert error.__cause__ is None
    assert error.__context__ is None, "the raw secret-bearing error is still on the chain"

    # walk everything reachable, the way an APM collector would
    def _reachable(exc, depth=0):
        if exc is None or depth > 5:
            return []
        blob = [str(exc), repr(exc), str(getattr(exc, "args", ""))]
        for attribute in ("json", "errors"):
            method = getattr(exc, attribute, None)
            if callable(method):
                with contextlib.suppress(Exception):  # probing, not asserting
                    blob.append(str(method()))
        return blob + _reachable(exc.__cause__, depth + 1) + _reachable(exc.__context__, depth + 1)

    assert not any(SENTINEL in part for part in _reachable(error))
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


# ── hostile SUBCLASSES: isinstance handed the boundary's logic to the value it was judging ────────
class EvilStr(str):
    """A `str` subclass that controls every operation the boundary performs on it."""

    def strip(self, *a, **k):
        raise RuntimeError("hostile strip")

    def __eq__(self, other):
        raise RuntimeError("hostile eq")

    def __hash__(self):
        return 0

    def encode(self, *a, **k):
        raise RuntimeError("hostile encode")


class EvilDict(dict):
    def items(self):
        raise RuntimeError("hostile items")

    def get(self, *a, **k):
        raise RuntimeError("hostile get")

    def __eq__(self, other):
        raise RuntimeError("hostile eq")

    def __hash__(self):
        return 0

    def __bool__(self):
        raise RuntimeError("hostile bool")


HOSTILE_SUBCLASSES = [EvilStr("x" * 40), EvilDict({"old": "s" * 32})]


@pytest.mark.parametrize("value", HOSTILE_SUBCLASSES, ids=["EvilStr", "EvilDict"])
def test_the_validators_survive_hostile_subclasses(value):
    """`isinstance` accepts a subclass, so the value's own `strip`/`items`/`__eq__` ran INSIDE the
    check meant to judge it (re-audit `4f23f23..122cc67` finding 6). Exact type checks refuse it
    before touching it."""
    for validator in (hmac_secret_violations, hmac_key_id_violations, hmac_sunset_violations):
        problems = validator(value, "field")
        assert isinstance(problems, list) and problems
    assert hmac_extra_key_violations(value, "active")


@pytest.mark.parametrize("field", STRING_FIELDS + ["hmac_inbound_extra_keys"])
def test_the_boundary_survives_hostile_subclasses_on_every_field(field):
    hostile = EvilDict({"old": "s" * 32}) if "extra_keys" in field else EvilStr("x" * 40)
    violations = production_config_violations(hardened().model_copy(update={field: hostile}))
    assert isinstance(violations, list) and violations


def test_no_violation_carries_exception_text():
    """The aggregate interpolated `str(exc)` — which on a hostile value is that value's own
    `__str__`, and on a Pydantic failure routinely contains the rejected value itself."""
    class Talkative:
        def __len__(self):
            raise RuntimeError(f"secret is {SENTINEL}")

        def __str__(self):
            return SENTINEL

        __repr__ = __str__

    violations = production_config_violations(
        hardened().model_copy(update={"hmac_inbound_secret": Talkative()}))
    assert violations
    assert not any(SENTINEL in item for item in violations), violations


def test_one_bad_section_does_not_hide_violations_in_another():
    """A hostile value used to abort the whole aggregate, so a genuinely broken setting later in
    the list was never reported and the operator fixed one thing at a time across reboots."""
    smuggled = hardened().model_copy(update={
        "hmac_inbound_secret": EvilStr("x" * 40),  # breaks the HMAC section
        "object_store": "fs",                       # an ordinary, unrelated violation
    })
    violations = production_config_violations(smuggled)
    assert any("object_store" in item for item in violations), (
        f"a later independent violation was hidden by an earlier failure: {violations}"
    )


def test_a_malformed_active_secret_yields_401_for_hostile_subclasses_too():
    from fastapi import HTTPException

    settings = hardened().model_copy(update={"hmac_inbound_secret": EvilStr("x" * 40)})

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


# ── duplicate rotation key ids, at every text source ─────────────────────────────────────────────
_SECRET_A, _SECRET_B = "a" * 34, "b" * 34
DUPLICATE_JSON = f'{{"old": "{_SECRET_A}", "old": "{_SECRET_B}"}}'
SINGLE_JSON = f'{{"old": "{_SECRET_A}"}}'


def test_a_duplicate_key_in_the_process_environment_is_refused(monkeypatch):
    from kyc_tool.config import get_settings

    monkeypatch.setenv("KYC_HMAC_INBOUND_EXTRA_KEYS", DUPLICATE_JSON)
    with pytest.raises(Exception) as excinfo:  # noqa: B017 — pydantic-settings wraps it
        Settings()
    assert "repeats key" in str(excinfo.value.__cause__)
    # and the OPERATOR-facing loader says why, not just "error parsing value"
    with pytest.raises(ConfigLoadError, match="repeats key"):
        get_settings()


def test_a_duplicate_key_in_a_dotenv_file_is_refused(tmp_path, monkeypatch):
    """The case the previous `os.environ` check missed entirely — and a `.env` file is the
    documented way to configure this (re-audit `4f23f23..122cc67` finding 8)."""
    monkeypatch.delenv("KYC_HMAC_INBOUND_EXTRA_KEYS", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(f"KYC_HMAC_INBOUND_EXTRA_KEYS={DUPLICATE_JSON}\n")
    with pytest.raises(Exception) as excinfo:  # noqa: B017
        Settings(_env_file=str(env_file))
    assert "repeats key" in str(excinfo.value.__cause__)


def test_a_non_duplicate_value_still_round_trips(tmp_path, monkeypatch):
    """Guard the guard: a check that refused everything would pass the two tests above."""
    monkeypatch.delenv("KYC_HMAC_INBOUND_EXTRA_KEYS", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(f"KYC_HMAC_INBOUND_EXTRA_KEYS={SINGLE_JSON}\n")
    assert Settings(_env_file=str(env_file)).hmac_inbound_extra_keys == {"old": "a" * 34}

    monkeypatch.setenv("KYC_HMAC_INBOUND_EXTRA_KEYS", SINGLE_JSON)
    assert Settings().hmac_inbound_extra_keys == {"old": "a" * 34}


def test_the_duplicate_check_reads_the_selected_source_not_the_process_environment(
        tmp_path, monkeypatch):
    """An explicit env file takes precedence over the process environment, so a duplicate there
    must be caught even when an unrelated clean value is exported."""
    monkeypatch.setenv("KYC_HMAC_INBOUND_EXTRA_KEYS", SINGLE_JSON)
    env_file = tmp_path / ".env"
    env_file.write_text(f"KYC_HMAC_INBOUND_EXTRA_KEYS={DUPLICATE_JSON}\n")
    with pytest.raises(Exception) as excinfo:  # noqa: B017
        Settings(_env_file=str(env_file))
    assert "repeats key" in str(excinfo.value.__cause__)


def test_a_duplicate_key_in_a_secrets_directory_file_is_refused(tmp_path, monkeypatch):
    """The source that was left unwrapped while the docstring claimed every text source was
    covered (re-audit `4c3015a..cccd5f7` F2).

    This is not an exotic path. A secrets directory is how containers and Kubernetes normally
    deliver credentials, so a file named for the rotation map is the NORMAL way to configure it in
    the deployment this document describes — and a repeated key id there constructed cleanly while
    discarding one side of a live rotation. The peer still signing with the dropped secret starts
    failing verification with nothing in the logs to explain it.
    """
    from kyc_tool.config import get_settings

    monkeypatch.delenv("KYC_HMAC_INBOUND_EXTRA_KEYS", raising=False)
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "KYC_HMAC_INBOUND_EXTRA_KEYS").write_text(DUPLICATE_JSON)
    with pytest.raises(Exception) as excinfo:  # noqa: B017 — pydantic-settings wraps it
        Settings(_secrets_dir=str(secrets), _env_file=None)
    assert "repeats key" in str(excinfo.value.__cause__)
    # the operator-facing reason survives this source too, not just env and dotenv
    monkeypatch.setattr(Settings, "model_config", {**Settings.model_config,
                                                  "secrets_dir": str(secrets)})
    with pytest.raises(ConfigLoadError, match="repeats key"):
        get_settings()


def test_a_clean_secrets_directory_file_still_round_trips(tmp_path, monkeypatch):
    """Guard the guard on the new source: refusing everything would pass the test above."""
    monkeypatch.delenv("KYC_HMAC_INBOUND_EXTRA_KEYS", raising=False)
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "KYC_HMAC_INBOUND_EXTRA_KEYS").write_text(SINGLE_JSON)
    loaded = Settings(_secrets_dir=str(secrets), _env_file=None)
    assert loaded.hmac_inbound_extra_keys == {"old": "a" * 34}


def test_an_empty_higher_precedence_map_REVOKES_a_lower_source_key(tmp_path, monkeypatch):
    """Gate finding 3, and a correction of what this file previously ASSERTED.

    Pydantic deep-merges dict fields BETWEEN sources. An earlier version of this test recorded that
    behaviour as expected and called it operationally interesting. It is not merely interesting: for
    a CREDENTIAL ROTATION MAP it means revocation silently fails. An operator who sets
    `KYC_HMAC_INBOUND_EXTRA_KEYS={}` to retire a key gets it back from a lower-precedence secrets
    file, and `_inbound_secret` keeps authenticating requests signed with it. I found the merge,
    documented it, and blessed it; the auditor was right that an empty higher-precedence map must be
    able to revoke.

    So this field has WHOLE-FIELD REPLACEMENT semantics: the highest-precedence source that defines
    it wins outright, and lower sources contribute nothing to it.
    """
    from kyc_tool.api.auth import _inbound_secret

    monkeypatch.delenv("KYC_HMAC_INBOUND_EXTRA_KEYS", raising=False)
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "KYC_HMAC_INBOUND_EXTRA_KEYS").write_text(SINGLE_JSON)  # {"old": "a"*34}

    revoked = Settings(hmac_inbound_extra_keys={}, _secrets_dir=str(secrets), _env_file=None)
    assert revoked.hmac_inbound_extra_keys == {}, "the retired key came back from a lower source"
    assert _inbound_secret(revoked, "old") == "", "a retired key still resolves to a secret"


def test_an_empty_env_map_revokes_a_secrets_file_key(tmp_path, monkeypatch):
    """The same revocation through the env source, which is how an operator would actually do it."""
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "KYC_HMAC_INBOUND_EXTRA_KEYS").write_text(SINGLE_JSON)
    monkeypatch.setenv("KYC_HMAC_INBOUND_EXTRA_KEYS", "{}")
    loaded = Settings(_secrets_dir=str(secrets), _env_file=None)
    assert loaded.hmac_inbound_extra_keys == {}


def test_a_higher_precedence_map_replaces_rather_than_merges(tmp_path, monkeypatch):
    """New-only at the top must not resurrect the old key underneath it — the exact shape of a
    completed rotation."""
    monkeypatch.delenv("KYC_HMAC_INBOUND_EXTRA_KEYS", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(f"KYC_HMAC_INBOUND_EXTRA_KEYS={SINGLE_JSON}\n")  # {"old": ...}
    loaded = Settings(hmac_inbound_extra_keys={"new": "n" * 34}, _env_file=str(env_file))
    assert loaded.hmac_inbound_extra_keys == {"new": "n" * 34}


def test_a_same_key_override_still_wins_and_a_lower_source_still_loads(tmp_path, monkeypatch):
    """Guard the guard: replacement must not become "ignore lower sources entirely". With nothing
    defined higher, the secrets file is still the value."""
    monkeypatch.delenv("KYC_HMAC_INBOUND_EXTRA_KEYS", raising=False)
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "KYC_HMAC_INBOUND_EXTRA_KEYS").write_text(SINGLE_JSON)
    assert Settings(_secrets_dir=str(secrets),
                    _env_file=None).hmac_inbound_extra_keys == {"old": "a" * 34}
    same = Settings(hmac_inbound_extra_keys={"old": "z" * 34},
                    _secrets_dir=str(secrets), _env_file=None)
    assert same.hmac_inbound_extra_keys == {"old": "z" * 34}


def test_rate_limits_keep_their_merge_semantics(tmp_path, monkeypatch):
    """Replacement is scoped to the credential map. `adapter_rate_limits` is not a revocation
    surface, and changing its semantics was explicitly out of scope."""
    monkeypatch.delenv("KYC_ADAPTER_RATE_LIMITS", raising=False)
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "KYC_ADAPTER_RATE_LIMITS").write_text('{"rir_rdap": 1.0}')
    merged = Settings(adapter_rate_limits={"gleif": 2.0},
                      _secrets_dir=str(secrets), _env_file=None)
    assert merged.adapter_rate_limits == {"gleif": 2.0, "rir_rdap": 1.0}


def test_every_mapping_shaped_field_has_a_declared_duplicate_policy():
    """The closure claim, now actually closed (gate finding 7).

    The first version detected only annotations whose DIRECT `__origin__` was `dict` or `list`, so a
    synthetic `dict[str, str] | None` satisfied the guard while sitting outside the checked set —
    the silently-exempt hole that scoping the decoder was meant to avoid. Discovery is now recursive
    through `get_origin`/`get_args`, and every mapping-shaped field must be either checked or
    exempt-with-a-reason.
    """
    from kyc_tool.config import (
        DUPLICATE_CHECKED_FIELDS,
        DUPLICATE_EXEMPT_FIELDS,
        mapping_shaped_fields,
    )

    _assert_duplicate_policy_partition(
        mapping_shaped_fields(Settings), DUPLICATE_CHECKED_FIELDS, DUPLICATE_EXEMPT_FIELDS)


def _assert_duplicate_policy_partition(discovered, checked, exempt) -> None:
    """The closure is a PARTITION, not a union (re-gate-3 finding 5). `checked | exempt` covering
    the discovered set proved nothing about the sets themselves: a field in both was fine, and a
    blank exemption reason satisfied the prose promise that every exemption carries one. Factored
    out so the mutation tests below run THIS check, not a restatement of it."""
    overlap = set(checked) & set(exempt)
    assert not overlap, f"field(s) both checked and exempt: {sorted(overlap)}"
    for field, reason in dict(exempt).items():
        assert isinstance(reason, str) and reason.strip(), (
            f"exempt field {field!r} carries no reason; an unexplained exemption is a silent hole"
        )
    classified = set(checked) | set(exempt)
    unclassified = set(discovered) - classified
    assert not unclassified, (
        f"mapping-shaped settings field(s) with no duplicate policy: {sorted(unclassified)}. "
        "Add them to DUPLICATE_CHECKED_FIELDS, or to DUPLICATE_EXEMPT_FIELDS with a reason."
    )
    stale = classified - set(discovered)
    assert not stale, f"policy declared for non-mapping field(s): {sorted(stale)}"


def test_an_overlapping_partition_is_refused():
    with pytest.raises(AssertionError, match="both checked and exempt"):
        _assert_duplicate_policy_partition({"a", "b"}, {"a", "b"}, {"b": "reason"})


def test_a_blank_exemption_reason_is_refused():
    with pytest.raises(AssertionError, match="no reason"):
        _assert_duplicate_policy_partition({"a", "b"}, {"a"}, {"b": "   "})


@pytest.mark.parametrize("annotation,expected", [
    ("dict[str, str]", True),
    ("dict[str, str] | None", True),
    ("Optional[dict[str, str]]", True),
    ("Annotated[dict[str, str], 'meta']", True),
    ("Annotated[dict[str, str] | None, 'meta']", True),
    ("dict[str, dict[str, int]]", True),
    ("str | int | dict[str, str]", True),
    ("list[str]", False),
    ("list[str] | None", False),
    ("str", False),
    ("int | None", False),
])
def test_mapping_discovery_sees_through_optional_annotated_and_unions(annotation, expected):
    """Codex's synthetic specimens, plus the negative half. Lists must NOT be swept in: duplicate
    detection is a policy about repeated OBJECT KEYS, and a JSON array has none."""
    from typing import Annotated, Optional  # noqa: F401 — referenced by eval'd annotations

    from kyc_tool.config import _carries_mapping

    assert _carries_mapping(eval(annotation)) is expected


def test_a_synthetic_optional_mapping_field_would_fail_the_closure_guard():
    """The guard must FAIL for an undeclared mapping, not merely pass for the current two."""
    from kyc_tool.config import (
        DUPLICATE_CHECKED_FIELDS,
        DUPLICATE_EXEMPT_FIELDS,
        mapping_shaped_fields,
    )

    class _Future(Settings):
        future_map: dict[str, str] | None = None

    discovered = mapping_shaped_fields(_Future)
    assert "future_map" in discovered, "recursive discovery missed an Optional mapping"
    classified = set(DUPLICATE_CHECKED_FIELDS) | set(DUPLICATE_EXEMPT_FIELDS)
    assert discovered - classified == {"future_map"}, (
        "the closure guard would not have forced a decision on this field"
    )


def test_a_duplicate_key_in_the_rate_limit_map_is_refused(monkeypatch):
    """The second complex field, through the same decoder."""
    monkeypatch.setenv("KYC_ADAPTER_RATE_LIMITS", '{"rir_rdap": 1.0, "rir_rdap": 2.0}')
    with pytest.raises(Exception) as excinfo:  # noqa: B017
        Settings()
    assert "repeats key" in str(excinfo.value.__cause__)


def test_the_helper_is_total_over_junk():
    from kyc_tool.config import _refuse_duplicate_json_keys

    for value in (None, 5, [], {}, "not json", "{malformed", b"{}"):
        _refuse_duplicate_json_keys("field", value)  # must not raise


# ── re-gate finding 6: abstract mappings are mapping-shaped too ───────────────────────────────
@pytest.mark.parametrize("label,annotation", [
    ("Mapping", "Mapping[str, str]"),
    ("MutableMapping", "MutableMapping[str, str]"),
    ("optional Mapping", "Mapping[str, str] | None"),
    ("optional MutableMapping", "MutableMapping[str, str] | None"),
    ("Annotated Mapping", "Annotated[Mapping[str, str], 'meta']"),
    ("nested union Mapping", "str | Mapping[str, str] | None"),
])
def test_abstract_mapping_annotations_are_discovered(label, annotation):
    """The closure claimed "every mapping-shaped field" and detected concrete `dict` only, so
    `Mapping`/`MutableMapping` fields would parse last-wins while sitting outside the checked set."""
    from collections.abc import Mapping, MutableMapping  # noqa: F401 — used by eval
    from typing import Annotated  # noqa: F401 — used by eval

    from kyc_tool.config import _carries_mapping

    assert _carries_mapping(eval(annotation)) is True


def test_a_synthetic_abstract_mapping_field_forces_a_policy_decision():
    """Mutating the REAL Settings subclass the closure assertion runs over, not `_carries_mapping`
    in isolation — Codex asked for the assertion itself to bite."""
    from collections.abc import Mapping

    from kyc_tool.config import (
        DUPLICATE_CHECKED_FIELDS,
        DUPLICATE_EXEMPT_FIELDS,
        mapping_shaped_fields,
    )

    class _Future(Settings):
        future_abstract: Mapping[str, str] | None = None

    discovered = mapping_shaped_fields(_Future)
    assert "future_abstract" in discovered
    classified = set(DUPLICATE_CHECKED_FIELDS) | set(DUPLICATE_EXEMPT_FIELDS)
    assert discovered - classified == {"future_abstract"}


# ── re-gate finding 7: duplicates are per-object, not per-parse ───────────────────────────────
@pytest.mark.parametrize("label,payload,refused", [
    ("separate nested objects", '{"a": {"x": 1}, "b": {"x": 2}}', False),
    ("same nested object", '{"a": {"x": 1, "x": 2}}', True),
    ("top level", '{"x": 1, "x": 2}', True),
    ("same key different depths", '{"x": 1, "a": {"x": 2}}', False),
    ("deep separate objects", '{"a": {"b": {"x": 1}}, "c": {"d": {"x": 2}}}', False),
    ("deep same object", '{"a": {"b": {"x": 1, "x": 2}}}', True),
    # Array roots (re-gate-3 finding 5): the old `startswith("{")` guard let a JSON array skip the
    # check entirely, so a duplicate INSIDE `[{"a":1,"a":2}]` parsed last-key-wins unexamined.
    ("array root, same object", '[{"a": 1, "a": 2}]', True),
    ("array root, sibling objects", '[{"a": 1}, {"a": 2}]', False),
    ("array nested in object", '{"outer": [{"a": 1, "a": 2}]}', True),
    ("object nested in array in object", '{"o": [[{"a": 1, "a": 2}]]}', True),
    ("scalar root", '42', False),
    ("non-JSON string", 'not json at all', False),
])
def test_duplicate_detection_is_per_object(label, payload, refused):
    """The global `object_pairs_hook` list conflated independent objects, so
    `{"a":{"x":1},"b":{"x":2}}` — which repeats nothing — was REJECTED. Refusing a legal config is
    the failure direction that breaks a deployment rather than merely admitting a bad one."""
    from kyc_tool.config import DuplicateKeyError, _refuse_duplicate_json_keys

    if refused:
        with pytest.raises(DuplicateKeyError):
            _refuse_duplicate_json_keys("adapter_rate_limits", payload)
    else:
        _refuse_duplicate_json_keys("adapter_rate_limits", payload)
