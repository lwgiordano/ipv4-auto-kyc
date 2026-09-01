"""Broker policy gate — EXACT identifier match only (v1, AUDIT:B3).

Matches the case's identifiers against broker_entities: legal name/aliases,
domains, email domains, RIR ORG-IDs, POC handles, ASNs. Identifiers are
canonicalized (lowercase/trim; names also punctuation-collapsed) but never
fuzzy-matched — near-miss names MUST NOT match (07 §unit). Runs on every
event (AUDIT:D1): org_id/poc submissions introduce matchable identifiers.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from kyc_tool.db.tables import BrokerEntity
from kyc_tool.domain.models import BrokerStatus
from kyc_tool.validators.normalize import domain_of, norm


def _canon(value: str | None) -> str:
    return (value or "").strip().lower()


def _extract_identifiers(case_snapshot: dict) -> dict[str, set[str]]:
    email = case_snapshot.get("email") or {}
    org = case_snapshot.get("org_id") or {}
    poc = case_snapshot.get("poc") or {}
    names = {norm(case_snapshot.get("company_legal_name"))}
    domains = {
        domain_of(case_snapshot.get("website")),
        domain_of(case_snapshot.get("company_domain")),
    }
    email_domains = {domain_of(email.get("domain") or email.get("email"))}
    org_ids = {_canon(org.get("org_handle")), _canon(poc.get("org_handle"))}
    poc_handles = {_canon(poc.get("poc_handle"))}
    asns = {_canon(str(a)) for a in case_snapshot.get("asns", [])}
    return {
        "names": names - {""},
        "domains": domains - {""},
        "email_domains": email_domains - {""},
        "org_ids": org_ids - {""},
        "poc_handles": poc_handles - {""},
        "asns": asns - {""},
    }


def _entity_matches(entity: BrokerEntity, identifiers: dict[str, set[str]]) -> str | None:
    """Returns the identifier class that matched, or None."""
    entity_names = {norm(entity.name), *(norm(a) for a in entity.aliases or [])} - {""}
    if identifiers["names"] & entity_names:
        return "legal_name"
    if identifiers["domains"] & {_canon(d) for d in entity.domains or []}:
        return "domains"
    if identifiers["email_domains"] & {_canon(d) for d in entity.email_domains or []}:
        return "email_domains"
    if identifiers["org_ids"] & {_canon(o) for o in entity.org_ids or []}:
        return "rir_org_ids"
    if identifiers["poc_handles"] & {_canon(p) for p in entity.poc_handles or []}:
        return "poc_handles"
    if identifiers["asns"] & {_canon(a) for a in entity.asns or []}:
        return "asns"
    return None


class BrokerGate:
    """Callable installed as Pipeline.broker_matcher."""

    def __call__(self, session: Session, case_snapshot: dict) -> BrokerStatus:
        identifiers = _extract_identifiers(case_snapshot)
        if not any(identifiers.values()):
            return BrokerStatus.CLEAR
        entities = session.execute(select(BrokerEntity)).scalars().all()
        allowed_hit = False
        for entity in entities:
            matched_class = _entity_matches(entity, identifiers)
            if matched_class is None:
                continue
            if entity.policy == "blocked":
                return BrokerStatus.BLOCKED  # short-circuits everything else
            allowed_hit = True
        return BrokerStatus.ALLOWED_BROKER if allowed_hit else BrokerStatus.CLEAR
