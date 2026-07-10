"""Shared RDAP machinery. Each RIR gets a thin strategy isolating its quirks
(endpoints, auth, vcard oddities, data gaps) — the catalog defines ONE
rir_rdap adapter (AUDIT:A3); the strategies are implementation detail.
"""

import httpx

from kyc_tool.adapters.base import hash_inputs


def parse_vcard(vcard_array: list | None) -> dict:
    """RDAP jCard → {name, address}. Tolerant: missing pieces come back ''. """
    name = ""
    address = ""
    if not vcard_array or len(vcard_array) < 2:
        return {"name": name, "address": address}
    for entry in vcard_array[1]:
        if not isinstance(entry, list) or len(entry) < 4:
            continue
        kind, params, _type, value = entry[0], entry[1], entry[2], entry[3]
        if kind == "fn" and not name:
            name = value if isinstance(value, str) else ""
        elif kind == "adr" and not address:
            label = params.get("label") if isinstance(params, dict) else None
            if label:
                address = label.replace("\n", ", ")
            elif isinstance(value, list):
                address = ", ".join(part for part in value if part)
    return {"name": name, "address": address}


def parse_entity(payload: dict) -> dict:
    """Normalize an RDAP entity lookup response."""
    card = parse_vcard(payload.get("vcardArray"))
    pocs = []
    for sub in payload.get("entities", []) or []:
        sub_card = parse_vcard(sub.get("vcardArray"))
        pocs.append(
            {
                "handle": sub.get("handle", ""),
                "roles": sub.get("roles", []),
                "name": sub_card["name"],
            }
        )
    asns = [
        str(a.get("handle", "")) for a in payload.get("autnums", []) or [] if a.get("handle")
    ]
    return {
        "org_handle": payload.get("handle", ""),
        "entity_name": card["name"],
        "address": card["address"],
        "pocs": pocs,
        "asns": asns,
        "status": payload.get("status", []),
        "remarks": [r.get("title", "") for r in payload.get("remarks", []) or []],
    }


class RdapStrategy:
    """Generic direct-entity-lookup strategy. Subclasses set rir/base_url and
    override hooks where the registry deviates."""

    rir = "generic"
    base_url = ""
    entity_path = "/entity/{handle}"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client(base_url=self.base_url, timeout=10.0)

    def input_hash(self, org_handle: str) -> str:
        return hash_inputs("rir_rdap", self.rir, org_handle)

    def lookup_org(self, org_handle: str) -> tuple[bytes, dict]:
        """→ (raw upstream bytes, normalized dict). 404 = handle not found."""
        response = self.client.get(self.entity_path.format(handle=org_handle))
        if response.status_code == 404:
            return response.content, {"found": False, "org_handle": org_handle}
        response.raise_for_status()
        normalized = self.postprocess(parse_entity(response.json()))
        normalized["found"] = True
        return response.content, normalized

    def postprocess(self, normalized: dict) -> dict:
        """Per-RIR quirk hook (default: mark obviously stale/missing data).

        TODO(integration) — KNOWN LIMITATION: four of the five needs_review
        routing rules (parent/subsidiary ambiguity, related-entity-only,
        resources-held-by-provider, via-broad-search) and the gate-5
        `conflicting_entity` flag require per-RIR relationship analysis against
        live RDAP data. They are NOT computed here — only fixtures inject them —
        so those human-review routes are currently unreachable in production.
        We deliberately do not fake the detection: guessing RIR semantics could
        wrongly fail legitimate orgs. Exposure is bounded because org_id_match
        still requires positive name + address matching to PASS. Implementing
        the real detection is scoped to the RIR integration work (validators
        already consume the flags; only the producer is missing)."""
        normalized["address_missing_or_stale"] = not normalized.get("address")
        return normalized
