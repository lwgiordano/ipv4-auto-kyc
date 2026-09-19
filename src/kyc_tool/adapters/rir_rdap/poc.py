"""Live RIR POC directory over RDAP — the lookup half of the +25 control proof
(03 §5): confirm the POC handle is associated with the SUBMITTED ORG-ID or
resource, and read the RIR-LISTED email the token may be sent to (the sending
itself stays an open decision; the adapter only asks for it).

Association is read from the AUTHORITATIVE side: the org / autnum / ip record is
fetched and must ITSELF list the POC. What the POC record claims about its own
org proves nothing — a contact object is not the registry's statement of who
holds the resource. Every wire call goes through the governed helper
(`adapters.retry`), never `client.get`.
"""

import ipaddress

from kyc_tool.adapters.retry import get_with_retry
from kyc_tool.adapters.rir_rdap.base import parse_entity, vcard_emails
from kyc_tool.validators.normalize import canon_id


def _resource_path(resource: str) -> str | None:
    """RDAP path for a submitted resource: an ASN (`AS123` or `123`) →
    `/autnum/{number}`, an address or CIDR → `/ip/{addr-or-CIDR}` (RDAP takes both).
    Anything else is not a resource and never becomes a path segment: the string is
    platform-supplied, and a `../` in it walks out of the registry's base path."""
    number = resource[2:] if resource[:2].lower() == "as" else resource
    if number.isdigit():
        return f"/autnum/{number}"
    try:
        ipaddress.ip_network(resource, strict=False)
    except ValueError:
        return None
    return f"/ip/{resource}"


def _roles_on(pocs: list[dict], poc_handle: str) -> list[str] | None:
    """The roles `poc_handle` carries on a parsed record, or None when the record
    does not list it at all (None = not associated; [] = listed without a role)."""
    for poc in pocs:
        if canon_id(poc.get("handle")) == canon_id(poc_handle):
            return list(poc.get("roles") or [])
    return None


class RdapPocDirectory:
    """`PocDirectory` over the same per-RIR strategies the rir_rdap adapter uses
    (one client and pool per RIR, shared with it — the registries rate-limit per
    registry). The tool's OWN permit is per adapter id, so these GETs draw the
    `rir_poc` budget, not `rir_rdap`'s."""

    def __init__(self, strategies: dict[str, object]) -> None:
        self.strategies = strategies  # rir -> RdapStrategy-like

    def lookup(
        self,
        rir: str,
        poc_handle: str,
        *,
        org_handle: str | None = None,
        resource: str | None = None,
    ) -> dict:
        miss = {
            "found": False,
            "associated_org_handles": [],
            "resources": [],
            "rir_listed_email": None,
        }
        strategy = self.strategies.get((rir or "").lower())
        if strategy is None:
            return miss  # unknown registry: no record, never an exception

        response = get_with_retry(strategy.client, strategy.entity_path.format(handle=poc_handle))
        if response.status_code == 404:
            return miss
        response.raise_for_status()

        roles: list[str] = []
        orgs: list[str] = []
        if org_handle:
            _, org = strategy.lookup_org(org_handle)  # governed GET of the ORG record
            on_org = _roles_on(org.get("pocs") or [], poc_handle)
            if on_org is not None:
                orgs = [org_handle]
                roles += on_org

        resources: list[str] = []
        resource_path = _resource_path(resource) if resource else None
        if resource_path:
            held = get_with_retry(strategy.client, resource_path)
            if held.status_code != 404:
                held.raise_for_status()
                on_resource = _roles_on(parse_entity(held.json())["pocs"], poc_handle)
                if on_resource is not None:
                    resources = [resource]
                    roles += on_resource

        emails = vcard_emails(response.json().get("vcardArray"))
        return {
            "found": True,
            "associated_org_handles": orgs,
            "resources": resources,
            "rir_listed_email": emails[0] if emails else None,
            "roles": sorted(set(roles)),  # audit raw only; the adapter ignores extra keys
        }
