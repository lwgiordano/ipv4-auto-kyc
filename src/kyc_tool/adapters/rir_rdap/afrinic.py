"""AFRINIC — RDAP."""

from kyc_tool.adapters.rir_rdap.base import RdapStrategy


class AfrinicStrategy(RdapStrategy):
    rir = "afrinic"
    base_url = "https://rdap.afrinic.net/rdap"
