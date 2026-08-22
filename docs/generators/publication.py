"""The ONE path a document takes out of this repo.

Re-audit-3 findings 2, 3 and 4 were three symptoms of one absence: the registry described a
published document — which module publishes it, under which title, under which filename, from
which commit — and then the executable commands did their own thing.

- `Doc.build(path, *, release=False)` — now `Doc.render`, with no such flag — held the
  provenance refusal, and both `main()` functions
  called it with the default. Patching `source_revision` to `deadbee+dirty` or `unknown` and
  calling either `main()` produced a finished, release-looking PDF stamped with a commit that
  cannot reproduce it. The test that "proved" the refusal called the helper, never the command.
- `--out` accepted any path, so the contract CLI wrote a Platform Integration Contract to
  `techcraft-deployment-guide.pdf` — the guide's reviewed, registry-owned filename — and nothing
  refused it.
- and the binding those two would have been checked against had just been broken for the
  documented `python -m` invocation.

So publication is a typed contract here, not a flag threaded through a renderer. A `Publication`
is resolved FROM the module doing the publishing — canonical dotted name, so `-m` and `import`
agree — and it owns every choice the registry says it owns: which document this is, what the file
is called, and that a release names a commit a reader can check out. `Doc.render` stays what it
always was, a renderer, and is what tests and previews use; it can no longer stamp a release,
because "release" is not a parameter any more.
"""

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

    def publish(self, doc, out_dir: str = ".") -> str:
        """Write the release artifact, or refuse.

        Three refusals, all of them things the registry already claimed to own:
        the document must be the one this module publishes; the file is named by the identity;
        and the stamped revision must be a commit someone else can check out.
        """
        if doc.document_id != self.identity.id:
            raise ValueError(
                f"{self.module} publishes {self.identity.id!r}, but this document is "
                f"{doc.document_id!r}; a publication is not a place to change which document "
                "you are"
            )
        revision = _revision()
        # A preview may be drawn from anything. A RELEASE artifact is the thing someone will
        # still be holding in a year, so it must name a commit anyone can check out.
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
        return doc.render(self.path(out_dir))


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
