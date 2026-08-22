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


class ColumnRole(StrEnum):
    """How the cells under one column may be DRAWN. Display authority, owned by the claim.

    PROSE — an English cell, rendered and compared EXACTLY (modulo whitespace). Punctuation loss
            here is a defect in externally-binding text, not a rendering artifact.
    TOKEN — a cell of machine identifiers, wrapped between comma-separated items and never inside
            an identifier, because `platform` / `_account_id` is one a reader copies wrong and
            payload extras are accepted, so the misspelling 202s while populating nothing
            (re-audit `6feca36..4f23f23` F10). That rendering LOSES the commas, so the page
            comparison has to forgive them — which is exactly why the choice is authority and
            belongs to the registry rather than to whoever draws the table.
    """

    PROSE = "prose"
    TOKEN = "token"


# Short names for the declaration sites, so a column's role reads at a glance beside its title.
PROSE = ColumnRole.PROSE
TOKEN = ColumnRole.TOKEN


@dataclass(frozen=True)
class Column:
    """One column of a claim's table: its title, and the role that governs its cells.

    The role has NO default on purpose (Wave-2 re-audit-2 finding 1). A default is a role nobody
    wrote down, and this field decides which visible cells the release verifier will forgive
    punctuation loss in — so every column states its own, in the same reviewed record as the
    header it sits above, and the receipt closure pins the pair.
    """

    header: str
    role: ColumnRole

    def __post_init__(self) -> None:
        if type(self.header) is not str or not self.header.strip():
            raise ValueError("a column carries a non-empty title")
        if type(self.role) is not ColumnRole:
            raise ValueError(
                f"{self.header!r}: {self.role!r} is not a ColumnRole; the closed set is "
                f"{[r.value for r in ColumnRole]}"
            )


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
    # The ordered COLUMN SCHEMA, REQUIRED for a claim rendered as a table (Wave 2 F5, and its
    # re-audit twice over). The registry owns the complete matrix — header row included — so a
    # renderer has nothing of a table's content left to author: `projection.expected_matrix`
    # derives header + rows from this claim alone and refuses a claim whose declared width its
    # own rows do not fit.
    columns: tuple["Column", ...] = ()

    def __post_init__(self) -> None:
        for index, column in enumerate(self.columns):
            if type(column) is not Column:
                raise ValueError(
                    f"{self.id}: column {index} is {column!r}; a table's columns are typed "
                    "`Column` records carrying a header and the role that governs how the "
                    "cells beneath it may be drawn"
                )

    @property
    def headers(self) -> tuple[str, ...]:
        """The column titles, in order — the matrix's first row."""
        return tuple(column.header for column in self.columns)

    @property
    def token_columns(self) -> tuple[int, ...]:
        """Indices of the columns whose cells render as TOKEN cells.

        DERIVED from the schema, never stored beside it (Wave-2 re-audit-2 finding 1). This was a
        `token_columns` FIELD holding a parallel tuple of integers, and the receipt closure
        classified it as publishing no text — true of the integers, false of the authority they
        carry. `dataclasses.replace(WIRE["WIRE.INGEST.STATUS"], token_columns=(1,))` was a
        coherent registry edit that made the release verifier forgive punctuation loss in a PROSE
        column: the rendered 200 row shipped `(stored response verbatim) or an inline
        reviewer.manual_approve which runs` — commas gone from externally-binding text — with the
        receipt closure, the table lane, and `_top_level_verify` all green. The role now lives ON
        the column it governs, inside the same reviewed projection as the header, so flipping it
        is a re-pin a human reads. There is no field of this name left to replace.
        """
        return tuple(i for i, column in enumerate(self.columns) if column.role is ColumnRole.TOKEN)


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
