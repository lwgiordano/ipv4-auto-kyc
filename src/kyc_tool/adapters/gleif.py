"""GLEIF LEI adapter — approval-grade, automated, public API (no key)."""

import httpx

from kyc_tool.adapters.base import AdapterOutput, hash_inputs
from kyc_tool.domain.models import AdapterStatus

BASE_URL = "https://api.gleif.org"


class GleifAdapter:
    adapter_id = "gleif"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client(base_url=BASE_URL, timeout=10.0)

    def input_hash(self, case_snapshot: dict, event: dict) -> str:
        return hash_inputs(self.adapter_id, case_snapshot.get("company_legal_name"))

    def run(self, case_snapshot: dict, event: dict) -> AdapterOutput:
        name = case_snapshot.get("company_legal_name")
        if not name:
            return AdapterOutput(self.adapter_id, AdapterStatus.NOT_APPLICABLE)

        response = self.client.get(
            "/api/v1/lei-records",
            params={"filter[entity.legalName]": name, "page[size]": 10},
        )
        response.raise_for_status()
        raw = response.content
        candidates = []
        for record in response.json().get("data", []):
            entity = (record.get("attributes") or {}).get("entity") or {}
            legal_address = entity.get("legalAddress") or {}
            address = ", ".join(
                part
                for part in (
                    *(legal_address.get("addressLines") or []),
                    legal_address.get("city"),
                    legal_address.get("postalCode"),
                    legal_address.get("country"),
                )
                if part
            )
            candidates.append(
                {
                    "legal_name": (entity.get("legalName") or {}).get("name"),
                    "company_number": (record.get("attributes") or {}).get("lei"),
                    "status": entity.get("status"),
                    "address": address,
                }
            )
        return AdapterOutput(
            self.adapter_id,
            AdapterStatus.OK,
            raw=raw,
            normalized={"candidates": candidates, "query": name},
        )
