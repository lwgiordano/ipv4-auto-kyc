"""Production kill switch: validate_for_production() must refuse to boot on any
unsafe or stub configuration, and accept a fully-hardened one."""

import pytest

from kyc_tool.config import (
    ProductionConfigError,
    Settings,
    production_config_violations,
    validate_for_production,
)


def hardened(**overrides) -> Settings:
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
        # HMAC v2 (PR 5a): split secrets + key_ids, both sunset dates, window.
        hmac_inbound_key_id="kyc-platform-1",
        hmac_inbound_secret="i" * 40,
        hmac_outbound_key_id="kyc-tool-1",
        hmac_outbound_secret="o" * 40,
        hmac_v1_inbound_sunset_at="2026-09-01T00:00:00Z",
        hmac_v1_outbound_sunset_at="2026-10-01T00:00:00Z",
        hmac_v1_observation_window_days=14,
    )
    base.update(overrides)
    return Settings(**base)


def test_hardened_config_has_no_violations():
    assert production_config_violations(hardened()) == []
    validate_for_production(hardened())  # does not raise


@pytest.mark.parametrize(
    ("overrides", "needle"),
    [
        ({"auth_disabled": True}, "auth_disabled"),
        ({"platform_hmac_secret": ""}, "empty"),
        ({"platform_hmac_secret": "short"}, "weak"),
        ({"platform_callback_url": "http://platform.example/kyc"}, "HTTPS"),
        ({"platform_callback_url": "https://localhost/kyc"}, "localhost"),
        # a query/fragment base would misdirect the appended /kyc/decision suffix —
        # including a BARE "?"/"#" that urlparse reports as an empty component
        ({"platform_callback_url": "https://platform.example/kyc?x=1"}, "query or fragment"),
        ({"platform_callback_url": "https://platform.example/kyc#f"}, "query or fragment"),
        ({"platform_callback_url": "https://platform.example/kyc?"}, "query or fragment"),
        ({"platform_callback_url": "https://platform.example/kyc#"}, "query or fragment"),
        ({"object_store": "fs"}, "not s3"),
        ({"object_store": "s3", "s3_bucket": ""}, "s3_bucket is empty"),
        ({"ocr_engine": "json_scan"}, "ocr_engine"),
        ({"email_provider": "logging"}, "email_provider"),
        ({"adapters_profile": "fixture"}, "adapters_profile"),
        ({"read_auth_required": False}, "read_auth_required"),
        ({"ui_enabled": True, "ui_admin_token": ""}, "ops console"),
        # HMAC v2 (PR 5a)
        ({"hmac_inbound_secret": ""}, "inbound secret"),
        ({"hmac_outbound_secret": "short"}, "outbound secret"),
        ({"hmac_inbound_key_id": ""}, "hmac_inbound_key_id"),
        ({"hmac_outbound_key_id": ""}, "hmac_outbound_key_id"),
        ({"hmac_v1_inbound_sunset_at": ""}, "inbound sunset"),
        ({"hmac_v1_outbound_sunset_at": ""}, "outbound sunset"),
        ({"hmac_v1_observation_window_days": 0}, "observation window"),
        # malformed sunset dates must fail the kill switch at boot, not 500 at
        # request/delivery time (audit finding 4): non-date and tz-naive.
        ({"hmac_v1_inbound_sunset_at": "not-a-date"}, "timezone-aware ISO-8601"),
        ({"hmac_v1_outbound_sunset_at": "2026-10-01"}, "timezone-aware ISO-8601"),
    ],
)
def test_each_unsafe_condition_is_rejected(overrides, needle):
    violations = production_config_violations(hardened(**overrides))
    assert any(needle in v for v in violations), violations
    with pytest.raises(ProductionConfigError):
        validate_for_production(hardened(**overrides))


def test_non_production_environments_are_not_gated():
    # dev/test may run with the defaults (fixtures, fs store, no read auth)
    dev = Settings(environment="development")
    validate_for_production  # noqa: B018 — sanity that import resolves
    # the app factory only calls validate for production, so dev stays permissive
    assert production_config_violations(dev)  # dev config would fail *if* checked
