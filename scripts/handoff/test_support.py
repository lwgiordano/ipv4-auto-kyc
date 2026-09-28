"""Small production-shaped settings helpers used by the exported operational tests."""

from datetime import UTC, datetime, timedelta

from kyc_tool.config import Settings


def unarrived_sunset(days: int) -> str:
    """Return a timezone-aware specimen date that remains in the future."""
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


def hardened(**overrides) -> Settings:
    """Build a production-shaped configuration for refusal-path tests."""
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
        floqer_api_key="floq_placeholder-not-a-real-key",
        floqer_shortcut_id="00000000-0000-0000-0000-000000000000",
        hmac_inbound_key_id="kyc-platform-1",
        hmac_inbound_secret="i" * 40,
        hmac_outbound_key_id="kyc-tool-1",
        hmac_outbound_secret="o" * 40,
        hmac_v1_inbound_sunset_at=unarrived_sunset(365),
        hmac_v1_outbound_sunset_at=unarrived_sunset(395),
        hmac_v1_observation_window_days=14,
    )
    base.update(overrides)
    return Settings(**base)
