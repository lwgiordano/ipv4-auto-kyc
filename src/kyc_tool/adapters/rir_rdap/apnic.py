"""APNIC — RDAP. Quirk: address labels frequently collapse to one line."""

from kyc_tool.adapters.rir_rdap.base import RdapStrategy


class ApnicStrategy(RdapStrategy):
    rir = "apnic"
    base_url = "https://rdap.apnic.net"
