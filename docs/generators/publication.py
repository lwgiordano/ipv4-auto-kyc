"""The ONE path a document takes out of this repo.

Re-audit-3 findings 2, 3 and 4 were three symptoms of one absence: the registry described a
published document — which module publishes it, under which title, under which filename, from
which commit — and then the executable commands did their own thing.

- `Doc.build(path, *, release=False)` — now `Doc.render`, with no such flag — held the provenance
  refusal, and both `main()` functions called it with the default. Patching `source_revision` to
  `deadbee+dirty` or `unknown` and calling either `main()` produced a finished, release-looking
  PDF stamped with a commit that cannot reproduce it. The test that "proved" the refusal called
  the helper, never the command.
- `--out` accepted any path, so the contract CLI wrote a Platform Integration Contract to
  `techcraft-deployment-guide.pdf` — the guide's reviewed, registry-owned filename — and nothing
  refused it.
- and the binding those two would have been checked against had just been broken for the
  documented `python -m` invocation.

The first version of this module fixed those and still let two things in (re-audit-4):

- it accepted a `Doc` a caller assembled and asked that document who it was. `document_id` was
  ordinary mutable state and the only thing checked, while rendering used the identity and
  registry cached at construction — so `doc = deploy_gen.build(); doc.document_id = CONTRACT`
  published six pages of operational guide under the contract's governed filename, and
  `Doc(OPERATIONS, DEPLOYMENT_GUIDE)` plus two arbitrary blocks published as the guide. A
  document's own id cannot be the evidence for what it is;
- and it AUTHORIZED one reading of the source revision while the renderer took another. Approving
  `aaaaaaa` and stamping `bbbbbbb` — or `bbbbbbb+dirty`, the exact artifact this gate promises to
  refuse — needed nothing but a commit landing between the two reads.

So a `Publication` binds the whole definition, all of it registry-side: the module, the identity,
the source registry, and the builder — which it CALLS. Nobody hands it a document. It takes one
provenance snapshot, validates that, renders with that exact value, re-attests it, and only then
promotes the finished file into place atomically, so a refusal never leaves a distributable
artifact behind.
"""

import importlib
import os
import pathlib
from dataclasses import dataclass

from docs.contracts import documents
from docs.contracts.documents import DocumentIdentity


class ProvenanceError(RuntimeError):
    """A release artifact could not name the commit that produced it."""


@dataclass(frozen=True)
class Publication:
    """The published document bound to one generator module."""

    module: str
    identity: DocumentIdentity

    def path(self, out_dir: str = ".") -> str:
        """Where this document goes: the caller's DIRECTORY, the registry's FILENAME.

        A destination directory is a deployment detail. The basename is identity — it is what a
        reader receives the file as, it is reviewed and pinned beside the title, and treating it
        as a mere default let one document be published under another's name.
        """
        return str(pathlib.Path(out_dir) / self.identity.out)

    def source_registry(self):
        """The registry this document's body must come from, resolved from the closed record."""
        module_name, _, attribute = self.identity.registry.partition(":")
        return getattr(importlib.import_module(module_name), attribute)

    def build(self, **inputs):
        """Build this publication's document by calling the BOUND generator.

        The builder is part of the definition, not an argument (re-audit-4 finding 1). While
        `publish` took a `Doc`, the only question it could ask was "what do you say you are?" —
        and a caller who assembles the document decides the answer. Asking the registry which
        module publishes this document, and calling THAT module, is the same question with an
        outside answer.
        """
        doc = importlib.import_module(self.module).build(**inputs)
        if doc.document_id != self.identity.id or doc.identity is not self.identity:
            raise ValueError(
                f"{self.module}.build() produced {doc.document_id!r}, not the "
                f"{self.identity.id!r} this publication publishes"
            )
        if doc.registry is not self.source_registry():
            raise ValueError(
                f"{self.identity.id}: this document's body must come from "
                f"{self.identity.registry}, not {getattr(doc.registry, 'name', doc.registry)!r}"
            )
        return doc

    def publish(self, out_dir: str = ".", **inputs) -> str:
        """Build and write the release artifact, or refuse, leaving nothing behind.

        The provenance value is read ONCE, validated, and passed into rendering, then read again
        and required to be unchanged before the finished file is promoted (re-audit-4 finding 2).
        Two independent reads let a release approve one commit and stamp another; one snapshot
        plus a re-attestation makes the window a refusal instead of a silent substitution.
        """
        authorized = _check(_revision())
        doc = self.build(**inputs)
        final = pathlib.Path(self.path(out_dir))
        # Written beside the destination so the promotion is a same-filesystem rename, and named
        # so that a partial file is never mistaken for the artifact.
        staged = final.with_name(f".{final.name}.{os.getpid()}.partial")
        try:
            doc.render(str(staged), revision=authorized)
            confirmed = _check(_revision())
            if confirmed != authorized:
                raise ProvenanceError(
                    f"the source tree changed while this document was being built "
                    f"({authorized} -> {confirmed}); the stamped commit is no longer the one "
                    "these pages were authorized from. Rebuild from a settled checkout."
                )
            os.replace(staged, final)
        finally:
            staged.unlink(missing_ok=True)
        return str(final)


def _check(revision: str) -> str:
    """A release names a commit anyone can check out, or it does not get made.

    A preview may be drawn from anything. A RELEASE artifact is the thing someone will still be
    holding in a year.
    """
    if revision == "unknown":
        raise ProvenanceError(
            "release build cannot determine the source commit; refusing to stamp a PDF "
            "'source unknown'. Build from a git checkout."
        )
    if revision.endswith("+dirty"):
        raise ProvenanceError(
            f"release build has uncommitted changes ({revision}); the stamped commit "
            "would not reproduce these pages. Commit first, then rebuild."
        )
    return revision


def _revision() -> str:
    """Read through the module so a test can patch the one function both sides consult."""
    from docs.generators import render

    return render.source_revision()


def publication_for(name: str, spec) -> Publication:
    """The publication bound to the calling module. Pass `__name__` and `__spec__`.

    Neither argument is a choice: one is what the module is called, the other is what the import
    system loaded it as. The registry answers which document that module publishes.
    """
    module = documents.canonical_module(name, spec)
    return Publication(module=module, identity=documents.identity_of(module))
