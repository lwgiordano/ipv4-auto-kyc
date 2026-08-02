"""Re-audit `5b0f0b8..b75a320` close-out #1: the numeric-setting registry is the single authority
for every numeric config domain. This guard proves (a) EVERY numeric Settings field is registered —
so a new numeric setting cannot ship without a declared domain; (b) the DECLARATION layer (Pydantic
Field) rejects out-of-domain values at construction; (c) the BOUNDARY layer
(`numeric_domain_violations`) catches an unvalidated `model_copy` that bypassed the field; and (d) the
production-only rules (exact replay window, positive leases/TTLs/retention) hold. Per-consumer
(direct-call) layers are proven in the F1/F2/F3 suites.
"""

import pytest
from pydantic import ValidationError

from kyc_tool.config import (
    MAX_HMAC_SKEW_SECONDS,
    NUMERIC_SETTINGS,
    Settings,
    numeric_domain_violations,
    production_config_violations,
    production_numeric_violations,
)


def test_every_numeric_field_is_registered():
    numeric_fields = {
        name for name, f in Settings.model_fields.items() if f.annotation in (int, float)
    }
    registered = {ns.name for ns in NUMERIC_SETTINGS}
    assert numeric_fields == registered, (
        f"registry drift — unregistered numeric settings: {numeric_fields - registered}; "
        f"registered non-fields: {registered - numeric_fields}"
    )


def test_defaults_are_in_domain():
    assert numeric_domain_violations(Settings()) == []


@pytest.mark.parametrize("ns", NUMERIC_SETTINGS, ids=lambda ns: ns.name)
def test_field_rejects_out_of_domain_at_construction(ns):
    """Declaration layer: the Pydantic field itself refuses values outside [floor, ceiling]."""
    # a clearly-below value (floor - 1) and a clearly-above value (ceiling + 1) must both fail
    with pytest.raises(ValidationError):
        Settings(**{ns.name: ns.floor - 1})
    with pytest.raises(ValidationError):
        Settings(**{ns.name: ns.ceiling + 1})
    # the endpoints themselves construct
    assert getattr(Settings(**{ns.name: ns.floor}), ns.name) == ns.floor


@pytest.mark.parametrize("ns", NUMERIC_SETTINGS, ids=lambda ns: ns.name)
def test_boundary_layer_catches_model_copy_bypass(ns):
    """`model_copy(update=...)` does NOT re-run field validation — the registry boundary check must
    still catch an out-of-domain value it lets through."""
    leaked = Settings().model_copy(update={ns.name: ns.ceiling + 1})
    assert any(ns.name in m for m in numeric_domain_violations(leaked))


def test_non_finite_float_is_refused():
    for bad in (float("nan"), float("inf"), float("-inf")):
        leaked = Settings().model_copy(update={"worker_poll_seconds": bad})
        assert any("worker_poll_seconds" in m for m in numeric_domain_violations(leaked))


def test_bool_is_not_a_valid_numeric_value():
    leaked = Settings().model_copy(update={"retention_days": True})
    assert any("retention_days" in m for m in numeric_domain_violations(leaked))


def test_production_requires_the_exact_governed_replay_window():
    prod = Settings().model_copy(update={"hmac_max_skew_seconds": 200})
    hits = production_numeric_violations(prod)
    assert any("hmac_max_skew_seconds" in m and str(MAX_HMAC_SKEW_SECONDS) in m for m in hits)
    # the exact governed value passes
    ok = Settings().model_copy(update={"hmac_max_skew_seconds": MAX_HMAC_SKEW_SECONDS})
    assert not any("hmac_max_skew_seconds" in m for m in production_numeric_violations(ok))


def test_production_config_violations_surfaces_a_negative_retention():
    """The retention data-loss value (R4-F2) is refused by the whole production kill switch, not just
    the numeric helper — deleting the registry wiring from production_config_violations is visible."""
    prod = _hardened().model_copy(update={"retention_days": -1})
    assert any("retention_days" in m for m in production_config_violations(prod))


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
