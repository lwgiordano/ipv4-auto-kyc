"""LACNIC — RDAP is comparatively sparse: entity addresses are often absent.
The postprocess hook keeps that visible so validation routes to needs_review
(address missing/stale) instead of failing hard."""

from kyc_tool.adapters.rir_rdap.base import RdapStrategy


class LacnicStrategy(RdapStrategy):
    rir = "lacnic"
    base_url = "https://rdap.lacnic.net/rdap"
