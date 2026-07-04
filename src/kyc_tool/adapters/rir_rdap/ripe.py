"""RIPE — RDAP over the RIPE DB. Quirk: organisation names live in the vcard
fn of the ORG entity itself; abuse-c contacts appear as sub-entities."""

from kyc_tool.adapters.rir_rdap.base import RdapStrategy


class RipeStrategy(RdapStrategy):
    rir = "ripe"
    base_url = "https://rdap.db.ripe.net"
