"""The closed registry of document identities — what the page FURNITURE may say.

Wave-2 re-audit finding 3. The title moved out of `build()` into a `DocumentManifest`, which
fixed the caller-supplied-title witness but left the identity itself as free text on a mutable
object in the GENERATOR: `Doc.build()` wrote `manifest.title` into every footer and the PDF
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
from types import MappingProxyType


@dataclass(frozen=True)
class DocumentIdentity:
    """One document's published identity: what its cover, footers, and metadata may say."""

    id: str
    title: str
    out: str

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("a document identity carries a stable id")
        if type(self.title) is not str or not self.title.strip():
            raise ValueError(f"{self.id}: a document identity carries a non-empty title")
        if type(self.out) is not str or not self.out.endswith(".pdf"):
            raise ValueError(f"{self.id}: a document identity names its output .pdf")

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
        ),
        DocumentIdentity(
            id=DEPLOYMENT_GUIDE,
            title="KYC Tool — Staging Integration and Production Readiness Guide",
            out="techcraft-deployment-guide.pdf",
        ),
        DocumentIdentity(
            id=TEST_FIXTURE,
            title="KYC Tool — test fixture",
            out="fixture.pdf",
        ),
    )
})


def identity(document_id: str) -> DocumentIdentity:
    """The identity for `document_id`, or a refusal naming the closed set."""
    try:
        return DOCUMENT_IDENTITIES[document_id]
    except KeyError:
        raise KeyError(
            f"no document identity {document_id!r}; the closed set is "
            f"{sorted(DOCUMENT_IDENTITIES)}"
        ) from None
