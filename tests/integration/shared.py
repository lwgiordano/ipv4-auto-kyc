"""Shared constants and builders for the integration suites."""

import json
from pathlib import Path

import httpx

RECORDED = Path(__file__).parent.parent / "fixtures" / "recorded"

ACME_KYB = {
    "company_legal_name": "Acme Networks Ltd",
    "address": "1 Main Street, London, EC1A 1AA",
    "registration_number": "12345678",
    "jurisdiction": "GB",
    "website": "https://acme.example",
}

ACME_KYB_WITH_CONTACT = {**ACME_KYB, "contact": {"name": "Jane Doe", "title": "Director"}}

POC_DIRECTORY = {
    "arin:JD123-ARIN": {
        "found": True,
        "associated_org_handles": ["ORG-ACME-1"],
        "rir_listed_email": "noc@acme.example",
    },
    "ripe:HIDDEN-RIPE": {
        "found": True,
        "associated_org_handles": ["ORG-HIDE-1"],
        "rir_listed_email": None,
    },
}

RDAP_RECORDS = {
    "ORG-ACME-1": {
        "org_handle": "ORG-ACME-1",
        "entity_name": "ACME NETWORKS LTD",
        "address": "1 Main Street, London, EC1A 1AA",
    },
    "ORG-AMBIG-1": {
        "org_handle": "ORG-AMBIG-1",
        "entity_name": "ACME NETWORKS LTD",
        "address": "1 Main Street, London, EC1A 1AA",
        "parent_subsidiary_ambiguity": True,
    },
    "ORG-CONFLICT-1": {
        "org_handle": "ORG-CONFLICT-1",
        "entity_name": "ACME NETWORKS LTD",
        "address": "1 Main Street, London, EC1A 1AA",
        "conflicting_entity": True,
    },
}

FLOQER_RECORDS = {
    "acme networks ltd": {
        "company_domain": "acme.example",
        "website": "https://acme.example",
        "linkedin": {
            "person_name": "Jane Doe",
            "company": "Acme Networks Ltd",
            "title": "Director",
            "company_domain": "acme.example",
        },
        "aliases": ["Acme Networks"],
        "registry_candidates": [{"registry": "companies_house", "number": "12345678"}],
    }
}


def registry_transport(request: httpx.Request) -> httpx.Response:
    if "/search/companies" in request.url.path:
        return httpx.Response(
            200, json=json.loads((RECORDED / "companies_house_acme.json").read_text())
        )
    if "lei-records" in request.url.path:
        return httpx.Response(200, json={"data": []})
    return httpx.Response(404)
