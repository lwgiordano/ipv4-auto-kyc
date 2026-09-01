"""ARIN — Whois-RWS/RDAP. Quirks: org entities use the 'registrant' role;
API keys raise rate limits (env ARIN_API_KEY, TODO(integration))."""

from kyc_tool.adapters.rir_rdap.base import RdapStrategy


class ArinStrategy(RdapStrategy):
    rir = "arin"
    base_url = "https://rdap.arin.net/registry"
