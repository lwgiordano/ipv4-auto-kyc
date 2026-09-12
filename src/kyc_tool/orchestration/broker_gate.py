"""Broker policy gate — EXACT identifier match only (v1, AUDIT:B3).

Matches the case's identifiers against broker_entities: legal name/aliases,
domains, email domains, RIR ORG-IDs, POC handles, ASNs. Identifiers are
canonicalized (lowercase/trim; names also punctuation-collapsed) but never
fuzzy-matched — near-miss names MUST NOT match (07 §unit). Runs on every
event (AUDIT:D1): org_id/poc submissions introduce matchable identifiers.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from kyc_tool.db.tables import BrokerEntity
from kyc_tool.domain.models import BrokerStatus
from kyc_tool.validators.normalize import canon_id, domain_of, norm


def _canon(value: str | None) -> str:
    return canon_id(value)


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


def entity_identifiers(entity) -> dict[str, set[str]]:
    """Matcher-equivalent classes shared by matches and overlap warnings."""
    return {
        "legal_name": {norm(entity.name), *(norm(a) for a in entity.aliases or [])} - {""},
        "domains": {_canon(d) for d in entity.domains or []} - {""},
        "email_domains": {_canon(d) for d in entity.email_domains or []} - {""},
        "rir_org_ids": {_canon(d) for d in entity.org_ids or []} - {""},
        "poc_handles": {_canon(d) for d in entity.poc_handles or []} - {""},
        "asns": {_canon(d) for d in entity.asns or []} - {""},
    }


def _entity_matches(entity, identifiers: dict[str, set[str]]) -> str | None:
    """Returns the identifier class that matched, or None."""
    classes = entity_identifiers(entity)
    for source, target in (
        ("names", "legal_name"),
        ("domains", "domains"),
        ("email_domains", "email_domains"),
        ("org_ids", "rir_org_ids"),
        ("poc_handles", "poc_handles"),
        ("asns", "asns"),
    ):
        if identifiers[source] & classes[target]:
            return target
    return None


@dataclass(frozen=True)
class BrokerMatch:
    status: BrokerStatus
    entity_id: str | None = None
    identifier_class: str | None = None


def match_brokers(entities, case_snapshot) -> BrokerMatch:
    identifiers = _extract_identifiers(case_snapshot)
    result = BrokerMatch(BrokerStatus.CLEAR)
    for entity in sorted(entities, key=lambda entity: entity.id):
        matched_class = _entity_matches(entity, identifiers)
        if matched_class is None:
            continue
        hit = BrokerMatch(
            BrokerStatus.BLOCKED if entity.policy == "blocked" else BrokerStatus.ALLOWED_BROKER,
            entity.id,
            matched_class,
        )
        if hit.status is BrokerStatus.BLOCKED:
            return hit
        if result.entity_id is None:
            result = hit
    return result


def broker_overlaps(entities) -> list[dict]:
    result = []
    indexed = [(entity, entity_identifiers(entity)) for entity in entities]
    for index, (left, left_ids) in enumerate(indexed):
        for right, right_ids in indexed[index + 1 :]:
            classes = [key for key in left_ids if left_ids[key] & right_ids[key]]
            if classes:
                result.append(
                    {
                        "entity_ids": [left.id, right.id],
                        "identifier_classes": classes,
                        "blocked_precedence": "blocked" in (left.policy, right.policy),
                    }
                )
    return result


class BrokerGate:
    """Callable installed as Pipeline.broker_matcher."""

    def match(self, session: Session, case_snapshot: dict, *, snapshot=None) -> BrokerMatch:
        entities = snapshot if snapshot is not None else session.execute(select(BrokerEntity)).scalars().all()
        return match_brokers(entities, case_snapshot)

    def __call__(self, session: Session, case_snapshot: dict, *, snapshot=None) -> BrokerStatus:
        return self.match(session, case_snapshot, snapshot=snapshot).status
