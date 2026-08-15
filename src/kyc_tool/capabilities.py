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


def register(slot: str, provider: object) -> None:
    """Ship a provider into a slot — the call the owning unit makes when it lands. One
    authority per slot: registering over a live provider refuses, and a `MissingCapability`
    or empty provider is not a registration."""
    if slot not in _SLOTS:
        raise ValueError(f"unknown capability slot {slot!r}")
    if provider is None or isinstance(provider, MissingCapability):
        raise ValueError("a registration must ship a real provider, not another absence")
    if not isinstance(_SLOTS[slot], MissingCapability):
        raise ValueError(f"slot {slot!r} already holds a registered authority")
    _SLOTS[slot] = provider
