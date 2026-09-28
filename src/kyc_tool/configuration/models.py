"""Strict, closed editable inputs and byte-preserving policy derivation."""

import copy
import hashlib
import json
import re
from dataclasses import asdict, dataclass

from kyc_tool.policy.loader import POLICY_FILES, PolicyBundle, build_bundle
from kyc_tool.policy.types import ScoringRubric
from kyc_tool.ui.salesforce_projection import FIELD_SOURCES
from kyc_tool.validators.normalize import canon_id, norm

MAX_REQUEST_BYTES = 32 * 1024 * 1024
IDENTIFIER_FIELDS = ("aliases", "domains", "email_domains", "org_ids", "poc_handles", "asns")
CHECK_TYPES = frozenset(
    (
        "verified_company_email",
        "official_registry_match",
        "org_id_match",
        "poc_verified",
        "business_document_verified",
        "linkedin_company_match",
        "website_verified",
        "verified_email",
    )
)


class ConfigurationInvalid(Exception):
    """Invalid editable input; never include credentials or request bodies."""


class ConfigurationUnavailable(Exception):
    """Missing/corrupt authority or bounded database-operation failure."""


class ConfigurationConflict(Exception):
    """The displayed revision is stale."""


class ConfigurationRequestConflict(Exception):
    """A request ID was reused for a different canonical payload."""


@dataclass(frozen=True)
class BrokerRecord:
    id: str
    name: str
    policy: str
    aliases: tuple[str, ...]
    domains: tuple[str, ...]
    email_domains: tuple[str, ...]
    org_ids: tuple[str, ...]
    poc_handles: tuple[str, ...]
    asns: tuple[str, ...]
    notes: str | None


@dataclass(frozen=True)
class ResolvedConfiguration:
    revision: int
    bundle: PolicyBundle
    brokers: tuple[BrokerRecord, ...]
    mappings: dict[str, str]  # fresh owned copy on every resolution; never cached


def canonical_json(value) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode()) > MAX_REQUEST_BYTES:
            raise ConfigurationInvalid("configuration request exceeds 32 MiB")
        return encoded
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ConfigurationInvalid("configuration must be bounded JSON") from exc


def validate_points(value, rubric: ScoringRubric) -> dict[str, int]:
    if type(value) is not dict or set(value) != set(rubric.check_types):
        raise ConfigurationInvalid("points must contain exactly the existing check types")
    if any(type(v) is not int or not 0 <= v <= 1000 for v in value.values()):
        raise ConfigurationInvalid("points must be exact integers from 0 to 1000")
    return dict(sorted(value.items()))


def validate_mappings(value) -> dict[str, str]:
    if type(value) is not dict or set(value) != set(FIELD_SOURCES):
        raise ConfigurationInvalid("mappings must contain exactly the existing sources")
    if any(
        type(v) is not str or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", v) is None for v in value.values()
    ):
        raise ConfigurationInvalid("destination names must be 1-80 ASCII identifier characters")
    if len({v.lower() for v in value.values()}) != len(value):
        raise ConfigurationInvalid("destination names must be unique ignoring case")
    return dict(sorted(value.items()))


def _string(value, maximum, label):
    if type(value) is not str or not value.strip() or len(value) > maximum:
        raise ConfigurationInvalid(f"invalid {label}")
    return value.strip()


def validate_brokers(value) -> tuple[BrokerRecord, ...]:
    if type(value) is not list or len(value) > 200:
        raise ConfigurationInvalid("brokers must be a complete list of at most 200 entries")
    records, ids, names = [], set(), set()
    expected = {"id", "name", "policy", "notes", *IDENTIFIER_FIELDS}
    for row in value:
        if type(row) is not dict or set(row) != expected:
            raise ConfigurationInvalid("broker fields must match the closed snapshot schema")
        _string(row["id"], 256, "broker ID")
        # Entity IDs are opaque legacy primary keys, not matcher identifiers.
        entity_id = row["id"]
        name = _string(row["name"], 200, "broker name")
        if entity_id in ids or not norm(name) or norm(name) in names:
            raise ConfigurationInvalid("duplicate broker ID or normalized name")
        if type(row["policy"]) is not str or row["policy"] not in ("allowed", "blocked"):
            raise ConfigurationInvalid("broker policy must be allowed or blocked")
        notes = row["notes"]
        if notes is not None and (type(notes) is not str or len(notes) > 2000):
            raise ConfigurationInvalid("broker notes must be at most 2000 characters")
        identifiers = {}
        for field in IDENTIFIER_FIELDS:
            items = row[field]
            if type(items) is not list or len(items) > 100:
                raise ConfigurationInvalid(f"{field} must contain at most 100 identifiers")
            normalize = norm if field == "aliases" else canon_id
            clean = [normalize(_string(item, 256, field)) for item in items]
            if any(not item for item in clean) or len(set(clean)) != len(clean):
                raise ConfigurationInvalid(f"empty or duplicate {field} identifier")
            identifiers[field] = tuple(sorted(clean))
        records.append(BrokerRecord(entity_id, name, row["policy"], notes=notes, **identifiers))
        ids.add(entity_id)
        names.add(norm(name))
    return tuple(sorted(records, key=lambda row: row.id))


def brokers_json(records) -> list[dict]:
    return json.loads(canonical_json([asdict(record) for record in records]))


def validate_policy(raw: dict[str, bytes]) -> PolicyBundle:
    try:
        if set(raw) != set(POLICY_FILES):
            raise ValueError("wrong policy files")
        bundle = build_bundle(raw)
        rubric = json.loads(raw["scoring_rubric.json"])
        gates = {
            "score_met": "score >= 100",
            "legal_business_proof_required": True,
            "control_proof_required": True,
            "broker_status_allowed": ["clear", "allowed_broker"],
            "hard_conflict_must_be_false": True,
        }
        if (
            type(rubric["threshold"]) is not int
            or rubric["threshold"] != 100
            or canonical_json(rubric["hard_gates"]) != canonical_json(gates)
            or set(bundle.rubric.check_types) != CHECK_TYPES
            or len(bundle.rubric.items) != len(CHECK_TYPES)
        ):
            raise ValueError("invalid fixed rubric")
        values = {item["check_type"]: item["points"] for item in rubric["items"]}
        validate_points(values, bundle.rubric)
        for item in rubric["items"]:
            if item["check_type"] in ("org_id_match", "poc_verified") and (
                type(item.get("cap")) is not int or item["cap"] != item["points"]
            ):
                raise ValueError("inconsistent cap")
        expected = (
            f"ORG-ID contributes at most {values['org_id_match']}; POC at most {values['poc_verified']}."
        )
        if rubric["dynamic_rules"].count(expected) != 1:
            raise ValueError("inconsistent cap description")
        return bundle
    except ConfigurationInvalid:
        raise
    except Exception as exc:
        raise ConfigurationInvalid("policy does not satisfy configuration invariants") from exc


def _derive(raw, value):
    previous = validate_policy(raw)
    value = validate_points(value, previous.rubric)
    result = dict(raw)
    rubric = json.loads(raw["scoring_rubric.json"])
    if value == {i.check_type: i.points for i in previous.rubric.items}:
        return result
    old = {i.check_type: i.points for i in previous.rubric.items}
    for item in rubric["items"]:
        item["points"] = value[item["check_type"]]
        if item["check_type"] in ("org_id_match", "poc_verified"):
            item["cap"] = item["points"]
    old_text = f"ORG-ID contributes at most {old['org_id_match']}; POC at most {old['poc_verified']}."
    index = rubric["dynamic_rules"].index(old_text)
    rubric["dynamic_rules"][index] = (
        f"ORG-ID contributes at most {value['org_id_match']}; POC at most {value['poc_verified']}."
    )
    # AUDIT:D-LIVE-CONFIG: local weights replace packaged defaults, not evidence rules.
    rubric["version"] = "local-" + hashlib.sha256(canonical_json(value).encode()).hexdigest()
    result["scoring_rubric.json"] = canonical_json(rubric).encode()
    return result


def validate_candidate(candidate, previous) -> PolicyBundle:
    bundle = validate_policy(candidate)
    expected = _derive(previous, {i.check_type: i.points for i in bundle.rubric.items})
    for name in POLICY_FILES:
        if name == "scoring_rubric.json":
            if json.loads(candidate[name]) != json.loads(expected[name]):
                raise ConfigurationInvalid("candidate changes hidden rubric metadata")
        elif candidate[name] != expected[name]:
            raise ConfigurationInvalid("candidate changes a non-editable policy file")
    return bundle


def derive_points(raw, value) -> dict[str, bytes]:
    result = _derive(copy.copy(raw), value)
    validate_candidate(result, raw)
    return result
