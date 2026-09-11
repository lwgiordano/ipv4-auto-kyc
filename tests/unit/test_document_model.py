"""The built PDF is compared to a typed document model, span by span.

Re-audit `4f23f23..97deeae` F3. The previous proof was "every required claim id appears in a list
of rendered ids" plus "the claim's text appears somewhere in the page". Counting ids and searching
globally cannot prove what a reader saw, and Codex demonstrated it: the suite stayed green after

  * the visible meaning of `WIRE.ORDERING.NO_DECIDED_AT` was removed while its id stayed recorded,
  * the eight canonical signing lines were reversed on the page,
  * and a visibly contradictory, unclaimed commit-before-2xx paragraph was appended.

None of those change an id count, and the first two do not change the SET of strings on the page
either — only their presence, order, and attribution. So this file works from
`docs.generators.render.Doc`'s block model: every block records the exact lines it emits, and each
one is located on the built page, in document order, exactly once, inside its declared section.
"""

import contextlib
import dataclasses
import hashlib
import importlib.machinery
import inspect
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pdfplumber
import pytest
from docs.contracts import (
    PROSE,
    Claim,
    Column,
    ColumnRole,
    Registry,
    documents,
    outline,
    projection,
)
from docs.contracts.documents import TEST_FIXTURE
from docs.contracts.operations import OPERATIONS
from docs.contracts.wire import WIRE
from docs.generators import render as render_module
from docs.generators import techcraft_deployment_guide as deploy_gen
from docs.generators import techcraft_integration_contract as contract_gen

# The four rendered-artifact lanes are PRODUCTION now (re-audit-6 finding 3): a release
# runs them against the staged PDF before promoting it, and this suite attacks the same code.
from docs.generators.artifact import (  # noqa: E402
    _flat,
    _page_footers,
    _page_prose,
    _verify_footers,
    _verify_page_matches_model,
    _verify_prose_stream,
    _verify_tables_match_model,
    body_text,
)
from docs.generators.render import Doc

from .test_contract_rendering import SAMPLE_CONTACT, _build

# A line short enough to occur legitimately more than once ("300", "POST", a repeated cell value).
# Above it, a second occurrence is a duplicate nobody asked for — which is the mutation
# "add an unrecorded duplicate block".
UNIQUE_LINE_CHARS = 45

# Ad-hoc Docs below are FIXTURES, not published documents; they name a fixture identity that
# lives in the same closed registry as the real ones, because identity is never a caller's to
# invent (Wave-2 re-audit finding 3).
TEST_DOCUMENT = TEST_FIXTURE

GENERATORS = [(contract_gen, WIRE), (deploy_gen, OPERATIONS)]

# the documented commands run from the repo root
REPO_ROOT = str(Path(__file__).resolve().parents[2])




def _normalize(text: str) -> str:
    """Casefolded, alphanumerics only. Tolerates a caller reformatting a leaf for display without
    tolerating its absence."""
    return re.sub(r"[^a-z0-9]", "", str(text).casefold())










def _page_text(generator, tmp_path) -> str:
    path = str(tmp_path / "model.pdf")
    _build(generator).render(path)
    return body_text(path)


def _page_tables(generator, tmp_path):
    path = str(tmp_path / "model-tables.pdf")
    _build(generator).render(path)
    tables = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                tables.append([[_flat(cell or "") for cell in row] for row in table])
    return tables


@pytest.fixture(scope="module", params=GENERATORS, ids=lambda g: g[0].__name__.split(".")[-1])
def rendered(request, tmp_path_factory):
    generator, registry = request.param
    tmp = tmp_path_factory.mktemp("model")
    return generator, registry, _build(generator), _page_text(generator, tmp), _page_tables(
        generator, tmp)


# ── the model reaches the page, in order, once each, in its own section ───────────────────────────
def test_every_block_line_is_on_the_page_in_document_order(rendered):
    """Order is the property a global search throws away. Reversing the eight canonical signing
    lines leaves every one of them present and changes nothing a set comparison can see."""
    _generator, _registry, doc, page, _tables = rendered
    _verify_page_matches_model(doc, page)


def test_substantive_lines_appear_exactly_once(rendered):
    """Multiplicity. A duplicate block displays a value twice, and a reader cannot tell which one
    is authoritative — nor can a coverage count notice."""
    _generator, _registry, doc, page, _tables = rendered
    for block in doc.blocks:
        if block.kind == "table":
            continue
        for line in block.lines:
            needle = _flat(line)
            if len(needle) < UNIQUE_LINE_CHARS:
                continue
            if page.count(needle) == 0:
                squashed = "".join(page.split())
                assert squashed.count("".join(needle.split())) == 1, (
                    f"{block.claim_id or block.kind}: wrapped content is not on the page exactly "
                    f"once: {needle[:60]!r}")
                continue
            assert page.count(needle) == 1, (
                f"{block.claim_id or block.kind}: appears {page.count(needle)} times: "
                f"{needle[:70]!r}"
            )


def test_every_claim_renders_inside_its_declared_section(rendered):
    """A claim in the wrong section is a claim under the wrong heading. The reader takes the
    heading as its scope, so `decided_at` guidance filed under Retention is misleading even though
    every word of it is correct."""
    _generator, _registry, doc, page, _tables = rendered
    headings = [
        (section.section_id, page.find(_flat(section.title)))
        for section in doc.sections if section.title
    ]
    assert all(offset >= 0 for _id, offset in headings), headings

    for section in doc.sections:
        if not section.title:
            continue
        start = page.find(_flat(section.title))
        later = [o for _i, o in headings if o > start]
        end = min(later) if later else len(page)
        for block in section.blocks:
            if block.claim_id is None or block.kind == "table":
                continue
            anchor = max((_flat(line) for line in block.lines), key=len, default="")
            if len(anchor) < 12:
                continue
            at = page.find(anchor)
            assert start <= at < end, (
                f"{block.claim_id} renders outside section {section.section_id!r}"
            )








def test_every_table_is_the_registry_matrix_on_the_page_in_order(rendered):
    _generator, _registry, doc, _page, tables = rendered
    _verify_tables_match_model(doc, tables)


# ── the page is the model and NOTHING ELSE (Wave 2 F6, prose half) ────────────────────────────────






# ── the furniture lane: the footer band is derived, not free (Wave-2 audit finding 3) ────────────




def test_the_page_furniture_is_exactly_the_manifests_derivation(rendered, tmp_path):
    generator, _registry, _doc, _page, _tables = rendered
    doc = _build(generator)
    path = str(tmp_path / "furniture.pdf")
    doc.render(path)
    _verify_footers(doc, path, generator)


def test_w2f3_a_false_title_cannot_be_supplied_at_build_time():
    """The original witness, refused at the signature: no title parameter, and no identity
    OBJECT either — a Doc names its document by id and looks the identity up itself."""
    doc = _build(contract_gen)
    with pytest.raises(TypeError):
        doc.render("/tmp/unused.pdf", "KYC Tool — Return 2xx before COMMIT")
    import inspect

    assert "title" not in inspect.signature(render_module.Doc.render).parameters
    with pytest.raises(KeyError, match="no document identity"):
        render_module.Doc(WIRE, "KYC Tool — Return 2xx before COMMIT")


def test_w3f3_a_generator_cannot_publish_an_identity_it_authors(tmp_path):
    """Wave-2 re-audit finding 3, the exact trigger. Monkeypatching the generator's MANIFEST used
    to republish a false title on every page with `_top_level_verify` green, because the verifier
    derived its expectation from the same object the renderer stamped.

    There is no such object now: the generator holds an ID, and both the stamp and the check read
    the closed identity registry. Setting the ID to anything unregistered fails at build, and
    monkeypatching the module's DOCUMENT_ID to another REGISTERED document publishes that
    document's real title — which the furniture lane, reading the identity for the document the
    verifier was asked about, refuses.
    """
    monkey = contract_gen.DOCUMENT_ID
    try:
        contract_gen.DOCUMENT_ID = "KYC Tool — Return 2xx before COMMIT"
        with pytest.raises(KeyError, match="no document identity"):
            _build(contract_gen)
    finally:
        contract_gen.DOCUMENT_ID = monkey


@pytest.mark.parametrize(
    ("module", "registry", "stolen"),
    [(deploy_gen, OPERATIONS, documents.CONTRACT),
     (contract_gen, WIRE, documents.DEPLOYMENT_GUIDE)],
    ids=["guide-as-contract", "contract-as-guide"])
def test_w4f2_a_generator_cannot_publish_another_registered_documents_identity(
        module, registry, stolen, tmp_path, monkeypatch):
    """Re-audit-2 finding 2, both swaps, through the gate a release actually runs.

    The identity registry being closed stopped a generator inventing a title; it did not stop one
    publishing under another registered document's. `techcraft_deployment_guide.DOCUMENT_ID =
    CONTRACT` shipped the six-page operational guide with the cover, all six footers and the PDF
    metadata reading `KYC Tool — Platform Integration Contract`, and `_top_level_verify` passed —
    a reader could receive the guide AS the contract with the verifier green. The old check here
    only compared the two documents' footers to each other, which proves the titles differ today,
    not that either document used its own.

    The registry binds generator to document now, and the furniture lane asks it about the module
    under test rather than reading the id the module chose. A stolen identity is a real, pinned
    title on the wrong body — and that is exactly what fails.
    """
    monkeypatch.setattr(module, "DOCUMENT_ID", stolen)
    with pytest.raises(AssertionError, match="not the .*derivation"):
        _top_level_verify(module, registry, tmp_path)


def test_w4f2_the_registry_binds_each_generator_to_the_document_it_publishes():
    """And the binding is the registry's, not a restatement of what the generators already say:
    a module asks which document it is, so the two sides cannot agree by construction."""
    import inspect

    bindings = {
        contract_gen: documents.CONTRACT,
        deploy_gen: documents.DEPLOYMENT_GUIDE,
    }
    for module, expected in bindings.items():
        assert documents.bound_id(module.__name__) == expected
        assert expected == module.DOCUMENT_ID, (
            f"{module.__name__} publishes as {module.DOCUMENT_ID!r}, not its bound document")
        source = inspect.getsource(module)
        assert "publication_for(__name__, globals().get(\"__spec__\"))" in source, (
            f"{module.__name__} must ASK the registry which document it is")
        for literal in (documents.CONTRACT, documents.DEPLOYMENT_GUIDE, documents.TEST_FIXTURE):
            assert f'"{literal}"' not in source, (
                f"{module.__name__} names a document id literally; the binding is the registry's")
    # closed both ways: every published identity is bound to one module, and an unbound module
    # has no document at all
    published = {i.id for i in documents.DOCUMENT_IDENTITIES.values() if i.module}
    assert set(documents.PUBLISHED_BY_MODULE.values()) == published == set(bindings.values())
    with pytest.raises(KeyError, match="publishes no registered document"):
        documents.bound_id("docs.generators.not_a_generator")


def test_w4f3_the_binding_survives_being_run_as_a_command(tmp_path):
    """Re-audit-3 finding 3, a regression I shipped in the identity fold.

    Binding on `__name__` is right for an import and wrong for the only way these generators are
    documented to run. Under `python -m`, `__name__` is `"__main__"`, so both published commands
    died at import — before argparse, before rendering — with `'__main__' publishes no registered
    document`. `__spec__.name` is the module's real dotted name either way, and the subprocess
    REDs below are the boundary an imported-function test cannot reach.
    """
    spec = importlib.machinery.ModuleSpec("docs.generators.techcraft_deployment_guide", None)
    assert documents.canonical_module("__main__", spec) == spec.name
    assert documents.canonical_module("docs.generators.x", None) == "docs.generators.x"
    for module in (contract_gen, deploy_gen):
        assert module.PUBLICATION.module == module.__name__


@pytest.mark.parametrize(
    ("command", "expected"),
    [(["-m", "docs.generators.techcraft_deployment_guide"], "techcraft-deployment-guide.pdf"),
     (["-m", "docs.generators.techcraft_integration_contract",
       "--integration-contact", SAMPLE_CONTACT],
      "techcraft-integration-contract.pdf")],
    ids=["guide", "contract"])
def test_w4f234_the_documented_commands_publish_governed_artifacts(command, expected, tmp_path):
    """Re-audit-3 findings 2, 3 and 4 at the only place they are all true at once: the commands
    the documents' own docstrings tell an operator to run, executed as subprocesses.

    A dirty or unknown revision is refused HERE, not merely in a helper the command never called;
    the artifact is named by the registry, not by an argument; and the command reaches argparse
    at all. The run is given a clean revision through the environment probe the renderer uses.
    """
    env = {**os.environ, "PATH": str(tmp_path / "fakebin") + os.pathsep + os.environ["PATH"]}
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "git").write_text(
        # `printf`, not `echo -n`: POSIX leaves echo's option handling implementation-defined and
        # macOS /bin/sh emits the literal bytes `-n\n`, which `source_revision` then reads as a
        # dirty tree — so this fixture passed on CI and failed the same assertion on a developer's
        # machine (re-audit-4 finding 3). A release-boundary regression that is not portable is
        # not a regression test.
        "#!/bin/sh\ncase \"$*\" in *\"rev-parse\"*) echo abc1234 ;; *) printf '%s' '' ;; esac\n")
    (fake / "git").chmod(0o755)
    out_dir = tmp_path / "published"
    out_dir.mkdir()

    done = subprocess.run(  # noqa: S603 — fixed argv, no shell
        [sys.executable, *command, "--out-dir", str(out_dir)],
        capture_output=True, text=True, cwd=REPO_ROOT, env=env, check=False)
    assert done.returncode == 0, done.stderr[-2000:]
    assert [p.name for p in out_dir.iterdir()] == [expected], (
        "the published filename is the registry's")
    with pdfplumber.open(out_dir / expected) as pdf:
        assert "source abc1234 " in (pdf.pages[0].extract_text() or "")

    # …and the same command refuses a revision a reader could not check out
    (fake / "git").write_text(
        '#!/bin/sh\ncase "$*" in *"rev-parse"*) echo abc1234 ;; *) echo " M docs/x.py" ;; esac\n')
    (fake / "git").chmod(0o755)
    dirty = subprocess.run(  # noqa: S603 — fixed argv, no shell
        [sys.executable, *command, "--out-dir", str(out_dir)],
        capture_output=True, text=True, cwd=REPO_ROOT, env=env, check=False)
    assert dirty.returncode != 0 and "uncommitted changes" in dirty.stderr

    (fake / "git").write_text("#!/bin/sh\nexit 1\n")
    (fake / "git").chmod(0o755)
    unknown = subprocess.run(  # noqa: S603 — fixed argv, no shell
        [sys.executable, *command, "--out-dir", str(out_dir)],
        capture_output=True, text=True, cwd=REPO_ROOT, env=env, check=False)
    assert unknown.returncode != 0 and "source commit" in unknown.stderr


def test_w4f4_a_publication_refuses_another_documents_body_and_basename(tmp_path, monkeypatch):
    """The two refusals the CLI used to have no opinion about. `--out` took any path, so the
    contract published itself over `techcraft-deployment-guide.pdf` — the guide's reviewed,
    registry-owned filename — with a Platform Integration Contract inside and nothing refusing.
    There is no such argument: a caller picks a DIRECTORY, and the publication that owns the
    filename also refuses a document that is not the one it publishes."""
    from docs.generators import publication

    monkeypatch.setattr(render_module, "source_revision", lambda: "abc1234")
    guide_pub = deploy_gen.PUBLICATION
    assert "--out" not in inspect.getsource(contract_gen.main).replace("--out-dir", "")
    assert guide_pub.path(str(tmp_path)).endswith("techcraft-deployment-guide.pdf")
    # the builder is the publication's, so a document that is not this one can only arrive by
    # the bound generator producing it — and that is refused by name
    monkeypatch.setattr(deploy_gen, "build",
                        lambda **_: _build(contract_gen))
    with pytest.raises(ValueError, match="not the .*this publication publishes"):
        guide_pub.publish(str(tmp_path))
    assert not list(tmp_path.iterdir()), "a refused publication writes nothing"
    assert publication.publication_for(
        "docs.generators.techcraft_integration_contract", None).identity.id == documents.CONTRACT


def test_w5f1_a_document_cannot_be_its_own_evidence_for_what_it_is(tmp_path, monkeypatch):
    """Re-audit-4 finding 1, both witnesses, at `publish`.

    The publication bound module, identity and filename, then asked the DOCUMENT who it was. Its
    id was ordinary mutable state and the only thing checked, while rendering used the identity
    and registry cached at construction:

      doc = techcraft_deployment_guide.build()
      doc.document_id = documents.CONTRACT
      techcraft_integration_contract.PUBLICATION.publish(doc, tmp)

    wrote the guide's six pages — cover, footers and metadata all saying guide — as
    `techcraft-integration-contract.pdf`. And `Doc(OPERATIONS, DEPLOYMENT_GUIDE)` plus two
    arbitrary blocks published as the guide, because a caller may construct the target id around
    any body at all.

    Neither witness has a door now: `document_id` is read-only, and `publish` takes no document —
    it calls the generator the registry binds to this publication and checks the result against
    the whole closed definition, registry included.
    """
    monkeypatch.setattr(render_module, "source_revision", lambda: "abc1234")
    # …no document parameter at all: the first witness cannot even be expressed
    assert "doc" not in inspect.signature(deploy_gen.PUBLICATION.publish).parameters
    with pytest.raises(AttributeError):
        deploy_gen.build().document_id = documents.CONTRACT

    # …and the second: a body from the wrong registry is refused by name, before anything is drawn
    monkeypatch.setattr(deploy_gen, "build",
                        lambda **_: _hollow_doc(WIRE, documents.DEPLOYMENT_GUIDE))
    with pytest.raises(ValueError, match="body must come from"):
        deploy_gen.PUBLICATION.publish(str(tmp_path))
    assert not list(tmp_path.iterdir())
    assert deploy_gen.PUBLICATION.source_registry() is OPERATIONS
    assert contract_gen.PUBLICATION.source_registry() is WIRE


def _hollow_doc(registry, document_id):
    """A Doc with the target id built around an arbitrary body — Codex's second witness."""
    doc = Doc(registry, document_id)
    doc.title()
    doc.p("Anything at all.")
    return doc


def test_w6f1_a_correct_label_hollow_body_is_not_the_reviewed_document(tmp_path, monkeypatch):
    """Re-audit-5 finding 1, the exact witness, through the release path.

    Binding the builder closed "who assembled this document"; it did not ask what the document
    SAYS. `publish` checked three labels — id, identity object, registry object — and a body of

        doc.title(); doc.p("This arbitrary two-block body is not the reviewed deployment guide.")

    satisfies all three. It published as the governed `techcraft-deployment-guide.pdf`: right
    title, right metadata, right footer, one page, every operational section a reader needs
    simply gone. `_top_level_verify` passed it too, because that helper asks whether the PAGE
    matches the DOCUMENT — which a hollow document answers trivially — and the publication
    command never ran it.

    The reviewed projection is production authority now, and the release path checks it.
    """
    monkeypatch.setattr(render_module, "source_revision", lambda: "abc1234")
    hollow = _hollow_doc(OPERATIONS, documents.DEPLOYMENT_GUIDE)

    found = outline.problems(hollow)
    assert found and "sections are not the reviewed document's" in found[0]

    monkeypatch.setattr(deploy_gen, "build", lambda **_: hollow)
    with pytest.raises(ValueError, match="not the document that was reviewed"):
        deploy_gen.PUBLICATION.publish(str(tmp_path))
    assert not list(tmp_path.iterdir()), "a refused release leaves nothing behind"


def test_w6f1_the_outline_is_ordered_and_total_over_both_documents():
    """Guard the guard. The outline has to accept the real documents (or it is not the reviewed
    projection) and refuse every ordinary way a body can drift (or it is theatre)."""
    inputs = {"contact": SAMPLE_CONTACT}
    contract = contract_gen.build(**inputs)
    assert outline.problems(contract, inputs) == []
    assert outline.problems(deploy_gen.build()) == []

    # a dropped block, a reordered pair, a claim swapped for another, and edited narration
    guide = deploy_gen.build()
    section = next(s for s in guide.sections if s.section_id == "processes")
    original = list(section.blocks)

    section.blocks = original[:-1]
    assert any("blocks, reviewed with" in p for p in outline.problems(guide))

    section.blocks = [original[1], original[0], *original[2:]]
    assert outline.problems(guide), "a reordered pair must be visible"

    section.blocks = list(original)
    prose = next(i for i, b in enumerate(original) if b.kind == "prose" and not b.claim_id)
    section.blocks[prose] = dataclasses.replace(
        original[prose], lines=("Disable the healthcheck on API containers too.",))
    assert any("text changed since it was reviewed" in p for p in outline.problems(guide))

    section.blocks = list(original)
    claim_at = next(i for i, b in enumerate(original) if b.claim_id)
    section.blocks[claim_at] = dataclasses.replace(original[claim_at], claim_id="OPS.HEALTH.PROBES")
    assert any("reviewed as" in p for p in outline.problems(guide))

    # …and the contract's release slot must be the REVIEWED SENTENCE with this release's values
    # in it — not merely a line that mentions them (re-audit-6 finding 2)
    assert any("not the reviewed sentence" in p
               for p in outline.problems(contract, {**inputs, "contact": "someone@else.example"}))
    assert any("supplied no ['contact']" in p
               for p in outline.problems(contract, {}))


def test_w6f2_a_prepositioned_staging_symlink_cannot_be_followed_or_promoted(
        tmp_path, monkeypatch):
    """Re-audit-5 finding 2, the exact witness.

    The staged name was `.<governed-name>.<pid>.partial` and was opened by pathname, so a symlink
    pre-positioned there was followed: the target was overwritten with PDF bytes and `os.replace`
    then promoted the SYMLINK, leaving the governed artifact pointing at the file the release had
    just destroyed. Anything writable by the publisher was reachable that way.
    """
    from docs.generators import publication

    monkeypatch.setattr(render_module, "source_revision", lambda: "abc1234")
    victim = tmp_path / "outside-target.txt"
    victim.write_bytes(b"DO-NOT-OVERWRITE")
    out_dir = tmp_path / "publish"
    out_dir.mkdir()
    (out_dir / f".techcraft-deployment-guide.pdf.{os.getpid()}.partial").symlink_to(victim)

    final = Path(deploy_gen.PUBLICATION.publish(str(out_dir)))
    assert victim.read_bytes() == b"DO-NOT-OVERWRITE", "the release wrote through a symlink"
    assert not final.is_symlink() and final.is_file()
    assert not any(p.is_symlink() for p in out_dir.iterdir() if p.name == final.name)

    # and the guard itself, with the staging name forced to collide: exclusive, no-follow, refused
    monkeypatch.setattr(publication.secrets, "token_hex", lambda _n=8: "deadbeefdeadbeef")
    (out_dir / ".techcraft-deployment-guide.pdf.deadbeefdeadbeef.partial").symlink_to(victim)
    before = final.read_bytes()
    with pytest.raises(publication.StagingError, match="it did not create"):
        deploy_gen.PUBLICATION.publish(str(out_dir))
    assert victim.read_bytes() == b"DO-NOT-OVERWRITE"
    assert final.read_bytes() == before, "a refused release replaced the standing artifact"


def test_w6f3_a_document_identity_names_a_file_not_a_place(tmp_path):
    """Re-audit-5 finding 3. `out` is joined onto the caller's directory, and the only rule was
    the `.pdf` suffix — so `../escaped.pdf` resolved outside that directory and `/tmp/escaped.pdf`
    discarded it entirely. Today's two values are safe; the invariant was not."""
    for escape in ("../escaped.pdf", "/tmp/escaped.pdf", "sub/dir.pdf", "..", "."):
        with pytest.raises(ValueError):
            documents.DocumentIdentity(id="x", title="X", out=escape,
                                       module="m", registry="r:R")
    for module in (contract_gen, deploy_gen):
        published = Path(module.PUBLICATION.path(str(tmp_path)))
        assert published.parent == tmp_path, "a published artifact is a child of the chosen dir"
        assert published.name == module.PUBLICATION.identity.out


def test_w5f2_a_release_stamps_the_revision_it_authorized(tmp_path, monkeypatch):
    """Re-audit-4 finding 2. `publish` validated one reading of the source revision and the
    renderer took another, so a commit landing between the two reads meant a release approved
    `aaaaaaa` and stamped `bbbbbbb` — or `bbbbbbb+dirty`, the exact artifact this gate promises
    to refuse — with no refusal anywhere.

    One snapshot is authorized, passed into rendering, and re-attested before the finished file is
    promoted. A moving tree is now a refusal that leaves nothing distributable behind.
    """
    from docs.generators import publication

    final = tmp_path / "techcraft-deployment-guide.pdf"
    # a clean commit that is simply not the authorized one, and a tree that went dirty under the
    # build — the second is refused as dirty, which is the more specific of the two true reasons
    for second, refusal in (("bbbbbbb", "changed while this document"),
                            ("bbbbbbb+dirty", "uncommitted changes")):
        seen = iter(("aaaaaaa", second))
        monkeypatch.setattr(render_module, "source_revision", lambda s=seen: next(s))
        with pytest.raises(publication.ProvenanceError, match=refusal):
            deploy_gen.PUBLICATION.publish(str(tmp_path))
        assert not list(tmp_path.iterdir()), (
            f"a release refused mid-build left an artifact behind ({second})")

    # a settled tree publishes, and what it stamps is what it authorized
    monkeypatch.setattr(render_module, "source_revision", lambda: "abc1234")
    out = deploy_gen.PUBLICATION.publish(str(tmp_path))
    assert out == str(final)
    with pdfplumber.open(out) as pdf:
        assert "source abc1234 " in (pdf.pages[0].extract_text() or "")
    assert [p.name for p in tmp_path.iterdir()] == [final.name], "no staged file survives"


def test_w3f3_the_identity_registry_is_the_only_source_of_furniture_text():
    """Structural: neither generator holds identity TEXT, so there is nothing at that layer for a
    coherent edit to falsify. The registry is the one home, and its complete projection is pinned
    by the authority suite."""
    import inspect

    for module in (contract_gen, deploy_gen):
        source = inspect.getsource(module)
        published = documents.identity_of(module.__name__)
        assert published.title not in source, (
            f"{module.__name__} restates its own title; identity belongs to the registry alone")
        assert not hasattr(module, "MANIFEST")


def test_w2f3_a_tampered_footer_title_fails_the_furniture_lane(tmp_path):
    """And if the stamp itself is subverted — an identity swapped between layout and stamping —
    the lane refuses, so the control is the comparison and not merely the shape of the API."""
    doc = _build(contract_gen)
    hostile = documents.DocumentIdentity(
        id="hostile", title="KYC Tool — Return 2xx before COMMIT", out="hostile.pdf")
    real = render_module._stamped_canvas

    def stamped_with_lie(_identity, revision, page_sections=None):
        return real(hostile, revision, page_sections)

    render_module._stamped_canvas = stamped_with_lie
    try:
        path = str(tmp_path / "false-footer.pdf")
        doc.render(path)
    finally:
        render_module._stamped_canvas = real
    with pytest.raises(AssertionError, match="not the .*derivation"):
        _verify_footers(doc, path, contract_gen)


@pytest.mark.parametrize("generator", [contract_gen, deploy_gen], ids=["contract", "guide"])
def test_w2f3_the_real_release_paths_publish_governed_furniture(generator, tmp_path, monkeypatch):
    """Both real `main()` entry points, executed — the paths a release actually runs, not a
    rebuilt Doc that happens to resemble them.

    They are RELEASE paths now (re-audit-3 finding 2), so a clean revision is part of running
    them at all; the suite runs against a working tree, which is exactly the state the real
    command refuses."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(render_module, "source_revision", lambda: "abc1234")
    if generator is contract_gen:
        out = generator.main(["--integration-contact", SAMPLE_CONTACT])
        doc = generator.build(contact=SAMPLE_CONTACT)
    else:
        out = generator.main([])
        doc = generator.build()
    doc.render(str(tmp_path / "twin.pdf"))
    assert _page_footers(out) == _page_footers(str(tmp_path / "twin.pdf"))
    _verify_footers(doc, out, generator)
    with pdfplumber.open(out) as pdf:
        assert (pdf.metadata.get("Title") or "") == documents.identity(generator.DOCUMENT_ID).title


def test_the_page_prose_is_exactly_the_model_and_nothing_else(rendered, tmp_path):
    generator, _registry, doc, _page, _tables = rendered
    path = str(tmp_path / "prose-total.pdf")
    _build(generator).render(path)
    _verify_prose_stream(doc, _page_prose(path))


# ── attribution: nothing authoritative is said outside the claim that owns it ─────────────────────
def test_exclusive_terms_appear_only_in_their_own_claim(rendered):
    """The appended-contradictory-paragraph mutation. A claim being correct does not make the page
    correct: an unclaimed paragraph next to it is equally visible and was attributable to nothing."""
    _generator, registry, doc, _page, _tables = rendered
    for claim in registry.claims:
        for term in claim.exclusive_terms:
            for block in doc.blocks:
                if block.claim_id is not None:
                    continue  # another claim may reference the subject; its authority test covers it
                if block.kind == "heading":
                    continue  # a heading NAMES the subject; it does not assert anything about it
                haystack = " ".join(list(block.lines) + [c for r in block.rows for c in r])
                # word boundaries: "COMMIT" must not match "commitment"
                hit = re.search(rf"\b{re.escape(term)}\b", haystack, re.IGNORECASE)
                assert hit is None, (
                    f"{term!r} is owned by {claim.id} but appears in unattributed "
                    f"{block.kind} prose, which nothing verifies"
                )


def test_at_least_the_two_mutated_claims_declare_exclusive_terms():
    """Guard the guard: an empty `exclusive_terms` everywhere makes the test above vacuous."""
    declared = [c.id for c in (*WIRE.claims, *OPERATIONS.claims) if c.exclusive_terms]
    assert "WIRE.CALLBACK.RECEIVER_TXN" in declared
    assert "WIRE.ORDERING.NO_DECIDED_AT" in declared


def test_every_claim_block_carries_visible_lines(rendered):
    """`_emit` refuses a flowable with no visible text, so a block that displays nothing cannot be
    recorded — which is the 'delete a visible block while preserving its id' mutation."""
    _generator, _registry, doc, _page, _tables = rendered
    for block in doc.blocks:
        if block.claim_id is None:
            continue
        assert block.lines or block.rows, f"{block.claim_id} recorded nothing visible"


def test_claim_ids_and_blocks_agree(rendered):
    """`rendered` (the id list) and the block model must describe the same document."""
    _generator, _registry, doc, _page, _tables = rendered
    from_blocks = Counter(b.claim_id for b in doc.blocks if b.claim_id)
    assert from_blocks == Counter(doc.rendered)
    # every recorded line carries exactly one closed presentation role (re-audit-11 finding 2);
    # the Block constructor enforces the pairing, so here we prove the DOCUMENTS exercise it
    for block in doc.blocks:
        assert len(block.roles) == len(block.lines)
        assert set(block.roles) <= set(render_module.ROLE_STYLES)


# ── the named mutations from the audit, each of which used to pass ────────────────────────────────
@contextlib.contextmanager
def _mutated(registry, claim_id, **changes):
    """Swap one claim in place for the duration of a test.

    In place, not a copy: the generators, the authority map and this module all hold the same
    Registry object, so a replacement bound to one name would leave the others verifying the
    original — and the mutation would "fail" for the wrong reason, or pass.
    """
    original = registry.claims
    mutant = dataclasses.replace(registry[claim_id], **changes)
    object.__setattr__(
        registry, "claims", tuple(mutant if c.id == claim_id else c for c in original))
    try:
        yield
    finally:
        object.__setattr__(registry, "claims", original)


def _top_level_verify(generator, registry, tmp_path):
    """Everything a release runs: the authority map, the rendered-page comparison, and the
    ordered table-matrix comparison (Wave 2 F5 — a mutation that only reorders or re-columns a
    table is invisible to the prose lane, so leaving tables out of this harness would let every
    table mutation 'fail' against a bespoke check the release never runs)."""
    from docs.contracts import authority

    for claim in registry.claims:
        authority.AUTHORITY_VERIFIERS[claim.id]()
    # …and the receipt closure, so the release verifier covers the REVIEWED lane too (Wave-2
    # re-audit finding 1). Codex's inverted sentences passed `_top_level_verify` precisely
    # because this ran only in the authority suite: an executed answer cannot judge English, so
    # the pin that does has to be part of the same gate.
    problems = authority._receipt_problems((WIRE, OPERATIONS))
    assert not problems, "\n".join(problems)

    doc = _build(generator)
    path = str(tmp_path / "mutant.pdf")
    doc.render(path)
    _verify_page_matches_model(doc, body_text(path))
    tables = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                tables.append([[_flat(cell or "") for cell in row] for row in table])
    _verify_tables_match_model(doc, tables)
    _verify_prose_stream(doc, _page_prose(path))
    # the band the other three lanes subtract (Wave-2 finding 3), checked against the identity
    # the REGISTRY binds to this generator rather than the one the generator picked
    _verify_footers(doc, path, generator)


def _hmac_rule_saying(answer: str):
    """The HMAC-set statement re-declared with the other answer, so it publishes the other
    sentence. Used by the mutation table below."""
    from docs.contracts.statements import Statement

    published = OPERATIONS.value("OPS.CONFIG.HMAC_SET_RULE")
    return Statement(fact=published.fact, answer=answer,
                     alternatives=dict(published.alternatives))


MUTATIONS = [
    # Every one of these left the previous top-level suite green.
    ("renaming a canonical signing line", WIRE, contract_gen, "WIRE.SIGN.CANONICAL",
     {"value": ("v2", "NOT_KEY_ID", "direction", "method", "path?query", "timestamp", "slot",
                "sha256(body) hex")}),
    ("swapping a required HMAC variable for an unrelated real setting", OPERATIONS, deploy_gen,
     "OPS.CONFIG.HMAC_SET",
     {"value": ("KYC_PLATFORM_HMAC_SECRET", "KYC_DATABASE_URL", "KYC_HMAC_INBOUND_SECRET",
                "KYC_HMAC_OUTBOUND_KEY_ID", "KYC_HMAC_OUTBOUND_SECRET",
                "KYC_HMAC_V1_INBOUND_SUNSET_AT", "KYC_HMAC_V1_OUTBOUND_SUNSET_AT",
                "KYC_HMAC_V1_OBSERVATION_WINDOW_DAYS")}),
    # the floor/date promise is its own claim now (Wave-2 finding 1). Declaring the other answer
    # publishes "Production accepts a subset…", which the executed boundary refuses.
    ("advertising a partial HMAC set as acceptable", OPERATIONS, deploy_gen,
     "OPS.CONFIG.HMAC_SET_RULE",
     {"value": _hmac_rule_saying("accepts_partial")}),
    ("prescribing a rolling security cutover", OPERATIONS, deploy_gen,
     "OPS.RELEASE.CLASSIFICATION",
     {"value": "Releases without a migration are always safe to deploy rolling, including "
               "security releases."}),
    ("deleting the accepted 025 requirements", WIRE, contract_gen, "WIRE.ORDERING.BOOTSTRAP_025",
     {"value": "NOT BUILT"}),
    ("reversing the eight visible canonical lines", WIRE, contract_gen, "WIRE.SIGN.CANONICAL",
     {"value": ("sha256(body) hex", "slot", "timestamp", "path?query", "method", "direction",
                "key_id", "v2")}),
    ("removing the visible meaning while keeping the id", WIRE, contract_gen,
     "WIRE.ORDERING.NO_DECIDED_AT", {"value": "decided_at is present on every callback."}),
    ("making the receiver answer before it commits", WIRE, contract_gen,
     "WIRE.CALLBACK.RECEIVER_TXN",
     {"value": ("Verify the signature.", "Only then return 2xx.",
                "COMMIT in your own time afterwards.")}),
]


@pytest.mark.parametrize(
    ("description", "registry", "generator", "claim_id", "changes"),
    MUTATIONS, ids=[m[0] for m in MUTATIONS])
def test_each_demonstrated_mutation_fails_the_top_level_verifier(
        description, registry, generator, claim_id, changes, tmp_path):
    """The list Codex proved could pass. Each one is applied to the live registry and driven
    through the SAME entry point a release runs — the authority map plus the rendered-page
    comparison — so a mutation that only a bespoke check would catch does not count."""
    with _mutated(registry, claim_id, **changes), pytest.raises(AssertionError):
        _top_level_verify(generator, registry, tmp_path)


def test_the_unmutated_documents_pass_the_same_verifier(tmp_path):
    """Guard the guard: if `_top_level_verify` failed unconditionally, every row above would
    'pass' while proving nothing."""
    for generator, registry in GENERATORS:
        _top_level_verify(generator, registry, tmp_path)


def test_mutation_a_contradictory_unclaimed_paragraph_fails(tmp_path):
    """An unattributed paragraph that contradicts a claim, added straight to the document."""
    doc = _build(contract_gen)
    doc.p("You may return 2xx before committing; we will retry if you fail later.")

    offenders = []
    for claim in WIRE.claims:
        for term in claim.exclusive_terms:
            for block in doc.blocks:
                if block.claim_id == claim.id:
                    continue
                if term.lower() in " ".join(block.lines).lower():
                    offenders.append((claim.id, term, block.claim_id))
    assert offenders, "the contradictory paragraph was not attributed to anything and passed"


def test_mutation_moving_a_claim_to_the_wrong_section_fails(tmp_path):
    """Rendered under Retention, the `decided_at` prohibition reads as a retention rule."""
    doc = _build(contract_gen)
    misfiled = [s for s in doc.sections if s.section_id == "retention"][0]
    ordering = [s for s in doc.sections if s.section_id == "ordering"][0]
    block = next(b for b in ordering.blocks if b.claim_id == "WIRE.ORDERING.NO_DECIDED_AT")
    ordering.blocks.remove(block)
    misfiled.blocks.append(block)
    assert doc.section_of("WIRE.ORDERING.NO_DECIDED_AT") == "retention"
    # and the real check would reject it, because the text renders before the Retention heading
    path = str(tmp_path / "misfiled.pdf")
    doc.render(path)
    with pdfplumber.open(path) as pdf:
        page = _flat("\n".join(p.extract_text() or "" for p in pdf.pages))
    start = page.find(_flat(misfiled.title))
    anchor = max((_flat(line) for line in block.lines), key=len)
    assert page.find(anchor) < start, "the misfiled claim would have to move on the page too"


# ── the F5/F6 table witnesses, each proven certifying before the matrix lane existed ──────────────
#
# Survey probes against the pre-fold tree (`a4d9be4`): with the caller still assembling rows,
# reversing every effectiveness row, swapping the Effective?/Why values under unchanged headers,
# moving a Why to its neighbouring row, and appending a whole reversed duplicate of the table
# directly to the story each left BOTH document suites fully green (123 passed, every time).
# The tests below hold the exact witnesses against the comparison a release now runs.

def _extracted_tables(doc, tmp_path, name):
    path = str(tmp_path / f"{name}.pdf")
    doc.render(path)
    tables = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                tables.append([[_flat(cell or "") for cell in row] for row in table])
    return tables


TABLE_BETRAYALS = [
    ("drawing every row reversed", lambda rows: tuple(reversed(rows))),
    ("swapping the Effective? and Why values under unchanged headers",
     lambda rows: tuple((*row[:3], row[4], row[3]) for row in rows)),
    ("moving one row's Why onto its neighbour",
     lambda rows: ((*rows[0][:4], rows[1][4]), (*rows[1][:4], rows[0][4]), *rows[2:])),
    ("duplicating the first row over its neighbour",
     lambda rows: (rows[0], rows[0], *rows[2:])),
]


@pytest.mark.parametrize(("label", "mutate"), TABLE_BETRAYALS, ids=[t[0] for t in TABLE_BETRAYALS])
def test_a_renderer_that_draws_other_than_its_recorded_matrix_fails(tmp_path, label, mutate):
    """The hostile half the model cannot see: the renderer RECORDS the honest registry matrix
    and DRAWS something else. Only the page-side ordered comparison can refuse this — a bag of
    cells cannot, which is how all four of these certified before the fold."""
    claim_id = "WIRE.CALLBACK.EFFECTIVENESS"
    real = render_module.Doc.claim_table

    def hostile(self, cid, widths, heading=None):
        if cid != claim_id:
            return real(self, cid, widths, heading=heading)
        claim = self.registry[cid]
        matrix = projection.expected_matrix(claim)
        drawn = (matrix[0], *mutate(matrix[1:]))
        data = [[render_module.Paragraph(render_module.escape(h), render_module.CELLB)
                 for h in drawn[0]]]
        for row in drawn[1:]:
            data.append([render_module._prose_cell(cell, widths[i])
                         for i, cell in enumerate(row)])
        table = render_module.Table(data, colWidths=widths, repeatRows=1)
        table.setStyle(render_module._TABLE_STYLE)
        self._story.append(table)
        self.rendered.append(cid)
        self._add(render_module.Block(kind="table", claim_id=cid, lines=(), rows=matrix,
                                      projection=projection.TABLE,
                                      row_fields=claim.row_fields))

    render_module.Doc.claim_table = hostile
    try:
        doc = _build(contract_gen)
    finally:
        render_module.Doc.claim_table = real
    tables = _extracted_tables(doc, tmp_path, "betrayal")
    with pytest.raises(AssertionError, match="not the model's rows in order"):
        _verify_tables_match_model(doc, tables)


@pytest.mark.parametrize(("label", "mutate"), TABLE_BETRAYALS, ids=[t[0] for t in TABLE_BETRAYALS])
def test_a_recorded_matrix_that_is_not_the_registrys_fails(label, mutate):
    """The coherent half: the renderer records exactly what it draws, but neither is the
    registry's matrix. MODEL == REGISTRY refuses it before the page is even consulted, so a
    coherent dual mutation cannot certify the way the caller-assembled rows once did."""
    doc = _build(contract_gen)
    for section in doc.sections:
        for i, block in enumerate(section.blocks):
            if block.claim_id == "WIRE.CALLBACK.EFFECTIVENESS" and block.kind == "table":
                tampered = (block.rows[0], *mutate(block.rows[1:]))
                section.blocks[i] = dataclasses.replace(block, rows=tampered)
    with pytest.raises(AssertionError, match="not the registry's matrix"):
        _verify_tables_match_model(doc, [])


def _injected_table(matrix, widths):
    data = [[render_module.Paragraph(render_module.escape(h), render_module.CELLB)
             for h in matrix[0]]]
    for row in matrix[1:]:
        data.append([render_module._prose_cell(cell, widths[i]) for i, cell in enumerate(row)])
    table = render_module.Table(data, colWidths=widths, repeatRows=1)
    table.setStyle(render_module._TABLE_STYLE)
    return table


def test_an_injected_duplicate_table_fails_the_concatenation(tmp_path):
    """The R15 F6 table witness, verbatim: a whole reversed duplicate of the effectiveness table
    appended straight to the story — no block, no claim, every word already legitimate. The
    survey probe proved it certified (123 passed). Multiplicity in the per-header concatenation
    refuses it now: the header class gains rows the model never recorded."""
    doc = _build(contract_gen)
    claim = doc.registry["WIRE.CALLBACK.EFFECTIVENESS"]
    matrix = projection.expected_matrix(claim)
    widths = [0.6 * render_module.INCH, 1.85 * render_module.INCH, 1.2 * render_module.INCH,
              1.15 * render_module.INCH, 2.15 * render_module.INCH]
    doc._story.append(_injected_table((matrix[0], *reversed(matrix[1:])), widths))
    tables = _extracted_tables(doc, tmp_path, "dup-table")
    with pytest.raises(AssertionError, match="not the model's rows in order"):
        _verify_tables_match_model(doc, tables)


def test_an_injected_novel_header_table_is_refused_by_name(tmp_path):
    """A table whose header tuple belongs to no model block — built from words the documents
    already use, so the vocabulary check cannot be the control that speaks."""
    doc = _build(contract_gen)
    doc._story.append(_injected_table(
        (("Record", "Why"), ("record it", "the platform recovers it")),
        [2 * render_module.INCH, 3 * render_module.INCH]))
    tables = _extracted_tables(doc, tmp_path, "novel-table")
    with pytest.raises(AssertionError, match="no model block accounts for"):
        _verify_tables_match_model(doc, tables)


def test_claim_table_accepts_no_caller_rows_or_headers():
    """The escape hatch is gone at the signature, not policed by convention: there is no
    parameter through which a caller could hand claim_table a cell or a column title."""
    doc = _build(contract_gen)
    with pytest.raises(TypeError):
        doc.claim_table("WIRE.CALLBACK.LEGEND", [1 * render_module.INCH], rows=[("a", "b")])
    with pytest.raises(TypeError):
        doc.claim_table("WIRE.CALLBACK.LEGEND", [1 * render_module.INCH],
                        headers=("Token", "Meaning"))
    import inspect

    params = inspect.signature(render_module.Doc.claim_table).parameters
    assert "rows" not in params and "headers" not in params


def test_a_table_claim_without_declared_headers_cannot_render():
    """expected_matrix refuses a claim with no declared columns, so the registry cannot quietly
    hand column-title authorship back to a caller."""
    doc = Doc(Registry(name="headless", claims=(
        Claim(id="X.NOHEAD", value=(("a", "b"),), authority="test"),
    )), TEST_DOCUMENT)
    with pytest.raises(ValueError, match="declares no columns"):
        doc.claim_table("X.NOHEAD", [1 * render_module.INCH, 1 * render_module.INCH])
    with pytest.raises(ValueError, match="does not fit the declared"):
        projection.expected_matrix(Claim(id="X.WIDTH", value=(("a", "b", "c"),), authority="test",
                                         columns=(Column("One", PROSE), Column("Two", PROSE))))


def test_w2f4_a_prose_cell_that_loses_its_commas_fails(tmp_path):
    """Wave-2 audit finding 4, the exact trigger: a `_prose_cell` that eats commas before
    drawing. The live status row rendered "malformed envelope malformed payload or an invalid
    reviewer actor" and passed every comparison, because the forgiveness token cells need was
    applied to every column. Scoped to declared token columns, this refuses."""
    real = render_module._prose_cell

    def comma_eating(text, width):
        return real(str(text).replace(",", ""), width)

    render_module._prose_cell = comma_eating
    try:
        doc = _build(contract_gen)
    finally:
        render_module._prose_cell = real
    tables = _extracted_tables(doc, tmp_path, "comma-eaten")
    with pytest.raises(AssertionError, match="not the model's rows in order"):
        _verify_tables_match_model(doc, tables)


def test_w2f4_token_columns_still_forgive_their_own_line_breaks(rendered):
    """Guard the guard: the forgiveness must survive where it is real. The event table's payload
    columns are token cells whose commas the renderer genuinely replaces with line breaks, so
    scoping the rule must not make honest documents fail."""
    _generator, _registry, doc, _page, tables = rendered
    commas = [
        cell
        for b in doc.blocks if b.kind == "table" and b.code_columns
        for row in b.rows[1:]
        for i, cell in enumerate(row) if i in b.code_columns and "," in cell
    ]
    if not commas:
        pytest.skip("this document's token columns carry no comma-separated lists")
    _verify_tables_match_model(doc, tables)


@contextlib.contextmanager
def _swapped_claim(registry, claim_id, **changes):
    """Swap one claim in place for the duration of a test — in place, so every holder of the
    Registry object (generators, the authority map, this module) sees the mutant."""
    original = registry.claims
    mutant = dataclasses.replace(registry[claim_id], **changes)
    object.__setattr__(
        registry, "claims", tuple(mutant if c.id == claim_id else c for c in original))
    try:
        yield
    finally:
        object.__setattr__(registry, "claims", original)


LIE_ACK = ("A 2xx before your commit is recoverable and safe; the word unrecoverable is only a "
           "label and at-least-once will retry it after a lost commit.")
LIE_PLAYBOOK = ("Do not EXECUTE from the named playbook. It is merely a reference; plan and run "
                "the cutover from this summary instead.")


@pytest.mark.parametrize(("claim_id", "answer", "lie"), [
    ("WIRE.CALLBACK.ACK_CONSEQUENCE", "unrecoverable", LIE_ACK),
    ("OPS.CUTOVER.EXECUTION_SOURCE", "playbook_only", LIE_PLAYBOOK),
], ids=["early-2xx", "playbook-optional"])
def test_w3f1_a_constructible_inversion_fails_the_release_verifier(claim_id, answer, lie,
                                                                   tmp_path):
    """Wave-2 re-audit finding 1, both witnesses verbatim.

    Codex's point was exact and I had overclaimed: the token rule is a SUBSTRING check, and
    substring membership is not meaning. Each of these sentences carries its answer's own token,
    avoids its sibling's, constructs cleanly, and inverts what the document tells TechCraft — one
    says an early 2xx is recoverable while the publisher terminalizes the row, the other tells
    operators not to execute from the safety playbook.

    Nothing here pretends a machine now understands them. The ANSWER is executed; the SENTENCES
    are reviewed English, pinned per branch, and the release verifier runs that closure — so a
    rewritten sentence is refused as an unreviewed edit, at the same gate a release passes
    through, which is where these two used to slip by.
    """
    from docs.contracts.statements import Statement

    registry = WIRE if claim_id.startswith("WIRE.") else OPERATIONS
    generator = contract_gen if registry is WIRE else deploy_gen
    published = registry.value(claim_id)
    alternatives = dict(published.alternatives)
    alternatives[answer] = lie
    hostile = Statement(fact=published.fact, answer=answer, alternatives=alternatives)
    assert hostile.text == lie, "the witness must actually be constructible"

    with (
        _swapped_claim(registry, claim_id, value=hostile),
        pytest.raises(AssertionError, match="changed since it was reviewed"),
    ):
        _top_level_verify(generator, registry, tmp_path)


def test_w3f1_the_token_rule_is_declared_defense_in_depth_not_authority():
    """Guard against re-overclaiming: the module must say plainly that its substring rule does
    not judge meaning, and the sentences must be covered by the reviewed lane rather than the
    verifier-bound one."""
    from docs.contracts import statements as statements_module
    from docs.contracts.authority import REGISTRY_PROSE_PINS, VERIFIER_BOUND

    doc = statements_module.__doc__ or ""
    assert "DEFENSE IN DEPTH" in doc.upper() and "not meaning" in doc
    branches = [k for k in REGISTRY_PROSE_PINS if "Statement.alternatives{" in k[1]]
    assert len(branches) >= 20, "every alternative branch must sit in the reviewed lane"
    assert not [k for k in VERIFIER_BOUND if "Statement.alternatives" in k[1]], (
        "an alternative sentence is reviewed prose, never verifier-bound English")


def test_w3f2_a_caller_cannot_declare_a_prose_column_lossy(tmp_path):
    """Wave-2 re-audit finding 2, the exact trigger. `code_columns` used to be a caller argument
    that `_emit` recorded and the page comparison then TRUSTED, so a caller could mark a prose
    column a token column and have its comma loss forgiven by a check reading its own
    declaration: the live 200-row rendered without punctuation and `_top_level_verify` passed.

    The declaration is the claim's now. The parameter is gone from the signature, and if a
    renderer draws token columns the claim does not declare, the comparison says so by name.
    """
    doc = _build(contract_gen)
    import inspect

    assert "code_columns" not in inspect.signature(render_module.Doc.claim_table).parameters
    with pytest.raises(TypeError):
        doc.claim_table("WIRE.INGEST.STATUS", [1 * render_module.INCH], code_columns=(1,))

    # and the recorded schema must equal the claim's, so a renderer that draws something else is
    # refused rather than believed
    for section in doc.sections:
        for i, block in enumerate(section.blocks):
            if block.claim_id == "WIRE.INGEST.STATUS" and block.kind == "table":
                section.blocks[i] = dataclasses.replace(block, code_columns=(1,))
    tables = _extracted_tables(doc, tmp_path, "hostile-schema")
    with pytest.raises(AssertionError, match="the claim declares"):
        _verify_tables_match_model(doc, tables)


def test_w4f1_a_column_role_cannot_exist_apart_from_the_column_it_governs():
    """Re-audit-2 finding 1, the structural half. The declaration used to be a parallel tuple of
    integers — a second list to keep in step with the headers, and a field a coherent
    `dataclasses.replace` could rewrite. There is no such field: a role is a member of a closed
    set, written on the column it governs, so 'outside its table' and 'aimed at the wrong column'
    are not states this registry can be in.
    """
    for registry in (WIRE, OPERATIONS):
        for claim in registry.claims:
            assert len(claim.headers) == len(claim.columns)
            for index in claim.token_columns:
                assert 0 <= index < len(claim.columns), (claim.id, index)
    with pytest.raises(TypeError):  # no field of this name is left to replace
        dataclasses.replace(WIRE["WIRE.INGEST.STATUS"], token_columns=(1,))
    with pytest.raises(ValueError, match="not a ColumnRole"):
        Column("One", "token")
    with pytest.raises(ValueError, match="typed `Column` records"):
        Claim(id="X.BAD", value=(("a", "b"),), authority="t", columns=("One", "Two"))


def test_w4f1_a_registry_edit_cannot_make_a_prose_column_lossy(tmp_path):
    """Re-audit-2 finding 1, Codex's exact witness, through the gate a release actually runs.

    Marking `WIRE.INGEST.STATUS`'s Meaning column as a token column is a coherent registry edit:
    every row still fits, the renderer honours it, and the page comparison then forgives the
    commas `_token_cell` destroys. On the pre-fold tree it printed `(stored response verbatim) or
    an inline reviewer.manual_approve which runs without a queued job` — punctuation stripped
    from externally-binding text — with `_receipt_problems` and `_top_level_verify` both green,
    because the display schema was classified as publishing no text.

    The role is reviewed registry authority now, so the same edit changes a pinned receipt and
    the release verifier refuses it by name.
    """
    from docs.contracts.authority import _receipt_problems

    from .test_contract_registry_authority import _swapped_claim

    lossy = tuple(dataclasses.replace(column, role=ColumnRole.TOKEN) if index == 1 else column
                  for index, column in enumerate(WIRE["WIRE.INGEST.STATUS"].columns))
    with _swapped_claim(WIRE, "WIRE.INGEST.STATUS", columns=lossy):
        problems = _receipt_problems((WIRE, OPERATIONS))
        assert any("Column.role" in p and "changed since it was reviewed" in p
                   for p in problems), problems
        with pytest.raises(AssertionError, match="changed since it was reviewed"):
            _top_level_verify(contract_gen, WIRE, tmp_path)


def test_the_story_has_no_public_append():
    """The R15 F6 witness was one line: `doc.story.append(...)`. That line no longer exists —
    the public story is a read-only tuple view, so a flowable can only enter through a method
    that records its Block in the same call."""
    doc = _build(contract_gen)
    with pytest.raises(AttributeError):
        doc.story.append("anything")
    with pytest.raises(AttributeError):
        doc.story = []


def test_an_injected_prose_flowable_fails_the_total_stream(tmp_path):
    """The R15 F6 witness verbatim, forced through the PRIVATE story the way a hostile module
    would: every word already legitimate, no block, visible to a reader. The survey probe proved
    the full suites stayed green on the pre-fold tree; the total page==model prose stream is the
    control that refuses it now."""
    doc = _build(contract_gen)
    doc._story.append(render_module.Paragraph("Return 2xx before COMMIT.", render_module.BODY))
    path = str(tmp_path / "injected-prose.pdf")
    doc.render(path)
    with pytest.raises(AssertionError, match="not exactly the model's prose"):
        _verify_prose_stream(doc, _page_prose(path))


def test_a_duplicated_governed_paragraph_fails_the_total_stream(tmp_path):
    """Duplicate-and-relocate, tuned to duck every older control: the re-drawn line is a real
    recorded step SHORT enough to fall under the exactly-once check's UNIQUE_LINE_CHARS floor,
    so the multiplicity test skips it, the order check skips it, and the vocabulary is all
    legitimate. Character equality of the total stream has no floor, so it is the one control
    that refuses the page carrying the model twice."""
    doc = _build(contract_gen)
    block = next(b for b in doc.blocks if b.claim_id == "WIRE.CALLBACK.RECEIVER_TXN")
    line = next(str(li) for li in block.lines if len(_flat(li)) < UNIQUE_LINE_CHARS)
    doc._story.append(render_module.Paragraph(render_module.escape(line), render_module.BODY))
    path = str(tmp_path / "dup-prose.pdf")
    doc.render(path)
    with pytest.raises(AssertionError, match="not exactly the model's prose"):
        _verify_prose_stream(doc, _page_prose(path))


def test_the_release_inputs_still_reach_the_page(rendered):
    _generator, _registry, _doc, page, _tables = rendered
    if _generator is contract_gen:
        assert SAMPLE_CONTACT in page


def test_no_section_id_is_reused():
    for generator, _registry in GENERATORS:
        doc = _build(generator)
        ids = [s.section_id for s in doc.sections]
        assert len(ids) == len(set(ids)), ids
        assert re.match(r"^[a-z0-9()_ -]+$", "".join(ids))


# ── the oracle is the REGISTRY, not the renderer ─────────────────────────────────────────────────
def test_the_page_matches_content_recomputed_from_the_registry(rendered):
    """Re-audit `4f23f23..122cc67` finding 3. The previous comparison used `block.lines` — the
    lines the RENDERER had just computed — so editing `claim_paragraph` to display something other
    than its claim changed the page and the expectation together and the suite stayed green.

    Here the expected content is recomputed FROM THE CLAIM through `docs.contracts.projection`,
    which the renderer does not author. A renderer that displays anything else is compared against
    what the claim actually says.
    """
    _generator, registry, doc, page, tables = rendered
    squashed = "".join(page.split())

    for block in doc.blocks:
        if block.claim_id is None:
            continue
        claim = registry[block.claim_id]

        if block.projection == projection.COMPOSED:
            # A table can no longer be COMPOSED (Wave 2 F5): claim_table derives the whole
            # matrix, so the caller has no rows to assemble. Prose composition remains — the
            # caller writes the sentence, but every leaf the claim owns must still be printed.
            # Compared NORMALIZED — casefolded with punctuation removed — because a caller
            # legitimately reformats a leaf on its way to the page (`job_max_attempts` is printed
            # as the environment variable `KYC_JOB_MAX_ATTEMPTS`). Omission and contradiction are
            # still caught; only presentation is tolerated.
            assert block.kind != "table", (
                f"{block.claim_id}: a COMPOSED table block should be unconstructible now")
            haystack = _normalize(page)
            for leaf in projection.leaf_strings(claim.value, block.row_fields):
                needle = _normalize(leaf)
                if len(needle) < 4:
                    continue
                assert needle in haystack, (
                    f"{block.claim_id}: the page omits a value the claim carries: {leaf[:70]!r}")
            continue

        if block.projection == projection.TABLE:
            # the ordered-matrix lane owns tables completely: model == registry matrix and
            # page == model in order, multiplicity included (_verify_tables_match_model)
            continue

        wanted = projection.expected_lines(claim, block.projection)
        assert wanted, f"{block.claim_id}: projection {block.projection!r} produced nothing"
        for line in wanted:
            needle = _flat(line)
            if len(needle) < 4:
                continue
            assert needle in page or "".join(needle.split()) in squashed, (
                f"{block.claim_id}: recomputed line not on the page: {needle[:70]!r}")


def test_the_renderer_cannot_author_the_expectation(rendered):
    """Guard the guard: the recorded block lines must EQUAL what the registry projection says, so
    the two halves cannot quietly diverge and leave the test above checking the renderer's own
    answer."""
    _generator, registry, doc, _page, _tables = rendered
    for block in doc.blocks:
        if block.claim_id is None or block.projection in (projection.COMPOSED, projection.TABLE):
            continue
        wanted = projection.expected_lines(registry[block.claim_id], block.projection)
        recorded = tuple(line for line in block.lines if line in wanted)
        assert set(wanted) <= set(block.lines), (
            f"{block.claim_id}: recorded {recorded} but the registry projects {wanted}")


@pytest.mark.parametrize(
    ("claim_id", "registry", "generator", "lie"),
    [
        ("WIRE.ORDERING.NO_DECIDED_AT", WIRE, contract_gen,
         "decided_at is the ordering key; sort by it."),
        ("OPS.PROCESS.DEV_WORKER_BANNED", OPERATIONS, deploy_gen,
         "dev_worker is fine in production."),
    ],
    ids=["contract", "guide"],
)
def test_a_renderer_that_displays_something_other_than_the_claim_fails(
        claim_id, registry, generator, lie, tmp_path, monkeypatch):
    """The exact mutation: make the renderer print a lie in place of the claim's value. The page
    and the renderer's own record change together — and the registry-derived expectation does
    not, so it fails."""
    from docs.generators import render as render_module

    original = render_module.Doc.claim_paragraph

    def lying_claim_paragraph(self, cid, *, prefix=""):
        if cid != claim_id:
            return original(self, cid, prefix=prefix)
        from docs.generators.render import Block, Paragraph, escape

        self._story.append(Paragraph(prefix + escape(lie), render_module.BODY))
        self.rendered.append(cid)
        self._add(Block(kind="prose", claim_id=cid, lines=(lie,), roles=("BODY",),
                        projection=projection.PARAGRAPH))
        return None

    monkeypatch.setattr(render_module.Doc, "claim_paragraph", lying_claim_paragraph)
    doc = _build(generator)
    path = str(tmp_path / "lie.pdf")
    doc.render(path)
    page = body_text(path)

    wanted = projection.expected_lines(registry[claim_id], projection.PARAGRAPH)[0]
    assert _flat(wanted) not in page, "the mutation did not actually remove the claim's text"
    with pytest.raises(AssertionError):
        for block in doc.blocks:
            if block.claim_id != claim_id:
                continue
            for line in projection.expected_lines(registry[block.claim_id], block.projection):
                assert _flat(line) in page, f"{block.claim_id}: recomputed line not on the page"


# ── nothing on the page is unaccounted for ────────────────────────────────────────────────────
#
# The tests above prove the MODEL reaches the page. They say nothing about the other direction:
# a paragraph that belongs to no claim is a fact nothing verifies, and the F3 attack was exactly
# that — a contradictory commit-before-2xx paragraph appended beside the claim it contradicted.
# `exclusive_terms` catches that only for subjects somebody thought to declare in advance, which
# is the wrong shape for a control: it enumerates what to look for instead of what is allowed.
#
# So unattributed blocks are a CLOSED, PINNED set. Every one is listed here with a digest over its
# exact rendered content. Adding prose fails (an unlisted key), deleting it fails (a listed key
# with nothing behind it), and editing it fails (a digest mismatch). Re-pinning is the review, the
# same act `playbook_digest` requires of a cutover section.
#
# The residual risk is stated plainly: this makes unattributed prose a visible, reviewed decision;
# it does not make the reviewer read it. What it removes is the SILENT path — prose can no longer
# arrive on an externally-binding page without a human touching this table.

NARRATION_LABELS = {
    # ── contract ──────────────────────────────────────────────────────────────────────────────
    ("contract", "(front matter)", 0): ("b70e26fd070d110c", "Audience + scope of this document"),
    ("contract", "(front matter)", 1): ("21a2f1b76bd4de4b", "one-paragraph summary of the wire"),
    ("contract", "(front matter)", 2): ("2c4ad3bf2cfb9b3a",
                                        "where to send answers; interpolates the release contact, "
                                        "which test_the_release_inputs_still_reach_the_page "
                                        "checks separately"),
    ("contract", "asks", 0): ("c77063c9a829011c", "1.1 heading + why ordering is asked for"),
    ("contract", "asks", 1): ("f7662c6b9304e6b7", "1.1 what we need now is not code"),
    ("contract", "asks", 2): ("cb7699e00b602dd7",
                              "1.1 necessary and not sufficient; screened but "
                              "cannot clear (gate finding 10)"),
    ("contract", "asks", 3): ("3f901c37e3839bdc", "1.2 dedupe commitment"),
    ("contract", "asks", 4): ("87c2e7c956b36fc0", "1.3 callback URL and key exchange"),
    ("contract", "asks", 5): ("a6143e1512e363d3", "1.4 document upload path"),
    ("contract", "events", 0): ("4009c5156c229907", "first event creates the case; no rate limit"),
    ("contract", "events", 1): ("939d3d69279a82cb", "the label 'Envelope:'"),
    ("contract", "events", 2): ("4dc71563f2253448", "the worked envelope example"),
    ("contract", "callbacks", 0): ("c1d5f1bafb61d43a", "content type + which key signs"),
    ("contract", "signing", 0): ("6651f36142732ce8", "why v2 replaced v1"),
    ("contract", "checklist", 0): ("bb248f15a20df14d", "the go-live checklist table"),
    # ── guide ─────────────────────────────────────────────────────────────────────────────────
    ("guide", "(front matter)", 0): ("f36ddedf7d0de988", "audience + pointer to the contract"),
    ("guide", "blocker", 0): ("fcecf71459ea67c0", "what this guide does and does not cover"),
    ("guide", "blocker", 1): ("06389181dce3477d", "who maintains the code and cuts releases"),
    ("guide", "blocker", 2): ("04ea3b2467e78210", "the three in-release references"),
    ("guide", "processes", 0): ("7fcf2738c2096c61", "how the image is built"),
    ("guide", "processes", 1): ("e8b1fd6efdad4dd6", "disable the HTTP healthcheck on workers"),
    ("guide", "configuration", 0): ("b69bc9148329c468", "where the full config sample lives"),
    ("guide", "releases", 0): ("fae3434680bd1512", "the rolling-release shape"),
    ("guide", "day2", 0): ("22a27d96c28fd96c", "three limits worth knowing before an incident"),
    ("guide", "day2", 1): ("1ca5e9968b5c4a90", "the two crons"),
}

DOC_NAMES = {id(contract_gen): "contract", id(deploy_gen): "guide"}


def _narration_digest(block) -> str:
    """Over the block's exact rendered content — lines AND table cells.

    Covering only `lines` would leave structural TABLES unpinned, and the contract's go-live
    checklist is exactly that: a table with no claim behind it whose every word is an obligation.
    """
    parts = [block.kind, *block.lines]
    for row in block.rows:
        parts.extend(row)
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()[:16]


def _narration(doc, name: str) -> dict:
    """Every unattributed, non-heading block, keyed by (document, section, ordinal)."""
    found, counter = {}, Counter()
    for section in doc.sections:
        for block in section.blocks:
            if block.claim_id is not None or block.kind == "heading":
                continue
            key = (name, section.section_id, counter[section.section_id])
            counter[section.section_id] += 1
            found[key] = block
    return found


def test_unattributed_prose_is_a_closed_pinned_set(rendered):
    generator, _registry, doc, _page, _tables = rendered
    name = DOC_NAMES[id(generator)]
    found = _narration(doc, name)
    listed = {k: v for k, v in NARRATION_LABELS.items() if k[0] == name}

    added = sorted(set(found) - set(listed))
    assert not added, (
        f"unattributed prose that nobody reviewed: {added}\n"
        "A block carrying no claim id publishes a fact nothing in the registry verifies. Either "
        "attach it to a claim, or add it to NARRATION_LABELS with its digest — which is the act "
        "of reviewing it."
    )
    removed = sorted(set(listed) - set(found))
    assert not removed, (
        f"NARRATION_LABELS still pins prose that is no longer rendered: {removed}. A stale entry "
        "silently widens the allowlist for whatever takes its place."
    )
    for key, block in found.items():
        pinned, label = listed[key]
        actual = _narration_digest(block)
        assert actual == pinned, (
            f"{key} ({label}) changed since it was reviewed.\n"
            f"  reviewed: {pinned}\n  now:      {actual}\n"
            f"  text:     {' '.join(block.lines)[:160]!r}\n"
            "Read the new text, then re-pin it in the SAME commit."
        )


def test_every_narration_entry_carries_a_human_label():
    """A digest alone tells the next reviewer nothing about what they are approving."""
    for key, (pinned, label) in NARRATION_LABELS.items():
        assert len(pinned) == 16 and re.fullmatch(r"[0-9a-f]{16}", pinned), key
        assert len(label.strip()) >= 12, f"{key} has no usable label"


def test_appending_unattributed_prose_fails_the_closed_set(tmp_path):
    """The F3 attack, run against this control: a contradictory paragraph that belongs to no
    claim. It used to need `exclusive_terms` to name its subject in advance; now it fails simply
    for being unlisted."""
    doc = _build(contract_gen)
    doc.section("smuggled", "Appendix")
    doc.p("We commit the decision before returning 2xx, so a duplicate cannot occur.")
    with pytest.raises(AssertionError, match="nobody reviewed"):
        test_unattributed_prose_is_a_closed_pinned_set(
            (contract_gen, WIRE, doc, "", []))


def test_editing_reviewed_prose_fails_the_pin(monkeypatch):
    """Rewriting narration in place keeps the key and changes the meaning — the mutation a
    key-only allowlist waves through."""
    doc = _build(deploy_gen)
    found = _narration(doc, "guide")
    key = ("guide", "releases", 0)
    original = found[key]
    tampered = dataclasses.replace(
        original,
        lines=("Rolling release: pull the tag and restart everything at once; no drain needed.",),
    )
    assert _narration_digest(tampered) != NARRATION_LABELS[key][0]


def test_the_page_uses_no_vocabulary_the_model_does_not_carry(rendered):
    """The other direction: text on the page that the model has never heard of.

    The pinned set above governs what the GENERATOR declares. This governs what the PAGE actually
    carries, so a renderer that draws its own words — a hardcoded string in a style, a stamped
    label, a template leaking through — is caught even though no block records it. Words are the
    unit because layout is not: reportlab wraps prose and pdfplumber interleaves table columns, so
    any longer span is an artifact of typesetting rather than of content.
    """
    generator, _registry, doc, page, _tables = rendered
    known = set()
    for section in doc.sections:
        known.update(_normalize(w) for w in section.title.split())
        for block in section.blocks:
            for line in block.lines:
                known.update(_normalize(w) for w in line.split())
            for row in block.rows:
                for cell in row:
                    known.update(_normalize(w) for w in cell.split())
    # Wrapping splits a token across two lines, so each half is a prefix or suffix of a known
    # word rather than a word. Accept only that, and only against the words actually present.
    unknown = []
    for word in page.split():
        needle = _normalize(word)
        if not needle or needle in known:
            continue
        if any(k.startswith(needle) or k.endswith(needle) for k in known):
            continue
        unknown.append(word)
    assert not unknown, (
        f"{DOC_NAMES[id(generator)]}: the page carries words the document model does not: "
        f"{sorted(set(unknown))[:20]}"
    )


# ── labels the renderer authors ───────────────────────────────────────────────────────────────
#
# `claim_paragraph(prefix=...)` frames a claim's value for the reader: "Compliance window (days):
# 2555". The value is the registry's and is checked against its authority; the LABEL is the
# renderer's and has no authority to be checked against. It was also, until this commit, not
# recorded in the block at all — so it reached the page while the model denied drawing it, and
# rewriting "(days)" to "(years)" published a false statement with the whole suite green.
#
# Recording it fixes the under-description. Pinning it here fixes the rest: a label that the model
# and the page agree on is still only the renderer agreeing with itself.







def test_a_reworded_label_fails_the_pin(monkeypatch, tmp_path):
    """The 'days' -> 'years' mutation, run end to end.

    Before the label was recorded this changed the page and nothing else — no model line moved, no
    claim value changed, and the value beside it stayed correct. It is the cheapest way to make an
    externally-binding document say something false.
    """
    from docs.generators import render as render_module

    original = render_module.Doc.claim_paragraph

    def relabelled(self, cid, *, prefix=""):
        if cid == "WIRE.RETENTION.WINDOW_DAYS":
            prefix = "<b>Compliance window (years): </b>"
        return original(self, cid, prefix=prefix)

    monkeypatch.setattr(render_module.Doc, "claim_paragraph", relabelled)
    doc = _build(contract_gen)
    path = str(tmp_path / "relabelled.pdf")
    doc.render(path)
    page = body_text(path)

    assert "Compliance window (years): 2555" in page, "the mutation did not reach the page"
    assert any("is framed" in problem for problem in outline.problems(
        doc, {"contact": SAMPLE_CONTACT}))


def test_the_vocabulary_check_catches_a_word_the_model_never_recorded(monkeypatch, tmp_path):
    """Negative control for the page->model direction: text drawn straight onto the page, with no
    block behind it at all. This is the shape a stamped label or a leaked template takes, and the
    model-side checks cannot see it because the model does not know it exists."""
    from docs.generators import render as render_module

    original = render_module.Doc.p

    def also_draw_unrecorded(self, markup):
        original(self, markup)
        # appended to the story, deliberately NOT to any block
        self._story.append(render_module.Paragraph(
            "Superseding addendum: zzyzx.", render_module.BODY))
        render_module.Doc.p = original  # once is enough

    monkeypatch.setattr(render_module.Doc, "p", also_draw_unrecorded)
    doc = _build(deploy_gen)
    monkeypatch.setattr(render_module.Doc, "p", original)
    path = str(tmp_path / "unrecorded.pdf")
    doc.render(path)
    page = body_text(path)

    assert "zzyzx" in page, "the unrecorded paragraph did not reach the page"
    with pytest.raises(AssertionError, match="does not"):
        test_the_page_uses_no_vocabulary_the_model_does_not_carry(
            (deploy_gen, OPERATIONS, doc, page, []))


# ── prose the renderer authors INSIDE a claim's block ─────────────────────────────────────────
#
# Self-audit of the two commits above. Pinning unattributed BLOCKS closed one door and left the
# adjacent one open: a block that carries a claim id is exempt from the narration pin, and
# `CLAIM_LABELS` only covers `claim_paragraph` prefixes. Everything a composed or tabular block
# says AROUND its claim's values — connective sentences, column headers, "Trap:", "Reversible?" —
# was renderer-authored, recorded (so the vocabulary check passes), attributed (so the narration
# pin skips it), and pinned by nothing.
#
# That is not a decorative surface. `WIRE.INGEST.EXTRA_FIELDS` promises a counterparty in prose
# that we "add without notice and never remove or repurpose one without a version bump agreed with
# you" — a binding forward-compatibility commitment with no claim behind it.
#
# So the rule becomes uniform, and it is the span-equality property finding 3 actually asked for:
# EVERY character on the page is either derived from the registry or pinned here.


# Below this, a block's residue is separators and numbering rather than words.








def test_rewriting_a_binding_commitment_in_connective_prose_is_caught(monkeypatch, tmp_path):
    """The finding, run as an attack.

    `WIRE.INGEST.EXTRA_FIELDS` promises we never repurpose a callback field without an agreed
    version bump. That sentence is the generator's, not the registry's, so before this pin it
    could be reversed outright: the claim's values still rendered, the block still carried its
    claim id, the vocabulary check still passed because every word already appears on the page.
    """
    from docs.generators import render as render_module

    original = render_module.Doc.claim_prose

    def weakened(self, cid, segments):
        if cid == "WIRE.INGEST.EXTRA_FIELDS":
            segments = [projection.Lit(s.text.replace(
                "never remove or repurpose one without a version bump agreed with you",
                "may remove or repurpose one at any time"))
                if type(s) is projection.Lit else s for s in segments]
        return original(self, cid, segments)

    monkeypatch.setattr(render_module.Doc, "claim_prose", weakened)
    doc = _build(contract_gen)

    block = next(b for b in doc.blocks if b.claim_id == "WIRE.INGEST.EXTRA_FIELDS")
    assert any("may remove or repurpose one at any time" in line for line in block.lines), (
        "the mutation did not land")
    assert any("composed template changed since it was reviewed" in problem
               for problem in outline.problems(
                   doc, {"contact": SAMPLE_CONTACT}))


def test_the_connective_residue_is_stable_under_overlapping_leaf_values(rendered):
    """`approve` is a substring of `approve_buy_locked`. If leaves were removed shortest-first the
    residue would keep `_buy_locked` and the digest would depend on iteration order rather than on
    what the renderer wrote."""
    _generator, registry, doc, _page, _tables = rendered
    for block in doc.blocks:
        if not block.claim_id or block.projection == projection.PARAGRAPH:
            continue
        claim = registry[block.claim_id]
        first = outline.connective_text(block, claim)
        assert outline.connective_text(block, claim) == first
        leaves = [x for x in projection.leaf_strings(claim.value, block.row_fields) if x]
        for leaf in leaves:
            assert leaf not in first, (
                f"{block.claim_id}: the claim value {leaf[:40]!r} survived into the residue, so "
                "the pin would cover registry-owned content and fail on a legitimate claim edit"
            )


# ── Wave-1 gate F10: the answer table may not contradict the resolution gate ──────────────────────
#
# Gate audit `6c4f54a..91fbde3` finding 10. The generator said answers are "listed below against
# the obligation each one clears" and headed a column "What it unblocks", while the claim beside
# it correctly said no reply can resolve anything until the signed artifact schema ships — the
# counterparty received mutually exclusive instructions.


def test_f10_gate_the_unresolvable_alert_precedes_the_answer_table():
    """The PENDING note (which carries the no-reply-can-resolve statement) must be VISIBLE before
    any answer row, and the deliverable column must be negated, not affirmative."""
    doc = _build(contract_gen)
    blocks = doc.blocks
    # the no-reply-can-resolve sentence is its own claim now (Wave-2 finding 1), so the
    # ordering is compared across the two claims rather than within one
    statement_at = next(
        i for i, b in enumerate(blocks) if b.claim_id == "WIRE.ORDERING.OBLIGATION_STATE")
    table_at = next(
        i for i, b in enumerate(blocks)
        if b.claim_id == "WIRE.ORDERING.PENDING_INPUTS" and b.kind == "table")
    assert statement_at < table_at, (
        "the resolution-impossible statement renders after the answer rows")
    header_row = blocks[table_at].rows[0]
    assert any("does not unblock" in cell for cell in header_row), header_row
    assert not any(cell == "What it unblocks" for cell in header_row), (
        "the affirmative header is back"
    )


def test_f10_gate_no_affirmative_clearing_language_in_the_asks_section():
    """The framing prose may not promise that an answer CLEARS an obligation while the gate says
    resolution is impossible; the screened-but-cannot-clear wording is the honest form."""
    doc = _build(contract_gen)
    asks = next(s for s in doc.sections if s.section_id == "asks")
    prose = " ".join(" ".join(b.lines) for b in asks.blocks)
    assert "each one clears" not in prose
    assert "screened" in prose and "cannot clear" in prose


def test_w7f1_a_false_registry_claim_cannot_be_published(tmp_path, monkeypatch):
    """Re-audit-6 finding 1, the exact witness, through the release path.

    The outline says the reviewed CLAIM is in the reviewed PLACE; it deliberately does not restate
    the claim's content, because the registry owns that. Nothing in the release path was checking
    that the registry still told the truth. Rewriting `WIRE.CALLBACK.RECEIVER_TXN` to return 2xx
    BEFORE committing made its verifier fail in the suite and the contract publish anyway — a
    false counterparty obligation with every outline block in the reviewed order.

    The verifiers and the receipt closure are production now (`docs.contracts.authority`), and the
    publication runs them, so the suite and the release check the same code.
    """
    from docs.contracts import authority

    monkeypatch.setattr(render_module, "source_revision", lambda: "abc1234")
    false = ("Verify the signature.",
             "Return 2xx before committing.",
             "Commit your own database transaction later.")
    with _swap_claim(WIRE, "WIRE.CALLBACK.RECEIVER_TXN", value=false):
        assert authority.problems(), "the authority lane must see the false claim"
        with pytest.raises(ValueError, match="running system contradicts"):
            contract_gen.PUBLICATION.publish(
                str(tmp_path), contact=SAMPLE_CONTACT)
    assert not list(tmp_path.iterdir()), "a refused release leaves nothing behind"


def test_w7f2_a_release_slot_is_a_reviewed_sentence_not_a_licence(tmp_path, monkeypatch):
    """Re-audit-6 finding 2. The contract's reply address and deadline vary per release, so the
    outline declares them as slots — but requiring only that the values appear SOMEWHERE let the
    front matter say the opposite instruction while carrying them:

        Do not send answers to <address> by <date>; this address and date are shown only for
        audit bookkeeping.

    A slot is a reviewed sentence with holes in it now, and the rendered line must be exactly that
    sentence filled with the values this release was given.
    """
    monkeypatch.setattr(render_module, "source_revision", lambda: "abc1234")
    real_why = render_module.Doc.why

    def inverted(self, text):
        if "Send answers" in text:
            text = (f"Do <b>not</b> send answers to <b>{SAMPLE_CONTACT}</b>; this address "
                    "is shown only for audit bookkeeping.")
        return real_why(self, text)

    monkeypatch.setattr(render_module.Doc, "why", inverted)
    with pytest.raises(ValueError, match="not the reviewed sentence"):
        contract_gen.PUBLICATION.publish(
            str(tmp_path), contact=SAMPLE_CONTACT)
    assert not list(tmp_path.iterdir())


def test_w7f3_text_drawn_outside_the_model_cannot_be_published(tmp_path, monkeypatch):
    """Re-audit-6 finding 3, the exact witness. The release verified the MODEL and promoted the
    PDF without reading it back, so a flowable appended to the renderer's private story after a
    correct model was built had no `Block` — the outline stayed clean and `Return 2xx before
    COMMIT.` appeared on the governed page. The staged file is compared to its model before it
    becomes an artifact."""
    monkeypatch.setattr(render_module, "source_revision", lambda: "abc1234")
    real_build = deploy_gen.build

    def sneaky(**kwargs):
        doc = real_build(**kwargs)
        doc._story.append(render_module.Paragraph("Return 2xx before COMMIT.", render_module.BODY))
        return doc

    monkeypatch.setattr(deploy_gen, "build", sneaky)
    with pytest.raises(AssertionError, match="prose is not exactly the model"):
        deploy_gen.PUBLICATION.publish(str(tmp_path))
    assert not list(tmp_path.iterdir()), "a refused release leaves nothing behind"


@contextlib.contextmanager
def _swap_claim(registry, claim_id, **changes):
    """One claim replaced in place for the duration — every holder sees the mutant."""
    original = registry.claims
    mutant = dataclasses.replace(registry[claim_id], **changes)
    object.__setattr__(
        registry, "claims", tuple(mutant if c.id == claim_id else c for c in original))
    try:
        yield
    finally:
        object.__setattr__(registry, "claims", original)


# ── the words the RENDERER puts inside a claimed block (re-audit-7 finding 1) ──────────────────
#
# `CLAIM_LABELS` and `RENDERER_PROSE` used to live here, and they were right: a claim id on a
# block makes its VALUES the registry's, and leaves the words drawn around them the generator's.
# What was wrong is that only this file read them. They are `BlockOutline.label` and
# `BlockOutline.residue` now — one home, inside the reviewed projection a release runs — and
# these tests attack that lane instead of a private copy of it.

@pytest.mark.parametrize(
    ("attr", "claim_id", "hostile", "refusal"),
    [("claim_paragraph", "WIRE.INGEST.PATH", "<b>Do not use this endpoint: </b>", "is framed"),
     ("claim_prose", "WIRE.CALLBACK.FIELDS",
      [projection.Lit("Required body fields: <b>case_id only</b>.")],
      "changed since it was reviewed")],
    ids=["paragraph-label", "composed-template"])
def test_w8f1_renderer_authored_text_inside_a_claim_cannot_be_published(
        attr, claim_id, hostile, refusal, tmp_path, monkeypatch):
    """Both of Codex's witnesses, through `publish`.

    A false frame around a true value is a false document: `Do not use this endpoint:` in front of
    the ingest path, and `Required body fields: case_id only.` in place of the real field list,
    both published with the registry lane, the outline lane and all four rendered lanes green —
    because none of them read the characters the renderer authors inside a claimed block. The
    composed case now fails as a TEMPLATE change: the reviewed template names a Ref to the field
    list, and the hostile all-literal template digests differently.
    """
    monkeypatch.setattr(render_module, "source_revision", lambda: "abc1234")
    real = getattr(render_module.Doc, attr)

    if attr == "claim_paragraph":
        def patched(self, cid, *, prefix=""):
            return real(self, cid, prefix=hostile if cid == claim_id else prefix)
    else:
        def patched(self, cid, segments):
            return real(self, cid, hostile if cid == claim_id else segments)

    monkeypatch.setattr(render_module.Doc, attr, patched)
    with pytest.raises(ValueError, match=refusal):
        contract_gen.PUBLICATION.publish(
            str(tmp_path), contact=SAMPLE_CONTACT)
    assert not list(tmp_path.iterdir()), "a refused release leaves nothing behind"


def test_w8f1_every_renderer_authored_span_inside_a_claim_is_reviewed(rendered):
    """Closed forward: a claimed block that starts saying something new around its values is a
    finding, not a silent addition — and every label/residue the outline pins is still drawn."""
    generator, _registry, doc, _page, _tables = rendered
    inputs = ({"contact": SAMPLE_CONTACT}
              if generator is contract_gen else {})
    assert outline.problems(doc, inputs) == []

    for section in doc.sections:
        for index, block in enumerate(section.blocks):
            if not block.claim_id:
                continue
            claim = doc.registry[block.claim_id]
            if block.projection == projection.PARAGRAPH:
                continue
            spec = outline.outline(doc.document_id).sections[
                [s.section_id for s in doc.sections].index(section.section_id)].blocks[index]
            if block.projection == projection.COMPOSED:
                # field-bound both ways: the reviewed template, and the lines it derives
                assert spec.composed == outline.residue_digest(
                    projection.serialize_composed(block.composed))
                assert tuple(block.lines) == projection.composed_lines(claim, block.composed)
                continue
            text = outline.connective_text(block, claim)
            if outline.is_mechanical(text, block.projection):
                continue
            assert spec.residue == outline.residue_digest(text), (
                f"{block.claim_id}: renderer prose reaching the page is not the reviewed prose")


def test_w9f1_a_short_renderer_sentence_inside_a_claim_cannot_be_published(tmp_path, monkeypatch):
    """Re-audit-8 finding 1, the exact witness, through `publish`.

    The residue lane exempted anything whose alphanumerics were shorter than six characters, on
    the theory that such a residue is punctuation. Length is the wrong question: the load-bearing
    words in an operational contract are the short ones. A first bullet reading `No 2xx` on
    `WIRE.CALLBACK.DELIVERY` normalized to `no2xx` — five characters — so the residue counted as
    empty, and the governed contract published a bullet flatly contradicting the four beneath it.

    Scaffolding is SYNTAX now: one `\\x00` per line, plus its own ordinal for the numbered
    projections, and nothing else. Anything a renderer adds is a sentence that needs review.
    """
    monkeypatch.setattr(render_module, "source_revision", lambda: "abc1234")
    real = render_module.Doc.claim_bullets

    def with_an_extra_bullet(self, claim_id, *args, **kwargs):
        if claim_id != "WIRE.CALLBACK.DELIVERY":
            return real(self, claim_id, *args, **kwargs)
        lines = ("No 2xx", *projection.expected_lines(
            self.registry[claim_id], projection.BULLETS))
        body = "<br/>".join("&bull;  " + render_module.escape(line) for line in lines)
        self._emit(claim_id, [render_module.Paragraph(body, render_module.BODY)], lines,
                   roles=("BODY",) * len(lines), projection_name=projection.BULLETS)
        return None

    monkeypatch.setattr(render_module.Doc, "claim_bullets", with_an_extra_bullet)
    with pytest.raises(ValueError, match="nobody reviewed"):
        contract_gen.PUBLICATION.publish(
            str(tmp_path), contact=SAMPLE_CONTACT)
    assert not list(tmp_path.iterdir()), "a refused release leaves nothing behind"


def test_w9f1_scaffolding_is_recognised_by_syntax_and_nothing_else():
    """Guard the guard: the rule accepts exactly the scaffolding each projection draws, and no
    word — however short — rides along inside it."""
    assert outline.is_mechanical("\x00\n\x00\n\x00", projection.BULLETS)
    assert outline.is_mechanical("1. \x00\n2. \x00\n3. \x00", projection.STEPS)
    assert not outline.is_mechanical("1. \x00\n2. \x00", projection.BULLETS), (
        "ordinals are scaffolding only where the projection numbers its lines")
    assert not outline.is_mechanical("3. \x00\n2. \x00", projection.STEPS), (
        "an ordinal that is not this line's own number is renderer text")
    for word in ("no", "not", "never", "only", "safe", "No 2xx", "-", ":"):
        assert not outline.is_mechanical(f"{word}\n\x00", projection.BULLETS), word
    # and the length floor it replaced is gone, not renamed: no live reference to it, and no
    # length test in the scaffolding decision. It survives only in the comment saying why.
    # (`_authored_problems` may measure lengths to NAME a mismatch; it may not use one to
    # excuse text from review — that is what `is_mechanical` alone decides.)
    assert not hasattr(outline, "CONNECTIVE_FLOOR")
    assert "len(" not in inspect.getsource(outline.is_mechanical), (
        "scaffolding must not depend on how long the text is")


def test_w10f1_one_claim_leaf_cannot_impersonate_another(tmp_path, monkeypatch):
    """Re-audit-9 finding 1, the exact witness, through `publish`.

    `WIRE.CALLBACK.RETRY` holds both attempts=8 and worst_case_minutes=27. The residue lane
    recovered provenance by SUBTRACTING every string equal to any claim leaf, so a rendered
    `27 attempts` — registry untouched — erased to the same residue as the honest text, and the
    governed contract published a false operational number with every production lane green.

    A composed block is typed now: the template's Refs name the path that fills each hole, and
    the release derives the text from template + REGISTRY. Both doors are closed — drawing text
    the template does not derive, and re-aiming the Ref at the sibling field.
    """
    monkeypatch.setattr(render_module, "source_revision", lambda: "abc1234")
    real = render_module.Doc.claim_mixed

    def tampered_lines(self, cid, parts, **kwargs):
        real(self, cid, parts, **kwargs)
        if cid == "WIRE.CALLBACK.RETRY":
            block = self.sections[-1].blocks[-1]
            self.sections[-1].blocks[-1] = dataclasses.replace(
                block, lines=tuple(line.replace("8 attempts", "27 attempts")
                                   for line in block.lines))
        return None

    monkeypatch.setattr(render_module.Doc, "claim_mixed", tampered_lines)
    with pytest.raises(ValueError, match="template does not derive"):
        contract_gen.PUBLICATION.publish(
            str(tmp_path), contact=SAMPLE_CONTACT)
    assert not list(tmp_path.iterdir())

    def reaimed_ref(self, cid, parts, **kwargs):
        if cid == "WIRE.CALLBACK.RETRY":
            parts = [(kind, tuple(
                projection.Ref("{worst_case_minutes}")
                if type(seg) is projection.Ref and seg.path == "{attempts}" else seg
                for seg in segments)) for kind, segments in parts]
        return real(self, cid, parts, **kwargs)

    monkeypatch.setattr(render_module.Doc, "claim_mixed", reaimed_ref)
    with pytest.raises(ValueError, match="composed template changed"):
        contract_gen.PUBLICATION.publish(
            str(tmp_path), contact=SAMPLE_CONTACT)
    assert not list(tmp_path.iterdir())


def test_w10f1_no_ref_in_any_composed_block_survives_a_sibling_swap(rendered):
    """The closed metamorphic half: in EVERY composed block, re-aiming EVERY Ref at every sibling
    path in its own container must be visible — as a template change always, and as a text change
    whenever the sibling's rendered form differs. A one-off retry check would leave the same
    class open in the other composed blocks."""
    _generator, registry, doc, _page, _tables = rendered
    inputs = {"contact": SAMPLE_CONTACT}
    swaps = checked = 0
    for section in doc.sections:
        for block in section.blocks:
            if block.projection != projection.COMPOSED:
                continue
            claim = registry[block.claim_id]
            honest = projection.composed_lines(claim, block.composed)
            honest_serial = projection.serialize_composed(block.composed)
            refs = [(pi, si, seg) for pi, (_k, segs) in enumerate(block.composed)
                    for si, seg in enumerate(segs) if type(seg) is projection.Ref]
            checked += len(refs)
            for pi, si, ref in refs:
                for sibling in _sibling_paths(claim.value, ref.path):
                    swapped = tuple(
                        (kind, tuple(
                            projection.Ref(sibling, seg.formatter)
                            if (i, j) == (pi, si) else seg
                            for j, seg in enumerate(segments)))
                        for i, (kind, segments) in enumerate(block.composed))
                    try:
                        rendered_lines = projection.composed_lines(claim, swapped)
                    except ValueError:
                        continue  # the sibling refuses this formatter — already visible
                    assert projection.serialize_composed(swapped) != honest_serial, (
                        f"{block.claim_id}: a re-aimed Ref serialized identically")
                    if rendered_lines != honest:
                        swaps += 1
                        # …and the release lane sees it: swap the block in and ask the outline
                        original = block.composed
                        object.__setattr__(block, "composed", swapped)
                        object.__setattr__(block, "lines", rendered_lines)
                        try:
                            found = outline.problems(doc, inputs)
                            assert any("composed template changed" in p for p in found), (
                                f"{block.claim_id}: sibling swap {ref.path} -> {sibling} "
                                "published")
                        finally:
                            object.__setattr__(block, "composed", original)
                            object.__setattr__(block, "lines", honest)
    # per-document floors: the contract's composed blocks carry ~30 refs, the guide's
    # procedures loop 18; a sweep that suddenly covers fewer has lost blocks, not gained safety
    assert checked >= 15, f"only {checked} refs swept; the proof went hollow"
    assert swaps >= 5, f"only {swaps} value-changing swaps exercised"


def _sibling_paths(value, path):
    """Every path addressing a DIFFERENT member of the same container `path` points into."""
    import re as _re

    if not path:
        return []
    head, _, _tail = path.rpartition(".") if "." in path else ("", "", path)
    last = _re.search(r"(\{[^{}]+\}|\[\d+\]|\.[A-Za-z_][A-Za-z0-9_]*)$", path)
    prefix = path[:last.start()]
    container = projection.resolve_path(value, prefix)
    out = []
    if isinstance(container, dict):
        out = [f"{prefix}{{{k}}}" for k in container]
    elif isinstance(container, (list, tuple)):
        out = [f"{prefix}[{i}]" for i in range(len(container))]
    elif dataclasses.is_dataclass(container):
        out = [f"{prefix}.{f.name}" for f in dataclasses.fields(container)]
    return [p for p in out if p != path]


def test_w11f1_the_composed_template_encoding_is_injective(tmp_path, monkeypatch):
    """Re-audit-10 finding 1, the exact witness, through `publish`.

    The first serializer concatenated with unescaped control characters, and `Lit` accepts every
    exact string — so one Lit whose text was the encoded suffix of the honest RETRY template
    serialized identically to the whole typed tree: same reviewed digest, no Ref anywhere, and the
    governed page printed serializer material (`R:{delays}:seconds_list ...`) in place of the
    registry's retry values. The encoding is canonical JSON of typed arrays now; a literal may
    carry any character, delimiters included, and cannot escape its position.
    """
    monkeypatch.setattr(render_module, "source_revision", lambda: "abc1234")
    real = render_module.Doc.claim_mixed

    def collide(self, cid, parts, **kwargs):
        if cid == "WIRE.CALLBACK.RETRY":
            honest = tuple((kind, tuple(segments)) for kind, segments in parts)
            serial = projection.serialize_composed(honest)
            fake = (("p", (projection.Lit(serial),)),)
            # the collision itself is dead: distinct trees encode distinctly
            assert projection.serialize_composed(fake) != serial
            parts = list(fake)
        return real(self, cid, parts, **kwargs)

    monkeypatch.setattr(render_module.Doc, "claim_mixed", collide)
    with pytest.raises(ValueError, match="composed template changed"):
        contract_gen.PUBLICATION.publish(
            str(tmp_path), contact=SAMPLE_CONTACT)
    assert not list(tmp_path.iterdir())

    # injectivity at the seams the old encoding leaked through: delimiter-bearing literals,
    # boundary ambiguity between one segment and two, and kind/text confusion
    distinct = [
        (("p", (projection.Lit("a\x1eb"),)),),
        (("p", (projection.Lit("a"), projection.Lit("\x1eb"))),),
        (("p", (projection.Lit("a"), projection.Lit("b"))),),
        (("p", (projection.Lit("ab"),)),),
        (("p", (projection.Lit('["p",[["L","x"]]]'),)),),
        (("p", (projection.Lit("L:x"),)),),
        (("p", (projection.Ref("{x}"),)),),
        (("why", (projection.Ref("{x}"),)),),
        (("p", (projection.Ref("{x}", "comma_list"),)),),
    ]
    encodings = [projection.serialize_composed(parts) for parts in distinct]
    assert len(set(encodings)) == len(encodings), "two distinct typed trees encoded identically"


def test_w11f2_invisible_ink_cannot_be_published(tmp_path, monkeypatch):
    """Re-audit-10 finding 2, the exact witness, through `publish` — via the central style
    constant, which is the door a closed role alone cannot shut.

    The retry obligation rendered in white passed every lane: extraction is colour-blind, so the
    outline, page/model, table, prose-stream and footer comparisons all saw the complete
    schedule while a person saw nothing. The glyph gate reads the ink itself.
    """
    monkeypatch.setattr(render_module, "source_revision", lambda: "abc1234")
    from reportlab.lib import colors as _colors

    monkeypatch.setattr(render_module.BODY, "textColor", _colors.white)
    with pytest.raises(ValueError, match="cannot see"):
        contract_gen.PUBLICATION.publish(
            str(tmp_path), contact=SAMPLE_CONTACT)
    assert not list(tmp_path.iterdir()), "a refused release leaves nothing behind"


@pytest.mark.parametrize("role", ["BODY", "WHY", "CODE", "CELL", "CELLB", "ALERT", "TOKEN",
                                  "STEP", "WRAPCODE", "H1", "H2"])
def test_w11f2_every_governed_role_is_held_to_visible_ink(role, tmp_path, monkeypatch):
    """Analogous coverage across every governed text role: whiten any ONE of them and the staged
    artifact refuses. Also the other two governed properties, spot-checked through BODY below."""
    from docs.generators import artifact
    from reportlab.lib import colors as _colors

    monkeypatch.setattr(getattr(render_module, role), "textColor", _colors.white)
    # a role may appear in only one of the two documents (ALERT and STEP are the guide's), so
    # whitening it must trip the gate on at least one of them
    tripped = []
    for generator in (contract_gen, deploy_gen):
        doc = (generator.build(contact=SAMPLE_CONTACT)
               if generator is contract_gen else generator.build())
        path = str(tmp_path / f"whitened-{generator.DOCUMENT_ID}.pdf")
        doc.render(path, revision="abc1234")
        tripped.extend(artifact.visibility_problems(path))
    assert tripped and any("invisible" in problem for problem in tripped), (
        f"white {role} text passed the glyph gate in both documents")


def test_w11f2_the_glyph_gate_holds_size_and_frame_too(tmp_path, monkeypatch):
    """A glyph can vanish by being microscopic as well as by being white."""
    from docs.generators import artifact

    monkeypatch.setattr(render_module, "source_revision", lambda: "abc1234")
    monkeypatch.setattr(render_module.BODY, "fontSize", 2)
    monkeypatch.setattr(render_module.BODY, "leading", 3)
    doc = deploy_gen.build()
    path = str(tmp_path / "tiny.pdf")
    doc.render(path, revision="abc1234")
    found = artifact.visibility_problems(path)
    assert found and "below" in found[0], "2pt text passed the glyph gate"


# The COMPLETE public rendering surface of `Doc`, each method with its exact content-only
# parameters (re-audit-11 finding 2). This is the closed-signature property itself, not a
# forbidden name: renaming an override parameter — `style`, `_role`, anything — adds a parameter
# and fails the exact tuple. Extending this table is the deliberate act of widening the surface.
PUBLIC_RENDERING_SIGNATURES = {
    "section": ("self", "section_id", "title"),
    "title": ("self",),
    "h1": ("self", "text"),
    "h2": ("self", "text"),
    "p": ("self", "markup"),
    "why": ("self", "markup"),
    "code": ("self", "text"),
    "wrapcode": ("self", "text"),
    "space": ("self", "height"),
    "keep_last_together": ("self", "count"),
    "claim_statement": ("self", "claim_id"),
    "claim_paragraph": ("self", "claim_id", "prefix"),
    "claim_bullets": ("self", "claim_id"),
    "claim_steps": ("self", "claim_id", "heading"),
    "claim_code": ("self", "claim_id", "heading", "numbered", "lead"),
    "claim_prose": ("self", "claim_id", "segments"),
    "claim_mixed": ("self", "claim_id", "parts", "published_fields"),
    "claim_alert": ("self", "claim_id"),
    "claim_table": ("self", "claim_id", "widths", "heading"),
    "table": ("self", "headers", "rows", "widths", "code_columns"),
}


def test_w11f2_presentation_is_closed_and_the_real_documents_are_visible(tmp_path, monkeypatch):
    """The public rendering surface is CLOSED — content parameters only, pinned exactly — and
    both real documents pass every ink gate they are now held to.

    Re-audit-11 finding 2 called the old form of this test out by name: asserting that no
    parameter is literally spelled `style` certifies a spelling, and `_role` walked straight
    past it. What is asserted now is the property: every public rendering method of `Doc` has
    exactly its documented content parameters, nothing variadic, so there is no parameter of
    ANY name through which a caller reaches a style — and each structural method records the
    one fixed role that is that method.
    """
    from docs.generators import artifact

    public = {name for name in vars(render_module.Doc)
              if not name.startswith("_") and callable(getattr(render_module.Doc, name))
              and name not in ("blocks", "story", "document_id", "section_of",
                               "expected_footers", "render")}
    assert public == set(PUBLIC_RENDERING_SIGNATURES), (
        "the public rendering surface changed; review the closed-signature table")
    for name, wanted in PUBLIC_RENDERING_SIGNATURES.items():
        signature = inspect.signature(getattr(render_module.Doc, name))
        assert tuple(signature.parameters) == wanted, (
            f"Doc.{name} takes {tuple(signature.parameters)}, reviewed as {wanted}")
        assert not any(p.kind in (p.VAR_KEYWORD, p.VAR_POSITIONAL)
                       for p in signature.parameters.values()), (
            f"Doc.{name} accepts variadic arguments")

    # each structural method records its ONE role — the behavioral half of "the role is the
    # method"
    probe = Doc(OPERATIONS, documents.DEPLOYMENT_GUIDE)
    probe.title()
    probe.h1("Heading one")
    probe.h2("Heading two")
    probe.p("Body prose")
    probe.why("The indented aside")
    probe.code("literal()")
    probe.wrapcode("wrap")
    assert [b.roles for b in probe.blocks] == [
        ("TITLE",), ("H1",), ("H2",), ("BODY",), ("WHY",), ("CODE",), ("WRAPCODE",)]

    monkeypatch.setattr(render_module, "source_revision", lambda: "abc1234")
    for generator in (contract_gen, deploy_gen):
        doc = (generator.build(contact=SAMPLE_CONTACT)
               if generator is contract_gen else generator.build())
        path = str(tmp_path / f"{generator.DOCUMENT_ID}.pdf")
        doc.render(path, revision="abc1234")
        assert artifact.visibility_problems(path) == []
        assert artifact.painted_problems(path) == []
        artifact.verify_role_ink(doc, path)


# ── re-audit-11 finding 1: the gate reads character metadata, not the final painted page ──────────
#
# Three independent witnesses, each of which passed `visibility_problems` and published on the
# prior tree: ink with alpha 0 still DECLARES black; black glyphs on a black table background
# declare nothing wrong; and an opaque shape painted over a finished page changes no character at
# all. The authority is the RASTERIZED page now — `artifact.painted_problems` renders each page
# the way a viewer does and requires every character's own box to show contrasting pixels — and
# `publish` refuses on any hit, before promotion, leaving nothing behind.


def test_w12f1_alpha_zero_ink_cannot_be_published(tmp_path, monkeypatch):
    """The exact witness: `BODY.textColor = Color(0, 0, 0, alpha=0)` writes an ExtGState with
    `/ca 0` — pdfplumber reports pure black, the declared-ink gate passes, and the rasterized
    page shows nothing where most of the body should be."""
    from reportlab.lib.colors import Color

    monkeypatch.setattr(render_module, "source_revision", lambda: "abc1234")
    monkeypatch.setattr(render_module.BODY, "textColor", Color(0, 0, 0, alpha=0))
    with pytest.raises(ValueError, match="cannot see"):
        contract_gen.PUBLICATION.publish(
            str(tmp_path), contact=SAMPLE_CONTACT)
    assert not list(tmp_path.iterdir()), "a refused release leaves nothing behind"


def test_w12f1_black_on_black_cannot_be_published(tmp_path, monkeypatch):
    """The exact witness: a black whole-table BACKGROUND appended to the central `_TABLE_STYLE`.
    Every table glyph still declares luminance 0 — ink as dark as governed ink gets — and every
    cell is unreadable, because the declared gate never asked what the ink sits ON."""
    from reportlab.lib import colors as _colors
    from reportlab.platypus import TableStyle

    monkeypatch.setattr(render_module, "source_revision", lambda: "abc1234")
    monkeypatch.setattr(
        render_module, "_TABLE_STYLE",
        TableStyle(list(render_module._TABLE_STYLE.getCommands())
                   + [("BACKGROUND", (0, 0), (-1, -1), _colors.black)]))
    with pytest.raises(ValueError, match="cannot see"):
        contract_gen.PUBLICATION.publish(
            str(tmp_path), contact=SAMPLE_CONTACT)
    assert not list(tmp_path.iterdir()), "a refused release leaves nothing behind"


def test_w12f1_an_overdrawn_page_cannot_be_published(tmp_path, monkeypatch):
    """The exact witness: one zero-height flowable appended to the private story whose `draw()`
    paints an opaque white rectangle over the finished page. No character changes — extraction
    and every model lane stay green — and the page is blank. Private-story injection is this
    project's own established threat model (re-audit-6 finding 3), so 'the renderer draws no
    opaque shapes' was never a boundary; the paint is measured now."""
    from reportlab.platypus import Flowable

    class Blanket(Flowable):
        width = 0
        height = 0

        def draw(self):
            self.canv.saveState()
            self.canv.setFillColorRGB(1, 1, 1)
            self.canv.rect(-1000, -1000, 3000, 3000, stroke=0, fill=1)
            self.canv.restoreState()

    monkeypatch.setattr(render_module, "source_revision", lambda: "abc1234")
    real_build = deploy_gen.build

    def overdrawn_build(**inputs):
        doc = real_build(**inputs)
        doc._story.append(Blanket())
        return doc

    monkeypatch.setattr(deploy_gen, "build", overdrawn_build)
    with pytest.raises(ValueError, match="cannot see"):
        deploy_gen.PUBLICATION.publish(str(tmp_path))
    assert not list(tmp_path.iterdir()), "a refused release leaves nothing behind"


# ── re-audit-11 finding 2: the presentation role is part of the reviewed block identity ───────────
#
# The witness: intercept the first audience call to `Doc.p` and draw it as a red ALERT panel.
# Three doors, all shut: the parameter no longer exists under any spelling (the closed-signature
# table above); the private emitter records the role it draws, so an honest wrapper is refused by
# the reviewed outline; and a wrapper that draws one role while RECORDING another is contradicted
# by the page itself — `verify_role_ink` walks the model and page prose streams in lockstep and
# holds each painted character to its line's recorded role.


def test_w12f2_audience_cannot_be_promoted_to_an_alert(tmp_path, monkeypatch):
    """Codex's exact witness through every remaining door, each refused through `publish`."""
    monkeypatch.setattr(render_module, "source_revision", lambda: "abc1234")
    probe = Doc(OPERATIONS, documents.DEPLOYMENT_GUIDE)
    with pytest.raises(TypeError):
        probe.p("plain prose", _role=render_module.ALERT)
    with pytest.raises(TypeError):
        probe.p("plain prose", style=render_module.ALERT)

    real_build = deploy_gen.build
    real_p = render_module.Doc.p

    def build_with(first_p):
        def wrapped(**inputs):
            state = {"first": True}

            def hostile_p(self, markup):
                if state["first"]:
                    state["first"] = False
                    return first_p(self, markup)
                return real_p(self, markup)

            render_module.Doc.p = hostile_p
            try:
                return real_build(**inputs)
            finally:
                render_module.Doc.p = real_p
        return wrapped

    # the honest door: the private emitter records the role it draws, and the outline refuses it
    def honest_alert(self, markup):
        return render_module.Doc._prose(self, markup, "ALERT")

    monkeypatch.setattr(deploy_gen, "build", build_with(honest_alert))
    out = tmp_path / "honest"
    out.mkdir()
    with pytest.raises(ValueError, match=r"presented in roles \('ALERT',\)"):
        deploy_gen.PUBLICATION.publish(str(out))
    assert not list(out.iterdir()), "a refused release leaves nothing behind"

    # the forging door: draw ALERT, record BODY — the outline believes the record, the PAGE does
    # not
    def forged_alert(self, markup):
        self._story.append(render_module.Paragraph(markup, render_module.ALERT))
        self._add(render_module.Block(
            kind="prose", claim_id=None,
            lines=(render_module.visible_text(markup),), roles=("BODY",)))

    monkeypatch.setattr(deploy_gen, "build", build_with(forged_alert))
    out = tmp_path / "forged"
    out.mkdir()
    with pytest.raises(AssertionError, match="not the look of 'BODY'"):
        deploy_gen.PUBLICATION.publish(str(out))
    assert not list(out.iterdir()), "a refused release leaves nothing behind"


def test_w12f2_a_heading_cannot_borrow_another_levels_look(tmp_path, monkeypatch):
    """The same class at the heading levels the finding named: title/H1/H2 all record `heading`,
    so an H2 drawn in H1's larger type was invisible to the model. Forging the record is
    contradicted by the painted size."""
    monkeypatch.setattr(render_module, "source_revision", lambda: "abc1234")
    real_build = deploy_gen.build
    real_h2 = render_module.Doc.h2

    def wrapped(**inputs):
        state = {"first": True}

        def promoted_h2(self, text):
            if state["first"]:
                state["first"] = False
                self._story.append(render_module.Paragraph(
                    render_module.escape(text), render_module.ROLE_STYLES["H1"]))
                self._add(render_module.Block(
                    kind="heading", claim_id=None, lines=(text,), roles=("H2",)))
                return None
            return real_h2(self, text)

        render_module.Doc.h2 = promoted_h2
        try:
            return real_build(**inputs)
        finally:
            render_module.Doc.h2 = real_h2

    monkeypatch.setattr(deploy_gen, "build", wrapped)
    with pytest.raises(AssertionError, match="not the look of 'H2'"):
        deploy_gen.PUBLICATION.publish(str(tmp_path))
    assert not list(tmp_path.iterdir()), "a refused release leaves nothing behind"


def test_w12f2_the_outline_reviews_every_blocks_roles():
    """Total on the reviewed side: every outline row that shows lines names its roles, drawn
    from the closed set — so no block's presentation is left unreviewed."""
    for entry in outline.OUTLINES.values():
        for section in entry.sections:
            for spec in section.blocks:
                if spec.kind != "table":
                    assert spec.roles, f"{entry.document_id}/{section.section_id}: unreviewed roles"
                assert set(spec.roles) <= set(render_module.ROLE_STYLES), (
                    f"{entry.document_id}/{section.section_id}: unknown roles {spec.roles}")


# ── re-audit-12: contribution, not contrast — and a floor that holds across environments ──────────


def test_w13f1_a_checkerboard_overpaint_cannot_be_published(tmp_path, monkeypatch):
    """Re-audit-12 finding 1, the exact witness: after each completed page is stamped, wipe it
    white and tile a 1pt checkerboard over it, through the same internal canvas hook
    `Doc.render` uses. Every character's box then holds both black and white pixels — maximal
    CONTRAST — while not one governed word is readable, and the prior painted gate passed it
    into a promoted artifact. Contribution refuses it: removing the text layer changes nothing
    a reader could see, so the text was never visible."""
    from PIL import Image
    from reportlab.lib.utils import ImageReader

    monkeypatch.setattr(render_module, "source_revision", lambda: "abc1234")
    checker = Image.new("L", (612, 792))
    checker.putdata([255 if (x + y) % 2 else 0 for y in range(792) for x in range(612)])
    tile = ImageReader(checker.convert("RGB"))
    real_maker = render_module._stamped_canvas

    def sabotaged_maker(identity, revision, page_sections=None):
        base = real_maker(identity, revision, page_sections)

        class Checkered(base):
            def drawRightString(self, x, y, text):
                super().drawRightString(x, y, text)
                # the page number is the last stamp on each finished page; paint after it
                self.saveState()
                self.setFillColorRGB(1, 1, 1)
                self.rect(-5, -5, 700, 900, stroke=0, fill=1)
                self.drawImage(tile, 0, 0, width=612, height=792)
                self.restoreState()

        return Checkered

    monkeypatch.setattr(render_module, "_stamped_canvas", sabotaged_maker)
    with pytest.raises(ValueError, match="cannot see"):
        deploy_gen.PUBLICATION.publish(str(tmp_path))
    assert not list(tmp_path.iterdir()), "a refused release leaves nothing behind"


def test_w13f2_the_real_documents_clear_the_painted_floor_with_margin(tmp_path, monkeypatch):
    """Re-audit-12 finding 2: a verdict that flips with the platform's rasterizer is not a
    gate — the unmodified document failed its own baseline on another machine, over an
    underscore's tight metric box. The measured quantity is the glyph's own contribution now,
    and the REAL documents must clear the release floor with the governed guard's headroom, so
    an environment drifting toward the floor fails HERE, loudly, while readers are still a full
    step away from a wrong refusal. The constants must actually enclose each other for that
    sentence to mean anything, so their ordering is asserted too."""
    from docs.generators import artifact

    # the palest governed ink is the #666666 footer: at full coverage it contributes 0.60
    palest = 0.60
    assert (artifact.MINIMUM_PAINTED_CONTRAST
            < artifact.MINIMUM_REAL_CONTRIBUTION
            < palest), "the guard must sit between the release floor and the palest full ink"

    monkeypatch.setattr(render_module, "source_revision", lambda: "abc1234")
    for generator in (contract_gen, deploy_gen):
        doc = (generator.build(contact=SAMPLE_CONTACT)
               if generator is contract_gen else generator.build())
        path = str(tmp_path / f"{generator.DOCUMENT_ID}.pdf")
        doc.render(path, revision="abc1234")
        weakest = min(artifact.text_contributions(path), key=lambda row: row[2])
        page, text, value = weakest
        assert value >= artifact.MINIMUM_REAL_CONTRIBUTION, (
            f"{generator.DOCUMENT_ID} page {page}: {text!r} contributes only {value:.2f} in "
            f"this environment — inside the guard band, drifting toward the release floor")
