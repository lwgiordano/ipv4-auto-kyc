"""Closed editable inputs must not silently expand policy authority."""

import importlib
import json
from dataclasses import FrozenInstanceError

import pytest

from kyc_tool.policy.loader import read_policy_files
from kyc_tool.ui.salesforce_projection import FIELD_SOURCES


def models():
    assert importlib.util.find_spec("kyc_tool.configuration") is not None, (
        "configuration validation is missing"
    )
    return importlib.import_module("kyc_tool.configuration.models")


def points(policy):
    return {item.check_type: item.points for item in policy.rubric.items}


def broker(**changes):
    return {
        "id": "stable-1",
        "name": "Example Ltd",
        "policy": "allowed",
        "aliases": [],
        "domains": [],
        "email_domains": [],
        "org_ids": [],
        "poc_handles": [],
        "asns": [],
        "notes": "operator note",
        **changes,
    }


@pytest.mark.parametrize("bad", [True, 1.5, "25", -1, 1001])
def test_points_refuse_non_domain_values(policy, bad):
    m = models()
    value = points(policy)
    value[policy.rubric.items[0].check_type] = bad
    with pytest.raises(m.ConfigurationInvalid):
        m.validate_points(value, policy.rubric)


@pytest.mark.parametrize("extra", [True, False])
def test_points_require_exact_keys(policy, extra):
    m = models()
    value = points(policy)
    if extra:
        value["threshold"] = 100
    else:
        value.pop(next(iter(value)))
    with pytest.raises(m.ConfigurationInvalid):
        m.validate_points(value, policy.rubric)


def test_zero_and_max_points_are_legal(policy):
    m = models()
    value = points(policy)
    value["org_id_match"], value["poc_verified"] = 0, 1000
    assert m.validate_points(value, policy.rubric) == value
    raw = read_policy_files(policy.policy_dir)
    rubric = json.loads(raw["scoring_rubric.json"])
    rubric["unknown_extension"] = {"preserve": [1, "two"]}
    raw["scoring_rubric.json"] = json.dumps(rubric).encode()
    derived = m.derive_points(raw, value)
    got = json.loads(derived["scoring_rubric.json"])
    assert got["unknown_extension"] == {"preserve": [1, "two"]}
    assert got["threshold"] == rubric["threshold"]
    assert got["hard_gates"] == rubric["hard_gates"]
    assert {i["check_type"]: i.get("cap") for i in got["items"]}["org_id_match"] == 0
    assert {i["check_type"]: i.get("cap") for i in got["items"]}["poc_verified"] == 1000
    assert "ORG-ID contributes at most 0; POC at most 1000." in got["dynamic_rules"]
    assert got["version"] != rubric["version"]
    for name in raw.keys() - {"scoring_rubric.json"}:
        assert derived[name] == raw[name]


@pytest.mark.parametrize("field,bad", [("threshold", 99), ("hard_gates", {}), ("items", [])])
def test_bad_policy_is_refused(policy, field, bad):
    m = models()
    raw = read_policy_files(policy.policy_dir)
    rubric = json.loads(raw["scoring_rubric.json"])
    rubric[field] = bad
    raw["scoring_rubric.json"] = json.dumps(rubric).encode()
    with pytest.raises(m.ConfigurationInvalid):
        m.derive_points(raw, points(policy))


def test_candidate_cannot_change_hidden_evidence_rules(policy):
    m = models()
    raw = read_policy_files(policy.policy_dir)
    candidate = m.derive_points(raw, points(policy) | {"org_id_match": 0})
    rubric = json.loads(candidate["scoring_rubric.json"])
    rubric["items"][0]["pass_rule"] = "always pass"
    candidate["scoring_rubric.json"] = json.dumps(rubric).encode()
    with pytest.raises(m.ConfigurationInvalid):
        m.validate_candidate(candidate, raw)


@pytest.mark.parametrize("bad", ["", "x" * 81, "é", "a.b", "_private", True, 25])
def test_mapping_name_contract(bad):
    m = models()
    value = {key: key for key in FIELD_SOURCES}
    value[next(iter(value))] = bad
    with pytest.raises(m.ConfigurationInvalid):
        m.validate_mappings(value)


def test_mappings_are_case_insensitively_unique_and_closed():
    m = models()
    value = {key: key for key in FIELD_SOURCES}
    keys = list(value)
    value[keys[1]] = value[keys[0]].lower()
    with pytest.raises(m.ConfigurationInvalid):
        m.validate_mappings(value)
    with pytest.raises(m.ConfigurationInvalid):
        m.validate_mappings({"unexpected": "Field__c"})


@pytest.mark.parametrize(
    "changes",
    [
        {"policy": "anything"},
        {"name": ""},
        {"name": "x" * 201},
        {"notes": "x" * 2001},
        {"domains": ["x"] * 101},
        {"domains": ["x" * 257]},
        {"domains": [True]},
        {"domains": ["EXAMPLE.COM", " example.com "]},
        {"aliases": ["A-B", "a b"]},
        {"extra": "forged"},
        {"id": ""},
        {"asns": "123"},
    ],
)
def test_broker_contract(changes):
    m = models()
    with pytest.raises(m.ConfigurationInvalid):
        m.validate_brokers([broker(**changes)])


def test_broker_full_list_limits_and_duplicates():
    m = models()
    for value in (
        [broker(), broker(name="Other")],
        [broker(), broker(id="other", name="Example-Ltd")],
        [broker(id=str(i), name=f"Broker {i}") for i in range(201)],
    ):
        with pytest.raises(m.ConfigurationInvalid):
            m.validate_brokers(value)


def test_broker_snapshot_is_owned_frozen_and_preserves_notes():
    m = models()
    value = [broker(domains=[" EXAMPLE.COM "])]
    got = m.validate_brokers(value)
    value[0]["domains"].append("later.com")
    assert got[0].id == "stable-1"
    assert got[0].notes == "operator note"
    assert got[0].domains == ("example.com",)
    with pytest.raises(FrozenInstanceError):
        got[0].policy = "blocked"


def test_empty_broker_list_is_a_real_snapshot():
    assert models().validate_brokers([]) == ()


def test_opaque_broker_id_is_preserved_exactly():
    m = models()
    assert m.validate_brokers([broker(id=" stable-id ")])[0].id == " stable-id "
    with pytest.raises(m.ConfigurationInvalid):
        m.validate_brokers([broker(id=" stable-id "), broker(id=" stable-id ", name="Other Ltd")])
    with pytest.raises(m.ConfigurationInvalid):
        m.validate_brokers([broker(id="   ")])
