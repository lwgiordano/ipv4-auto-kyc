"""Floqer enrichment adapter — DISCOVERY ONLY (non-negotiable #8).

Its output seeds registry candidate lookups, LinkedIn matching, and website
review context. It can never by itself satisfy email, ORG-ID, POC, or
registry points — enforced by the intent builder, which awards nothing from
Floqer except linkedin_company_match after a full deterministic match.

TODO(integration) AUDIT:C4: the real Floqer API contract is unknown; the
client protocol freezes the shape we consume.
"""

import json
from typing import Protocol

from kyc_tool.adapters.base import AdapterOutput, hash_inputs
from kyc_tool.domain.models import AdapterStatus


class FloqerClient(Protocol):
    def enrich(self, company_name: str, domain: str) -> dict:
        """→ {company_domain, website, linkedin: {person_name, company, title,
        company_domain}, aliases: [..], registry_candidates: [..],
        broker_context: {...}} — keys optional."""
        ...


class FixtureFloqerClient:
    def __init__(self, records: dict[str, dict]) -> None:
        self.records = records  # key: normalized company name

    def enrich(self, company_name: str, domain: str) -> dict:
        return self.records.get(company_name.strip().lower(), {})


class FloqerAdapter:
    adapter_id = "floqer_company_enrichment"

    def __init__(self, client: FloqerClient) -> None:
        self.client = client

    def input_hash(self, case_snapshot: dict, event: dict) -> str:
        return hash_inputs(
            self.adapter_id,
            case_snapshot.get("company_legal_name"),
            case_snapshot.get("website"),
        )

    def run(self, case_snapshot: dict, event: dict) -> AdapterOutput:
        name = case_snapshot.get("company_legal_name")
        if not name:
            return AdapterOutput(self.adapter_id, AdapterStatus.NOT_APPLICABLE)
        record = self.client.enrich(name, case_snapshot.get("website", ""))
        if not record:
            return AdapterOutput(
                self.adapter_id, AdapterStatus.OK, raw=b"{}", normalized={"discovered": False}
            )
        return AdapterOutput(
            self.adapter_id,
            AdapterStatus.OK,
            raw=json.dumps(record, default=str).encode(),
            normalized={
                "discovered": True,
                "company_domain": record.get("company_domain", ""),
                "website": record.get("website", ""),
                "linkedin": record.get("linkedin") or {},
                "aliases": record.get("aliases", []),
                "registry_candidates": record.get("registry_candidates", []),
                "broker_context": record.get("broker_context", {}),
            },
        )
