"""The dev stack has ONE switch (like the Floqer shortcut id): with CH_API_KEY set,
`build_dev_adapters` wires the real Companies House, GLEIF, all five RIR RDAP strategies and
the POC directory over them — without it, the fixtures the app has always run on. Nothing else
changes in either mode. The production registry (`build_adapters`) still refuses every
non-fixture profile, so its POC directory is the fixture one."""

import pytest

from kyc_tool import config
from kyc_tool.adapters import companies_house, gleif
from kyc_tool.adapters.rir_rdap.adapter import FixtureRirStrategy
from kyc_tool.adapters.rir_rdap.afrinic import AfrinicStrategy
from kyc_tool.adapters.rir_rdap.apnic import ApnicStrategy
from kyc_tool.adapters.rir_rdap.arin import ArinStrategy
from kyc_tool.adapters.rir_rdap.lacnic import LacnicStrategy
from kyc_tool.adapters.rir_rdap.poc import RdapPocDirectory
from kyc_tool.adapters.rir_rdap.ripe import RipeStrategy
from kyc_tool.workers.dev_worker import build_dev_adapters
from kyc_tool.workers.pipeline_worker import build_adapters


@pytest.fixture
def adapters(monkeypatch, request):
    if request.param is None:
        monkeypatch.delenv("CH_API_KEY", raising=False)
    else:
        monkeypatch.setenv("CH_API_KEY", request.param)
    # store is only handed to the OCR adapter, which never touches it at build time.
    return build_dev_adapters(None, config.Settings(environment="development", floqer_shortcut_id=""))


@pytest.mark.parametrize("adapters", [None], indirect=True)
def test_no_key_keeps_the_fixtures(adapters):
    assert isinstance(adapters["rir_rdap"].strategies["arin"], FixtureRirStrategy)
    assert list(adapters["rir_rdap"].strategies) == ["arin"]
    assert type(adapters["rir_poc"].directory).__name__ == "FixturePocDirectory"
    # dev.local base urls are the httpx.MockTransport clients — no request can leave the box.
    assert str(adapters["companies_house"].client.base_url) == "https://ch.dev.local"
    assert str(adapters["gleif"].client.base_url) == "https://gleif.dev.local"


@pytest.mark.parametrize("adapters", ["dev-key"], indirect=True)
def test_the_key_switches_registries_and_all_five_rirs_live(adapters):
    assert {rir: type(s) for rir, s in adapters["rir_rdap"].strategies.items()} == {
        "arin": ArinStrategy,
        "ripe": RipeStrategy,
        "apnic": ApnicStrategy,
        "lacnic": LacnicStrategy,
        "afrinic": AfrinicStrategy,
    }
    # Default clients: the adapters' own real base urls (and CH's key auth).
    assert str(adapters["companies_house"].client.base_url) == companies_house.BASE_URL
    assert str(adapters["gleif"].client.base_url) == gleif.BASE_URL
    # The POC directory rides the SAME strategy objects — one client per registry.
    directory = adapters["rir_poc"].directory
    assert isinstance(directory, RdapPocDirectory)
    assert directory.strategies is adapters["rir_rdap"].strategies


@pytest.mark.parametrize("adapters", [None, "dev-key"], indirect=True)
def test_what_has_no_live_implementation_stays_fixture_in_both_modes(adapters):
    assert type(adapters["document_ocr"].engine).__name__ == "JsonScanOcrEngine"
    assert type(adapters["floqer_company_enrichment"].client).__name__ == "FixtureFloqerClient"


def test_the_production_registry_still_refuses_every_non_fixture_profile():
    """T14 stopped at the governed production blocker (OPS.BLOCKER.PRODUCTION_PROVIDERS, proven
    by executing this call): the live directory exists, but `build_adapters` may not offer it
    until that reviewed claim is re-pinned by a human."""
    # store is only handed to the OCR adapter, which never touches it at build time.
    adapters = build_adapters(config.Settings(environment="development"), None)
    assert type(adapters["rir_poc"].directory).__name__ == "FixturePocDirectory"
    with pytest.raises(NotImplementedError):
        build_adapters(config.Settings(environment="development", adapters_profile="real"), None)
