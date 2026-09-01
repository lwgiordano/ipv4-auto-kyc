"""The closed registry of document identities — what the page FURNITURE may say.

Wave-2 re-audit finding 3. The title moved out of `build()` into a `DocumentManifest`, which
fixed the caller-supplied-title witness but left the identity itself as free text on a mutable
object in the GENERATOR: the renderer wrote `manifest.title` into every footer and the PDF
metadata, and the furniture lane compared the page against `doc.expected_footers()`, derived from
the same object. That proves self-consistency, not authority — editing the generator's manifest
coherently republished `KYC Tool - Return 2xx before COMMIT` on twelve pages with every check
green.

Identity lives HERE now, on the registry side, keyed by document id:

- a generator names its document by ID; it cannot hand the renderer an identity object, so there
  is nothing at the generator layer to edit;
- `Doc` looks the identity up in this closed registry, and the furniture verifier looks it up the
  same way — independently of whatever the generator holds;
- the identities are reviewed FURNITURE prose, so the authority test pins their complete
  projection. Editing a title here is a re-pin a human reads, in a file that renders nothing.

Honest scope: a title is English, and no machine judges English. What this buys is that the
title has ONE home, outside the renderer's reach, with a receipt — not that "KYC Tool" is provably
the right name.
"""

from dataclasses import dataclass
from pathlib import PurePosixPath
from types import MappingProxyType


@dataclass(frozen=True)
class DocumentIdentity:
    """One document's published identity: what its cover, footers, and metadata may say.

    `module` and `registry` are the dotted names of the generator that publishes this document
    and of the registry its body is derived from. They are the half
    of the record that makes the identity BINDING rather than merely well-formed (re-audit-2
    finding 2). Without it the registry proved only that some registered title was stamped
    consistently: setting `techcraft_deployment_guide.DOCUMENT_ID = CONTRACT` published the
    six-page operational guide titled `KYC Tool — Platform Integration Contract`, on the cover, in
    every footer, and in the PDF metadata, and `_top_level_verify` passed — because the furniture
    lane looked up `doc.document_id`, which IS the generator's choice. The binding lives here, on
    the registry side, so the verifier can ask "which document is THIS generator's?" without
    consulting the generator. An identity with no module is not published by a generator at all
    (the test fixture).
    """

    id: str
    title: str
    out: str
    module: str = ""
    registry: str = ""

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("a document identity carries a stable id")
        if type(self.title) is not str or not self.title.strip():
            raise ValueError(f"{self.id}: a document identity carries a non-empty title")
        if type(self.out) is not str or not self.out.endswith(".pdf"):
            raise ValueError(f"{self.id}: a document identity names its output .pdf")
        # A BASENAME, enforced (re-audit-5 finding 3). `out` is described and used as the file a
        # reader receives, and `Publication.path` joins it onto the caller's directory — but the
        # only rule was the suffix, so `../escaped.pdf` resolved outside that directory and
        # `/tmp/escaped.pdf` discarded it entirely. Today's two values are safe; the invariant was
        # not, and a registry field that can leave the directory it is joined to is a hole whether
        # or not anyone has stepped in it.
        if PurePosixPath(self.out).name != self.out or self.out in (".", "..") or (
                "/" in self.out or "\\" in self.out):
            raise ValueError(
                f"{self.id}: {self.out!r} is not a plain filename; a document identity names the "
                "file a reader receives, never where on a disk it lands"
            )
        if type(self.module) is not str:
            raise ValueError(f"{self.id}: a publishing module is named by its dotted path")
        if type(self.registry) is not str:
            raise ValueError(f"{self.id}: a source registry is named by `module:attribute`")
        if bool(self.module) != bool(self.registry):
            raise ValueError(
                f"{self.id}: a published document names BOTH the module that publishes it and "
                "the registry its body comes from; one without the other leaves half the "
                "publication unbound"
            )

    def footer_line(self, section: str, revision: str) -> str:
        """The exact left-hand footer string for a page in `section`. ONE derivation, shared by
        the stamping canvas and the furniture verifier, so neither can drift from the other."""
        return (f"{self.title}  ·  {section}  ·  source {revision}" if section
                else f"{self.title}  ·  source {revision}")


CONTRACT = "techcraft-integration-contract"
DEPLOYMENT_GUIDE = "techcraft-deployment-guide"
# Ad-hoc Docs in the test suite are fixtures, not published documents, but identity is not
# something a caller may invent — so the fixture has an entry here like everything else, and its
# title says plainly what it is.
TEST_FIXTURE = "test-fixture"

DOCUMENT_IDENTITIES = MappingProxyType({
    identity.id: identity
    for identity in (
        DocumentIdentity(
            id=CONTRACT,
            title="KYC Tool — Platform Integration Contract",
            out="techcraft-integration-contract.pdf",
            module="docs.generators.techcraft_integration_contract",
            registry="docs.contracts.wire:WIRE",
        ),
        DocumentIdentity(
            id=DEPLOYMENT_GUIDE,
            title="KYC Tool — Staging Integration and Production Readiness Guide",
            out="techcraft-deployment-guide.pdf",
            module="docs.generators.techcraft_deployment_guide",
            registry="docs.contracts.operations:OPERATIONS",
        ),
        DocumentIdentity(
            id=TEST_FIXTURE,
            title="KYC Tool — test fixture",
            out="fixture.pdf",
        ),
    )
})

# module -> the ONE document it publishes. Built here rather than written out twice, and closed
# in both directions: two generators cannot claim one document, and one generator cannot claim
# two.
_BY_MODULE: dict = {}
for _entry in DOCUMENT_IDENTITIES.values():
    if not _entry.module:
        continue
    if _entry.module in _BY_MODULE:
        raise ValueError(
            f"{_entry.module} is bound to both {_BY_MODULE[_entry.module]!r} and {_entry.id!r}; "
            "a generator publishes exactly one document"
        )
    _BY_MODULE[_entry.module] = _entry.id
PUBLISHED_BY_MODULE = MappingProxyType(_BY_MODULE)


def identity(document_id: str) -> DocumentIdentity:
    """The identity for `document_id`, or a refusal naming the closed set."""
    try:
        return DOCUMENT_IDENTITIES[document_id]
    except KeyError:
        raise KeyError(
            f"no document identity {document_id!r}; the closed set is "
            f"{sorted(DOCUMENT_IDENTITIES)}"
        ) from None


def canonical_module(name: str, spec) -> str:
    """The dotted name of a module, the SAME whether it was imported or run with `python -m`.

    Re-audit-3 finding 3, a regression I shipped. Binding on `__name__` was right for an import
    and wrong for the only way these generators are documented to run: under `-m` Python sets
    `__name__ == "__main__"`, so both published commands died at import with
    `KeyError: "'__main__' publishes no registered document"` before argparse saw an argument.
    `__spec__.name` is the module's real dotted name in both cases, and it is not a literal a
    generator could choose — it is what the import system loaded.
    """
    resolved = getattr(spec, "name", "") if spec is not None else ""
    return resolved or name


def bound_id(module: str) -> str:
    """The id of the document `module` publishes — the registry's answer, not the module's.

    Both the generator (which asks what it is) and the furniture verifier (which asks what the
    generator under test should have published) resolve through here. That is the whole point:
    the two sides no longer share the generator's own variable, so a generator that stamps
    another registered document's identity is contradicted rather than confirmed.
    """
    try:
        return PUBLISHED_BY_MODULE[module]
    except KeyError:
        raise KeyError(
            f"{module!r} publishes no registered document; the closed set is "
            f"{sorted(PUBLISHED_BY_MODULE)}"
        ) from None


def identity_of(module: str) -> DocumentIdentity:
    """The identity of the document `module` publishes."""
    return identity(bound_id(module))
