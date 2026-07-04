"""Loads the normative policy JSONs once per process into a frozen bundle.

Provenance: each file's sha256 and a combined bundle hash are stamped onto
every run and decision — a compliance reviewer can pin any decision to the
exact policy bytes that produced it. No hot reload: policy change = deploy.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from kyc_tool.policy.types import (
    AdapterCatalog,
    BrokerPolicy,
    DecisionPolicy,
    PlatformEvents,
    ScoringRubric,
    StateMachineSpec,
)

POLICY_FILES = (
    "scoring_rubric.json",
    "decision_policy.json",
    "broker_policy.json",
    "platform_events.json",
    "adapter_catalog.json",
    "state_machine.json",
    "salesforce_sync_fields.json",
)


@dataclass(frozen=True)
class PolicyBundle:
    rubric: ScoringRubric
    decision_policy: DecisionPolicy
    broker_policy: BrokerPolicy
    events: PlatformEvents
    adapter_catalog: AdapterCatalog
    state_machine: StateMachineSpec
    salesforce_sync_fields: dict
    shas: dict[str, str]
    bundle_hash: str
    policy_dir: Path


def load_policy(policy_dir: Path) -> PolicyBundle:
    raw: dict[str, bytes] = {}
    for filename in POLICY_FILES:
        path = policy_dir / filename
        if not path.is_file():
            raise FileNotFoundError(f"normative policy file missing: {path}")
        raw[filename] = path.read_bytes()

    shas = {name: hashlib.sha256(data).hexdigest() for name, data in raw.items()}
    bundle_hash = hashlib.sha256(
        "".join(f"{name}:{shas[name]};" for name in POLICY_FILES).encode()
    ).hexdigest()

    def parse(name: str) -> dict | list:
        return json.loads(raw[name])

    return PolicyBundle(
        rubric=ScoringRubric.model_validate(parse("scoring_rubric.json")),
        decision_policy=DecisionPolicy.model_validate(parse("decision_policy.json")),
        broker_policy=BrokerPolicy.model_validate(parse("broker_policy.json")),
        events=PlatformEvents.model_validate(parse("platform_events.json")),
        adapter_catalog=AdapterCatalog.model_validate(parse("adapter_catalog.json")),
        state_machine=StateMachineSpec.model_validate(parse("state_machine.json")),
        salesforce_sync_fields=parse("salesforce_sync_fields.json"),
        shas=shas,
        bundle_hash=bundle_hash,
        policy_dir=policy_dir,
    )
