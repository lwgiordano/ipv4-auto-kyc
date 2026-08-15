"""The RUNTIME authority registry for capabilities this system publishes as absent.

Re-audit `1826661..b5c7a83` finding 6: the typed absence slots lived under the document layer
(docs/contracts), a package runtime is forbidden to import, so "shipping any part forces the gates forward"
rested on a four-string name sweep that a provider with different identifiers walked straight
past. The registry now lives HERE, in runtime, dependency-neutral (stdlib only, like
`kyc_tool.authority`): every official consumer — the retirement gates, the answer-artifact
resolution gate, and any future API route or worker that would exercise such an authority —
resolves through `resolve()`, and the published contract documents PROJECT this registry's
state rather than owning a copy of it.

The forcing property is scoped where it is executable: an unregistered provider has NO effect,
because nothing official consults anything but this registry; and `register()` — the one call
production would make to ship a provider — flips the slot away from `MissingCapability`, which
trips every consumer's rewrite-me branch and fails the dependent claim verifiers until the
claims, the gates, and the ROADMAP unit move in the same change.
"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@runtime_checkable
class RetirementEvidenceAuthority(Protocol):
    """What a shipped retirement-evidence provider must BE (R-audit-3 finding 11): the typed
    boundary registration validates, so `register(slot, object())` — an arbitrary object with
    no evidence surface — is refused, and 'MissingCapability | AuthorityProtocol' is the slot's
    real domain."""

    def inbound_zero_window_receipt(self, key_id: str) -> object: ...

    def signer_cutover_record(self, target_key_id: str) -> object: ...


@runtime_checkable
class AnswerArtifactAuthority(Protocol):
    """What a shipped answer-artifact verifying authority must BE."""

    def verify_artifact(self, artifact: object) -> list: ...


@dataclass(frozen=True)
class MissingCapability:
    """The typed absence of a runtime authority. Consumers dispatch on this TYPE: while a slot
    holds one, every dependent transition refuses with this reason; when a slot holds anything
    else, every consumer trips its rewrite-me branch and the claim verifiers fail."""

    reason: str
    roadmap_unit: str


SLOT_RETIREMENT_EVIDENCE = "retirement_evidence"
SLOT_ANSWER_ARTIFACT = "answer_artifact"

_SLOTS: dict[str, object] = {
    SLOT_RETIREMENT_EVIDENCE: MissingCapability(
        reason="no durable per-key fleet witness, signed fleet receipt, or HMAC signer-target "
               "cutover record exists",
        roadmap_unit="PR 5c",
    ),
    SLOT_ANSWER_ARTIFACT: MissingCapability(
        reason="no versioned, approved/signed answer-artifact schema or verifying authority "
               "exists",
        roadmap_unit="PR 7b-inputs",
    ),
}


def resolve(slot: str) -> object:
    """The one resolution path. Returns the slot's current holder — a `MissingCapability`
    until the owning ROADMAP unit ships and registers a real provider."""
    if slot not in _SLOTS:
        raise ValueError(f"unknown capability slot {slot!r}")
    return _SLOTS[slot]


_SLOT_PROTOCOLS: dict[str, type] = {
    SLOT_RETIREMENT_EVIDENCE: RetirementEvidenceAuthority,
    SLOT_ANSWER_ARTIFACT: AnswerArtifactAuthority,
}


def register(slot: str, provider: object) -> None:
    """Ship a provider into a slot — the call the owning unit makes when it lands. One
    authority per slot: registering over a live provider refuses; a `MissingCapability`,
    empty, or NON-CONFORMING provider is not a registration (R-audit-3 finding 11 — the slot's
    domain is MissingCapability | the slot's AuthorityProtocol, so an arbitrary object cannot
    become an authority)."""
    if slot not in _SLOTS:
        raise ValueError(f"unknown capability slot {slot!r}")
    if provider is None or isinstance(provider, MissingCapability):
        raise ValueError("a registration must ship a real provider, not another absence")
    if not isinstance(provider, _SLOT_PROTOCOLS[slot]):
        raise ValueError(
            f"the provider does not conform to {_SLOT_PROTOCOLS[slot].__name__}; a slot holds "
            "MissingCapability or a conforming authority, nothing else"
        )
    if not isinstance(_SLOTS[slot], MissingCapability):
        raise ValueError(f"slot {slot!r} already holds a registered authority")
    _SLOTS[slot] = provider
