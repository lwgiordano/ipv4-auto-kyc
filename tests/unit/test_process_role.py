"""Re-audit `03dbfab..bc325e7` R5-F1: the process-role authority. A DEV-ONLY role (fixture adapters)
must never run in a production environment — even with an otherwise-hardened config — and it must
refuse BEFORE any database/network/store creation. Every production entry point routes through the
same authority."""

import pytest

from kyc_tool import config
from kyc_tool.config import ProcessRole, ProductionConfigError, validate_process_role


def _hardened(**overrides):
    base = dict(
        environment="production", auth_disabled=False, platform_hmac_secret="s" * 40,
        platform_callback_url="https://platform.example/kyc", object_store="s3",
        s3_bucket="kyc-evidence", ocr_engine="tesseract", email_provider="ses",
        adapters_profile="real", read_auth_required=True, ui_enabled=False,
        hmac_inbound_key_id="k-in", hmac_inbound_secret="i" * 40,
        hmac_outbound_key_id="k-out", hmac_outbound_secret="o" * 40,
        hmac_v1_inbound_sunset_at="2030-01-01T00:00:00+00:00",
        hmac_v1_outbound_sunset_at="2030-01-01T00:00:00+00:00",
        hmac_v1_observation_window_days=14,
    )
    base.update(overrides)
    return config.Settings(**base)


def test_dev_worker_is_refused_in_production_even_when_fully_hardened():
    with pytest.raises(ProductionConfigError, match="DEV-ONLY"):
        validate_process_role(_hardened(), ProcessRole.DEV_WORKER)


def test_dev_worker_is_allowed_outside_production():
    validate_process_role(config.Settings(environment="development"), ProcessRole.DEV_WORKER)  # no raise


@pytest.mark.parametrize(
    "role",
    [ProcessRole.API, ProcessRole.PIPELINE_WORKER, ProcessRole.OUTBOX_WORKER, ProcessRole.RETENTION],
)
def test_production_roles_run_the_kill_switch(role):
    validate_process_role(_hardened(), role)  # hardened config passes
    with pytest.raises(ProductionConfigError):  # an unsafe config is refused
        validate_process_role(_hardened(auth_disabled=True), role)


def test_dev_worker_main_refuses_before_touching_the_database(monkeypatch):
    """The engine/store are never constructed for a production dev_worker — the guard fires first."""
    from kyc_tool.workers import dev_worker

    monkeypatch.setattr(dev_worker, "get_settings", lambda: _hardened())
    monkeypatch.setattr(
        dev_worker, "make_engine",
        lambda *a, **k: pytest.fail("dev_worker built the production engine"),
    )
    with pytest.raises(ProductionConfigError, match="DEV-ONLY"):
        dev_worker.main()


@pytest.mark.parametrize(
    "module,role",
    [
        ("kyc_tool.workers.pipeline_worker", "build_worker"),
        ("kyc_tool.workers.outbox_worker", "build_publisher"),
    ],
)
def test_production_workers_validate_before_engine(monkeypatch, module, role):
    import importlib

    mod = importlib.import_module(module)
    monkeypatch.setattr(mod, "get_settings", lambda: _hardened(auth_disabled=True))
    monkeypatch.setattr(
        mod, "make_engine", lambda *a, **k: pytest.fail("engine built on an unsafe production config")
    )
    with pytest.raises(ProductionConfigError):
        getattr(mod, role)()
