"""PR 3 (item 3): the rir_poc adapter marks association only when a submitted
ORG-ID or resource is a VERIFIED target in the directory record — a missing
submitted target is no longer 'associated by default'."""

from kyc_tool.adapters.rir_poc import FixturePocDirectory, RirPocAdapter

DIRECTORY = {
    "arin:JD123-ARIN": {
        "found": True,
        "associated_org_handles": ["ORG-ACME-1"],
        "resources": ["192.0.2.0/24"],
        "rir_listed_email": "noc@acme.example",
    },
}


def _run(poc: dict) -> dict:
    return RirPocAdapter(FixturePocDirectory(DIRECTORY)).run({"poc": poc}, {}).normalized


def test_associated_via_matching_org():
    out = _run({"rir": "arin", "poc_handle": "JD123-ARIN", "org_handle": "ORG-ACME-1"})
    assert out["associated"] is True
    assert out["association_target"]["org_handle"] == "ORG-ACME-1"
    assert out["send_token"] is True  # has a listed email


def test_associated_via_matching_resource():
    out = _run({"rir": "arin", "poc_handle": "JD123-ARIN", "resource": "192.0.2.0/24"})
    assert out["associated"] is True
    assert out["association_target"]["resource"] == "192.0.2.0/24"


def test_no_target_submitted_is_not_associated():
    # the old rule counted a missing submitted org as associated-by-default
    out = _run({"rir": "arin", "poc_handle": "JD123-ARIN"})
    assert out["associated"] is False
    assert out["send_token"] is False


def test_wrong_org_is_not_associated():
    out = _run({"rir": "arin", "poc_handle": "JD123-ARIN", "org_handle": "ORG-OTHER-9"})
    assert out["associated"] is False
