"""Companies House adapter (UK registry) — approval-grade, automated.

Fetches search results for the submitted company and normalizes candidates to
{legal_name, company_number, status, address}. Judging happens in the
validator, never here. Tests inject an httpx transport that replays recorded
fixtures; production needs CH_API_KEY (TODO(integration): confirm key + rate
plan with the platform team).
"""

import os

import httpx

from kyc_tool.adapters.base import AdapterOutput, hash_inputs
from kyc_tool.adapters.retry import get_with_retry
from kyc_tool.domain.models import AdapterStatus

BASE_URL = "https://api.company-information.service.gov.uk"


class CompaniesHouseAdapter:
    adapter_id = "companies_house"

    def __init__(self, client: httpx.Client | None = None) -> None:
        api_key = os.environ.get("CH_API_KEY", "")
        self.client = client or httpx.Client(
            base_url=BASE_URL, timeout=10.0, auth=(api_key, "")
        )

    def input_hash(self, case_snapshot: dict, event: dict) -> str:
        return hash_inputs(
            self.adapter_id,
            case_snapshot.get("company_legal_name"),
            case_snapshot.get("registration_number"),
        )

    def run(self, case_snapshot: dict, event: dict) -> AdapterOutput:
        name = case_snapshot.get("company_legal_name")
        if not name or (case_snapshot.get("jurisdiction") or "").upper() not in ("GB", "UK", ""):
            return AdapterOutput(self.adapter_id, AdapterStatus.NOT_APPLICABLE)

        response = get_with_retry(
            self.client, "/search/companies", params={"q": name, "items_per_page": 10}
        )
        response.raise_for_status()
        raw = response.content
        items = response.json().get("items", [])
        candidates = [
            {
                "legal_name": item.get("title"),
                "company_number": item.get("company_number"),
                "status": item.get("company_status"),
                "address": (item.get("address_snippet") or ""),
            }
            for item in items
        ]
        return AdapterOutput(
            self.adapter_id,
            AdapterStatus.OK,
            raw=raw,
            normalized={"candidates": candidates, "query": name},
        )
