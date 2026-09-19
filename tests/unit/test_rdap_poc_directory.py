"""The LIVE POC directory over RDAP (T14): association is read from the
AUTHORITATIVE record (the org / autnum / ip object must itself list the POC),
the RIR-listed email comes off the POC entity's jCard, and every wire call goes
through the governed helper. RFC 9083-shaped fixtures over httpx.MockTransport —
nothing here touches a real registry.
"""

import httpx

from kyc_tool.adapters.rir_poc import RirPocAdapter
from kyc_tool.adapters.rir_rdap import base as base_module
from kyc_tool.adapters.rir_rdap import poc as poc_module
from kyc_tool.adapters.rir_rdap.arin import ArinStrategy
from kyc_tool.adapters.rir_rdap.poc import RdapPocDirectory


def _vcard(*entries) -> list:
    return ["vcard", [["version", {}, "text", "4.0"], *entries]]


RECORDS = {
    "/entity/JD123-ARIN": {
        "handle": "JD123-ARIN",
        "vcardArray": _vcard(
            ["fn", {}, "text", "Jane Doe"], ["email", {}, "text", "noc@acme.example"]
        ),
    },
    # RIPE-style redaction: the jCard simply carries no email entry
    "/entity/RD999-ARIN": {
        "handle": "RD999-ARIN",
        "vcardArray": _vcard(["fn", {}, "text", "REDACTED FOR PRIVACY"]),
    },
    "/entity/ORG-ACME-1": {
        "handle": "ORG-ACME-1",
        "vcardArray": _vcard(["fn", {}, "text", "ACME NETWORKS LTD"]),
        "entities": [
            {
                "handle": "JD123-ARIN",
                "roles": ["technical", "abuse"],
                "vcardArray": _vcard(["fn", {}, "text", "Jane Doe"]),
            }
        ],
    },
    "/entity/ORG-HIDE-1": {
        "handle": "ORG-HIDE-1",
        "entities": [{"handle": "RD999-ARIN", "roles": ["administrative"]}],
    },
    "/entity/ORG-OTHER-9": {
        "handle": "ORG-OTHER-9",
        "entities": [{"handle": "ZZ999-ARIN", "roles": ["technical"]}],
    },
    "/ip/192.0.2.0/24": {
        "handle": "NET-192-0-2-0-1",
        "startAddress": "192.0.2.0",
        "entities": [{"handle": "JD123-ARIN", "roles": ["registrant"]}],
    },
    # a POC whose OWN record claims the org: the claim is ignored (the org record decides)
    "/entity/SELF-ARIN": {
        "handle": "SELF-ARIN",
        "vcardArray": _vcard(["email", {}, "text", "self@acme.example"]),
        "entities": [{"handle": "ORG-ACME-1", "roles": ["registrant"]}],
    },
    "/autnum/64500": {
        "handle": "AS64500",
        "entities": [{"handle": "JD123-ARIN", "roles": ["technical"]}],
    },
}


def _directory() -> tuple[RdapPocDirectory, list[str]]:
    """The directory over ONE mocked RIR, plus the log of paths the transport saw."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        payload = RECORDS.get(request.url.path)
        if payload is None:
            return httpx.Response(404, json={"errorCode": 404, "title": "Not found"})
        return httpx.Response(200, json=payload)

    strategy = ArinStrategy(
        client=httpx.Client(transport=httpx.MockTransport(handler), base_url="https://rdap.test")
    )
    return RdapPocDirectory({"arin": strategy}), seen


def test_org_association_is_read_off_the_org_record():
    directory, seen = _directory()
    record = directory.lookup("arin", "JD123-ARIN", org_handle="ORG-ACME-1")
    assert record["found"] is True
    assert record["associated_org_handles"] == ["ORG-ACME-1"]
    assert record["rir_listed_email"] == "noc@acme.example"
    assert record["roles"] == ["abuse", "technical"]  # audit raw
    assert seen == ["/entity/JD123-ARIN", "/entity/ORG-ACME-1"]


def test_org_that_does_not_list_the_poc_is_not_associated():
    directory, _ = _directory()
    record = directory.lookup("arin", "JD123-ARIN", org_handle="ORG-OTHER-9")
    assert record["found"] is True  # the POC exists...
    assert record["associated_org_handles"] == []  # ...the org just never names it
    assert record["roles"] == []


def test_missing_org_record_leaves_the_poc_unassociated():
    directory, seen = _directory()
    record = directory.lookup("arin", "JD123-ARIN", org_handle="ORG-NOPE-9")
    assert record["found"] is True
    assert record["associated_org_handles"] == []
    assert seen == ["/entity/JD123-ARIN", "/entity/ORG-NOPE-9"]


def test_cidr_resource_association_uses_the_ip_record():
    directory, seen = _directory()
    record = directory.lookup("arin", "JD123-ARIN", resource="192.0.2.0/24")
    assert record["resources"] == ["192.0.2.0/24"]
    assert record["roles"] == ["registrant"]
    assert seen == ["/entity/JD123-ARIN", "/ip/192.0.2.0/24"]


def test_asn_resource_uses_the_autnum_record_in_either_spelling():
    for submitted in ("AS64500", "64500"):
        directory, seen = _directory()
        record = directory.lookup("arin", "JD123-ARIN", resource=submitted)
        assert record["resources"] == [submitted]
        assert seen == ["/entity/JD123-ARIN", "/autnum/64500"]


def test_resource_that_does_not_list_the_poc_is_not_associated():
    directory, _ = _directory()
    record = directory.lookup("arin", "RD999-ARIN", resource="192.0.2.0/24")
    assert record["resources"] == []


def test_missing_resource_record_is_not_an_association():
    directory, seen = _directory()
    record = directory.lookup("arin", "JD123-ARIN", resource="198.51.100.0/24")
    assert record["resources"] == []
    assert seen == ["/entity/JD123-ARIN", "/ip/198.51.100.0/24"]


def test_unknown_poc_handle_is_not_found_and_stops_there():
    directory, seen = _directory()
    record = directory.lookup("arin", "ZZ000-ARIN", org_handle="ORG-ACME-1")
    assert record == {
        "found": False,
        "associated_org_handles": [],
        "resources": [],
        "rir_listed_email": None,
    }
    assert seen == ["/entity/ZZ000-ARIN"]  # no org lookup once the POC is gone


def test_redacted_jcard_yields_no_email():
    directory, _ = _directory()
    record = directory.lookup("arin", "RD999-ARIN", org_handle="ORG-HIDE-1")
    assert record["associated_org_handles"] == ["ORG-HIDE-1"]
    assert record["rir_listed_email"] is None


def test_unknown_rir_is_a_miss_not_an_exception():
    directory, seen = _directory()
    record = directory.lookup("ripe", "JD123-RIPE", org_handle="ORG-ACME-1")
    assert record["found"] is False
    assert seen == []  # nothing left the box


def test_every_wire_call_goes_through_the_governed_helper(monkeypatch):
    """No `client.get` anywhere: the plan budget, liveness proof, deadline and
    byte containment all hang off `adapters.retry` (re-audit F3)."""
    governed: list[str] = []
    for module in (poc_module, base_module):
        real = module.get_with_retry

        def helper(client, url, *, _real=real, **kwargs):
            governed.append(url)
            return _real(client, url, **kwargs)

        monkeypatch.setattr(module, "get_with_retry", helper)

    directory, seen = _directory()
    strategy = directory.strategies["arin"]
    directory.lookup("arin", "JD123-ARIN", org_handle="ORG-ACME-1", resource="192.0.2.0/24")
    assert governed == seen == [
        strategy.entity_path.format(handle="JD123-ARIN"),
        strategy.entity_path.format(handle="ORG-ACME-1"),
        "/ip/192.0.2.0/24",
    ]


def test_adapter_sends_the_token_on_the_associated_case_with_an_email():
    directory, _ = _directory()
    out = (
        RirPocAdapter(directory)
        .run({"poc": {"rir": "arin", "poc_handle": "JD123-ARIN", "org_handle": "ORG-ACME-1"}}, {})
        .normalized
    )
    assert out["associated"] is True
    assert out["rir_listed_email"] == "noc@acme.example"
    assert out["send_token"] is True
    assert out["email_unavailable"] is False


def test_adapter_reports_email_unavailable_on_the_redacted_case():
    directory, _ = _directory()
    out = (
        RirPocAdapter(directory)
        .run({"poc": {"rir": "arin", "poc_handle": "RD999-ARIN", "org_handle": "ORG-HIDE-1"}}, {})
        .normalized
    )
    assert out["associated"] is True
    assert out["send_token"] is False
    assert out["email_unavailable"] is True


def test_the_pocs_own_claims_never_prove_the_association():
    """A POC record may carry its own `entities`, and a maintainer controls them —
    they are never the registry's statement of who holds what. Only the org record
    decides, and here it names a DIFFERENT POC."""
    directory, seen = _directory()
    record = directory.lookup("arin", "SELF-ARIN", org_handle="ORG-ACME-1")
    assert record["found"] is True
    assert record["associated_org_handles"] == []  # ORG-ACME-1 lists JD123-ARIN, not SELF-ARIN
    assert seen == ["/entity/SELF-ARIN", "/entity/ORG-ACME-1"]


def test_a_resource_that_is_not_a_resource_never_becomes_a_path():
    directory, seen = _directory()
    for bad in ("AS64500/foo", "../entity/ORG-ACME-1", "//evil.example/x", "192.0.2.0/24?x=1", "as"):
        record = directory.lookup("arin", "JD123-ARIN", resource=bad)
        assert record["found"] is True and record["resources"] == []
    assert all(path.startswith("/entity/JD123-ARIN") for path in seen)

