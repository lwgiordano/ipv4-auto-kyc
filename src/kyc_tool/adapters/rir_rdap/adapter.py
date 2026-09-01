"""rir_rdap adapter — ONE adapter (per the catalog, AUDIT:A3) dispatching to
per-RIR strategies. Direct handle lookups only in v1: no broad name search
(the needs_review route for broad-search results exists in the validator for
strategies that ever set the flag)."""

from kyc_tool.adapters.base import AdapterOutput, hash_inputs
from kyc_tool.domain.models import AdapterStatus


class RirRdapAdapter:
    adapter_id = "rir_rdap"

    def __init__(self, strategies: dict[str, object]) -> None:
        self.strategies = strategies  # rir -> RdapStrategy-like

    def _org(self, case_snapshot: dict) -> dict:
        return case_snapshot.get("org_id") or {}

    def input_hash(self, case_snapshot: dict, event: dict) -> str:
        org = self._org(case_snapshot)
        return hash_inputs(self.adapter_id, org.get("rir"), org.get("org_handle"))

    def run(self, case_snapshot: dict, event: dict) -> AdapterOutput:
        org = self._org(case_snapshot)
        handle = org.get("org_handle")
        rir = (org.get("rir") or "").lower()
        if not handle or rir not in self.strategies:
            return AdapterOutput(self.adapter_id, AdapterStatus.NOT_APPLICABLE)

        raw, normalized = self.strategies[rir].lookup_org(handle)
        normalized["rir"] = rir
        return AdapterOutput(self.adapter_id, AdapterStatus.OK, raw=raw, normalized=normalized)


class FixtureRirStrategy:
    """Test/dev strategy: records keyed by handle; raw is the record itself."""

    def __init__(self, records: dict[str, dict]) -> None:
        self.records = records

    def lookup_org(self, org_handle: str) -> tuple[bytes, dict]:
        import json

        record = self.records.get(org_handle)
        if record is None:
            return b"{}", {"found": False, "org_handle": org_handle}
        normalized = {"found": True, "address_missing_or_stale": False, **record}
        return json.dumps(record).encode(), normalized
