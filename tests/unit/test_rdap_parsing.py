"""RDAP jCard/entity parsing and strategy behavior against a mock transport."""

import httpx

from kyc_tool.adapters.rir_rdap.arin import ArinStrategy
from kyc_tool.adapters.rir_rdap.base import parse_entity

RDAP_ENTITY = {
    "handle": "ORG-ACME-1",
    "vcardArray": [
        "vcard",
        [
            ["version", {}, "text", "4.0"],
            ["fn", {}, "text", "ACME NETWORKS LTD"],
            [
                "adr",
                {"label": "1 Main Street\nLondon\nEC1A 1AA"},
                "text",
                ["", "", "", "", "", "", ""],
            ],
        ],
    ],
    "entities": [
        {
            "handle": "JD123-ARIN",
            "roles": ["technical"],
            "vcardArray": ["vcard", [["fn", {}, "text", "Jane Doe"]]],
        }
    ],
    "autnums": [{"handle": "AS64500"}],
    "status": ["validated"],
}


def test_parse_entity_extracts_org_fields():
    normalized = parse_entity(RDAP_ENTITY)
    assert normalized["org_handle"] == "ORG-ACME-1"
    assert normalized["entity_name"] == "ACME NETWORKS LTD"
    assert normalized["address"] == "1 Main Street, London, EC1A 1AA"
    assert normalized["pocs"] == [
        {"handle": "JD123-ARIN", "roles": ["technical"], "name": "Jane Doe"}
    ]
    assert normalized["asns"] == ["AS64500"]


def test_strategy_lookup_found_and_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/entity/ORG-ACME-1"):
            return httpx.Response(200, json=RDAP_ENTITY)
        return httpx.Response(404, json={"errorCode": 404})

    strategy = ArinStrategy(
        client=httpx.Client(transport=httpx.MockTransport(handler), base_url="https://rdap.test")
    )
    raw, normalized = strategy.lookup_org("ORG-ACME-1")
    assert normalized["found"] is True
    assert normalized["entity_name"] == "ACME NETWORKS LTD"
    assert normalized["address_missing_or_stale"] is False
    assert b"ORG-ACME-1" in raw

    _, missing = strategy.lookup_org("ORG-NOPE-9")
    assert missing["found"] is False


def test_missing_address_flags_stale():
    entity = {**RDAP_ENTITY, "vcardArray": ["vcard", [["fn", {}, "text", "ACME NETWORKS LTD"]]]}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=entity)

    strategy = ArinStrategy(
        client=httpx.Client(transport=httpx.MockTransport(handler), base_url="https://rdap.test")
    )
    _, normalized = strategy.lookup_org("ORG-ACME-1")
    assert normalized["address_missing_or_stale"] is True
