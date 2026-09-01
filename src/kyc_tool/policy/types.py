"""Pydantic schemas for the normative machine_readable/*.json files.

Models are intentionally tolerant (`extra="allow"`) — the JSONs are the spec's
property, not ours; we validate what we consume and preserve the rest. Every
file must carry a `version` (the drift-guard test enforces sha↔version moves).
"""

from pydantic import BaseModel, ConfigDict, RootModel


class RubricItem(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    check_type: str
    points: int
    category: str
    mode: str
    source: str
    pass_rule: str
    cap: int | None = None


class ScoringRubric(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    version: str
    threshold: int
    hard_gates: dict
    items: tuple[RubricItem, ...]
    dynamic_rules: tuple[str, ...] = ()

    def item(self, check_type: str) -> RubricItem:
        for entry in self.items:
            if entry.check_type == check_type:
                return entry
        raise KeyError(f"check type not in rubric: {check_type}")

    @property
    def check_types(self) -> tuple[str, ...]:
        return tuple(entry.check_type for entry in self.items)

    @property
    def allowed_broker_statuses(self) -> tuple[str, ...]:
        return tuple(self.hard_gates.get("broker_status_allowed", ("clear", "allowed_broker")))


class DecisionRule(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    decision: str
    priority: int
    condition: str
    platform_enforcement: str


class DecisionPolicy(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    version: str
    decisions: tuple[DecisionRule, ...]
    manual_approve_exception: dict
    future_extension_not_in_v1: dict

    @property
    def decision_values(self) -> tuple[str, ...]:
        return tuple(rule.decision for rule in sorted(self.decisions, key=lambda r: r.priority))


class BrokerEntityRecord(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    name: str
    policy: str  # blocked | allowed


class BrokerPolicy(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    version: str
    matching_mode_v1: str
    identifier_classes: tuple[str, ...]
    entities: tuple[BrokerEntityRecord, ...]


class PlatformEvent(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    event_type: str
    tool_action: str


class PlatformEvents(RootModel[tuple[PlatformEvent, ...]]):
    @property
    def event_types(self) -> tuple[str, ...]:
        return tuple(event.event_type for event in self.root)


class AdapterCatalogEntry(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    adapter_id: str
    tier: str
    mode: str
    order: int


class AdapterCatalog(RootModel[tuple[AdapterCatalogEntry, ...]]):
    @property
    def adapter_ids(self) -> tuple[str, ...]:
        return tuple(entry.adapter_id for entry in sorted(self.root, key=lambda e: (e.order, e.adapter_id)))


class StateMachineSpec(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    version: str
    run_states: tuple[str, ...]
    transitions: tuple[tuple[str, ...], ...]
    case_statuses: tuple[str, ...]
    buy_statuses: tuple[str, ...]
