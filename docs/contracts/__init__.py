"""Typed contract registries for the externally-binding TechCraft documents.

Every value TechCraft builds against lives here ONCE, as structured data carrying a stable id, the
executable authority that governs it, and a lifecycle state. The PDF generators render these
records; the tests validate each record against its authority and then prove the rendered PDF
displays it.

The separation is the point (re-audit `82636da..9ac574f`, systemic prevention requirement). A
shared registry by itself is not evidence: when the generator and the test both read the same
wrong value they agree with each other and stay wrong. So the tests never compare the registry to
itself. Each claim is checked against an INDEPENDENT authority — a Pydantic model, a Settings
default, a runtime function, a migration, the canonical cutover record — and a separate pass
renders the document and asserts the claim's own rendering reaches the page exactly once.

Nothing here imports reportlab, so the registries and every authority check run in CI whether or
not the rendering toolchain is present.
"""

from dataclasses import dataclass, field
from enum import StrEnum


class ClaimState(StrEnum):
    """What an external reader may DO with a claim.

    SHIPPED  — the running code behaves this way today; build against it.
    PENDING  — an accepted design that does not exist yet; requirements only, not a schema to
               implement. (024 ordering bootstrap.)
    BLOCKED  — the documented capability cannot be exercised at all right now, and the document
               must say so where a reader would otherwise act. (Production providers: the config
               kill switch demands real providers and every real provider factory raises
               NotImplementedError, so a production launch is impossible until PR 9 lands.)
    """

    SHIPPED = "shipped"
    PENDING = "pending"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class Claim:
    """One externally-binding fact.

    id        stable identifier, referenced by the document model and by the tests
    value     the structured value; renderers format it, they never restate it
    authority dotted path or document reference the test validates against, so a reader (and the
              next auditor) can see WHAT proves this claim rather than trusting the registry
    state     ClaimState

    There is deliberately NO `note` field (Wave-2 audit finding 1). It held prose the documents
    published beside a claim, its docstring promised it "never carries authoritative data", and
    nothing enforced the promise: notes were subtracted from the authority map, so an inverted
    early-2xx receiver note and a "the named playbook is optional" cutover note both certified.
    Every sentence that lived there is now its own claim whose value is a
    `docs.contracts.statements.Statement` — text SELECTED by an executed fact — and therefore has
    a registered verifier like every other claim.

    exclusive_terms
              subjects that may be discussed only in ATTRIBUTED blocks. A document is not made
              correct by containing a correct claim: a contradictory paragraph beside it is just
              as visible, and an unclaimed one was invisible to every check we had (re-audit
              `4f23f23..97deeae` F3, which appended a contradictory commit-before-2xx paragraph
              and stayed green). `test_document_model` fails if one of these appears in a block
              carrying no claim id. Another CLAIM may mention the subject — claims are each held
              against an executable authority, so a contradiction there fails that test instead —
              but loose connective prose may not, because nothing verifies it.
    """

    id: str
    value: object
    authority: str
    state: ClaimState = ClaimState.SHIPPED
    exclusive_terms: tuple[str, ...] = ()
    # The ordered column titles, REQUIRED for a claim rendered as a table (Wave 2 F5). The
    # registry owns the complete matrix — header row included — so a renderer has nothing of a
    # table's content left to author: `projection.expected_matrix` derives header + rows from
    # this claim alone and refuses a claim whose declared width its own rows do not fit.
    table_headers: tuple[str, ...] = ()


@dataclass(frozen=True)
class Registry:
    """A closed, id-indexed set of claims."""

    name: str
    claims: tuple[Claim, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        ids = [c.id for c in self.claims]
        duplicates = {i for i in ids if ids.count(i) > 1}
        if duplicates:
            raise ValueError(f"{self.name}: duplicate claim ids {sorted(duplicates)}")

    def __getitem__(self, claim_id: str) -> Claim:
        for claim in self.claims:
            if claim.id == claim_id:
                return claim
        raise KeyError(f"{self.name}: no claim {claim_id!r}")

    def value(self, claim_id: str) -> object:
        return self[claim_id].value

    @property
    def ids(self) -> frozenset[str]:
        return frozenset(c.id for c in self.claims)
