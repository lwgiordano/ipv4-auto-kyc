"""Re-audit `03dbfab..bc325e7` R5-F6/F8/F11: the numeric registry must cover the NESTED adapter-rate
sink, reject Python bool at construction, enforce the 72h security TTL in production, and aggregate
malformed values into one ProductionConfigError instead of crashing."""

import pytest
from pydantic import ValidationError

from kyc_tool.config import (
    NUMERIC_SETTINGS,
    ProductionConfigError,
    Settings,
    adapter_rate_violations,
    production_config_violations,
    production_numeric_violations,
    validate_for_production,
)
from kyc_tool.orchestration.rate_limit import RateLimiter


def _hardened(**overrides):
    base = dict(
        environment="production",
        auth_disabled=False,
        platform_hmac_secret="s" * 40,
        platform_callback_url="https://platform.example/kyc",
        object_store="s3",
        s3_bucket="kyc-evidence",
        ocr_engine="tesseract",
        email_provider="ses",
        adapters_profile="real",
        read_auth_required=True,
        ui_enabled=False,
        ui_admin_token="t" * 32,
        hmac_inbound_key_id="k-in",
        hmac_inbound_secret="i" * 40,
        hmac_outbound_key_id="k-out",
        hmac_outbound_secret="o" * 40,
        hmac_v1_inbound_sunset_at="2030-01-01T00:00:00+00:00",
        hmac_v1_outbound_sunset_at="2030-01-01T00:00:00+00:00",
        hmac_v1_observation_window_days=14,
    )
    base.update(overrides)
    return Settings(**base)


# ── R5-F6: nested adapter-rate sink + bool coercion ────────────────────────────────────────────────
@pytest.mark.parametrize("rate", [float("nan"), float("inf"), float("-inf"), -1, 0, 1e-9, 999_999])
def test_bad_adapter_rate_is_refused_at_construction(rate):
    with pytest.raises(ValidationError):
        Settings(adapter_rate_limits={"rir_rdap": rate})


def test_valid_adapter_rates_via_the_real_env_parser(monkeypatch):
    monkeypatch.setenv("KYC_ADAPTER_RATE_LIMITS", '{"rir_rdap": 2, "companies_house": 5}')
    assert Settings().adapter_rate_limits == {"rir_rdap": 2.0, "companies_house": 5.0}


def test_negative_adapter_rate_via_the_real_env_parser_is_refused(monkeypatch):
    monkeypatch.setenv("KYC_ADAPTER_RATE_LIMITS", '{"rir_rdap": -1}')
    with pytest.raises(ValidationError):
        Settings()


def test_rate_limiter_consumer_rejects_a_bad_mapping():
    with pytest.raises(ValueError):
        RateLimiter({"rir_rdap": float("nan")})
    with pytest.raises(ValueError):
        RateLimiter({"rir_rdap": 1e-9})
    RateLimiter({"rir_rdap": 2.0})  # a valid mapping constructs


def test_adapter_rate_violations_helper_flags_each_hazard():
    for bad in ({"a": float("nan")}, {"a": -1}, {"a": 0}, {"a": 1e-9}, {"a": 999_999}, {"a": True}):
        assert adapter_rate_violations(bad)


@pytest.mark.parametrize("ns", NUMERIC_SETTINGS, ids=lambda ns: ns.name)
def test_bool_is_refused_for_every_numeric_field_at_construction(ns):
    # bool is an int subclass; Pydantic would coerce True->1, collapsing the horizon (R5-F6).
    with pytest.raises(ValidationError):
        Settings(**{ns.name: True})


# ── R5-F8: the 72h security/business TTL is a production contract, not a storage ceiling ────────────
def test_production_requires_exactly_72h_token_ttl():
    for bad in (71, 73, 87_600):
        hits = production_numeric_violations(Settings().model_copy(update={"poc_token_ttl_hours": bad}))
        assert any("poc_token_ttl_hours" in m and "72" in m for m in hits), bad
    ok = production_numeric_violations(Settings().model_copy(update={"poc_token_ttl_hours": 72}))
    assert not any("poc_token_ttl_hours" in m for m in ok)


# ── R5-F11: malformed values aggregate into one error, never a raw TypeError ────────────────────────
@pytest.mark.parametrize(
    "field", ["retention_days", "worker_poll_seconds", "hmac_v1_observation_window_days"]
)
def test_malformed_numeric_yields_aggregate_not_crash(field):
    leaked = _hardened().model_copy(update={field: "bad"})  # model_copy bypasses field validation
    problems = production_config_violations(leaked)  # must NOT raise a TypeError
    assert any(field in m for m in problems)
    with pytest.raises(ProductionConfigError):
        validate_for_production(leaked)
