"""What each published document MUST contain, in order — the reviewed release projection.

Re-audit-5 finding 1. The publication bound module, identity, source registry and builder, and
then checked three LABELS on whatever the builder returned: its `document_id`, its identity
object, its registry object. None of those is the document. A builder that returns

    doc = Doc(OPERATIONS, documents.DEPLOYMENT_GUIDE)
    doc.title()
    doc.p("This arbitrary two-block body is not the reviewed deployment guide.")

passes all three, and published as the governed `techcraft-deployment-guide.pdf`: correct title,
correct metadata, correct footer, correct registry, one page, and every operational section a
reader needs simply absent. `_top_level_verify` passed it too — that helper executes every
registry fact and compares the page to the DOCUMENT'S OWN model, which a hollow document
satisfies trivially, and the publication command never ran it anyway.

So the shape of the document is authority, and it lives here rather than in the generator that
produces it or the test that reads it:

- the ORDER and identity of the sections, with their titles;
- every block, in order, by kind — and for a block whose content the registry owns, the claim it
  renders and the projection it renders under;
- for a block the registry does NOT own (narration a reader still acts on, headings), the digest
  of its exact lines, so a rewritten paragraph is a re-pin somebody reads;
- and, for the two spans that legitimately vary per release, the SLOT they fill: the contract's
  reply address and response deadline are release inputs, not registry values, so the outline
  requires the block to carry the values this release was given rather than fixing its text.

`problems()` is what a release runs. It is not a nicer `_top_level_verify`: that one asks whether
the page matches the document, and this one asks whether the document is the one we reviewed.
"""

import hashlib
import re
from dataclasses import dataclass
from types import MappingProxyType

from docs.contracts import projection


@dataclass(frozen=True)
class BlockOutline:
    """One block a published document must emit, in this position."""

    kind: str
    claim_id: str = ""
    projection: str = ""
    digest: str = ""
    slots: tuple[str, ...] = ()
    template: str = ""
    # What the RENDERER authors inside a claimed block (re-audit-7 finding 1). A claim
    # id on a block makes its VALUES the registry's; the words drawn around them stay
    # the generator's, and nothing in the release path was reading them. `label` is the
    # exact prefix a PARAGRAPH claim is framed with — pinned by wording, because
    # "Compliance window (days):" is short enough to review on sight and a digest of it
    # tells a reviewer nothing. `residue` is the digest of everything a composed block
    # says around its claim's values.
    label: str = ""
    residue: str = ""

    def __post_init__(self) -> None:
        if not self.kind:
            raise ValueError("a block outline names the kind of block it fixes")
        if not self.claim_id and (self.label or self.residue):
            raise ValueError(
                "only a claimed block has renderer-authored text INSIDE it; an's "
                "unattributed block is pinned whole by its digest"
            )
        if self.label and self.residue:
            raise ValueError(
                "a label is pinned by wording and a residue by digest; pinning one "
                "block both ways is two places to update"
            )
        if self.claim_id and (self.digest or self.slots):
            raise ValueError(
                f"{self.claim_id}: a claim block's content is the registry's; pinning its text "
                "here would be a second copy to drift"
            )
        if self.digest and self.slots:
            raise ValueError("a block either has fixed text or fills release slots, not both")
        if bool(self.slots) != bool(self.template):
            raise ValueError(
                "a slot block is a reviewed SENTENCE with holes in it, not a licence to say "
                "anything containing the values (re-audit-6 finding 2)"
            )
        for slot in self.slots:
            if "{" + slot + "}" not in self.template:
                raise ValueError(
                    f"the reviewed sentence has nowhere to put {slot!r}: {self.template!r}"
                )
        if not (self.claim_id or self.digest or self.slots):
            raise ValueError(
                f"a {self.kind!r} block with no claim, no digest and no slot fixes nothing"
            )


@dataclass(frozen=True)
class SectionOutline:
    section_id: str
    title: str
    blocks: tuple[BlockOutline, ...]

    def __post_init__(self) -> None:
        if not self.blocks:
            raise ValueError(f"{self.section_id}: a section a reader turns to is not empty")


@dataclass(frozen=True)
class DocumentOutline:
    document_id: str
    sections: tuple[SectionOutline, ...]


# The scaffolding a projection draws around its claim's values, as SYNTAX (re-audit-8 finding 1).
#
# This was `CONNECTIVE_FLOOR = 6`: a residue whose alphanumerics were shorter than that counted as
# punctuation and needed no review. Length is the wrong question — the load-bearing words in an
# operational contract are the short ones. Adding a first bullet reading `No 2xx` to
# `WIRE.CALLBACK.DELIVERY` normalized to `no2xx`, five characters, so the residue was treated as
# empty and the governed contract published a bullet flatly contradicting the four beneath it.
#
# A projection either draws scaffolding or it does not, and what it draws is known, not guessed:
# every leaf becomes `\x00`, one per line, and the numbered projections prefix each line with its
# own ordinal. Anything else in the residue is a renderer sentence and needs a reviewed pin.
_NUMBERED = (projection.STEPS, projection.NUMBERED_CODE)


def is_mechanical(text: str, projection_name: str) -> bool:
    """True when the residue is EXACTLY this projection's scaffolding and nothing else."""
    for index, line in enumerate(text.split("\n"), 1):
        if line == "\x00":
            continue
        if projection_name in _NUMBERED and line == f"{index}. \x00":
            continue
        return False
    return True


def connective_text(block, claim) -> str:
    """What the block draws, minus everything the CLAIM supplies.

    Longest leaf first: `approve` is a substring of `approve_buy_locked`, and removing the short
    one first would leave `_buy_locked` behind and make the residue depend on iteration order.
    """
    parts = list(block.lines)
    for row in block.rows:
        parts.extend(row)
    text = "\n".join(parts)
    # column titles are claim-supplied too (Claim.columns, Wave 2 F5), so they are not the
    # renderer's words any more than the cells are
    leaves = sorted(
        (leaf for leaf in (*projection.leaf_strings(claim.value, block.row_fields),
                           *claim.headers) if leaf),
        key=len, reverse=True,
    )
    for leaf in leaves:
        text = text.replace(leaf, "\x00")
    return re.sub(r"\x00+", "\x00", text)


def claim_label(block, claim) -> str:
    """The renderer-authored prefix framing a PARAGRAPH claim's value, or empty."""
    derived = projection.expected_lines(claim, block.projection)
    return " ".join(line for line in block.lines if line not in derived)


def residue_digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def block_digest(block) -> str:
    """The digest of a block's exact visible content — lines first, then table cells in order."""
    return hashlib.sha256(
        "\n".join([*block.lines, *(cell for row in block.rows for cell in row)]).encode()
    ).hexdigest()[:16]


# Generated from the reviewed documents and then REVIEWED: this is the projection a release is
# checked against, so an edit here is the deliberate act of approving a different document.
OUTLINES = MappingProxyType({
    'techcraft-integration-contract': DocumentOutline(
        document_id='techcraft-integration-contract',
        sections=(
            SectionOutline(
                section_id='(front matter)',
                title='',
                blocks=(
                    BlockOutline(kind='heading', digest='46b7d3b137603ddd'),
                    BlockOutline(kind='prose', digest='4e5032187099aa1e'),
                    BlockOutline(kind='heading', digest='9c870aa6e5e93270'),
                    BlockOutline(kind='prose', claim_id='WIRE.INGEST.ORDERING', projection='paragraph'),
                    BlockOutline(kind='prose', digest='56480299ea6e40e9'),
                    BlockOutline(
                        kind='prose', slots=('contact', 'due_date'),
                        template='Send answers to the section 1 questions to {contact} by {due_date}.'),
                ),
            ),
            SectionOutline(
                section_id='asks',
                title='1. Answers we need from you',
                blocks=(
                    BlockOutline(kind='heading', digest='234929f29ee78a30'),
                    BlockOutline(kind='prose', digest='ef84d6cefa3fd5e3'),
                    BlockOutline(
                        kind='prose', claim_id='WIRE.ORDERING.BOOTSTRAP_024', projection='paragraph'),
                    BlockOutline(kind='prose', digest='5d2918318bf4467c'),
                    BlockOutline(kind='prose', digest='c73a077c46f1732c'),
                    BlockOutline(
                        kind='prose', claim_id='WIRE.ORDERING.OBLIGATION_STATE', projection='paragraph'),
                    BlockOutline(kind='table', claim_id='WIRE.ORDERING.PENDING_INPUTS', projection='table'),
                    BlockOutline(kind='prose', digest='fd8093c1f89e10cd'),
                    BlockOutline(kind='prose', digest='96e74fb46a028241'),
                    BlockOutline(kind='prose', digest='d96e9efec3b326d1'),
                ),
            ),
            SectionOutline(
                section_id='events',
                title='2. Events you send us',
                blocks=(
                    BlockOutline(kind='heading', digest='e234571e96ca5ffd'),
                    BlockOutline(
                        kind='prose', claim_id='WIRE.INGEST.PATH', projection='paragraph',
                        label='Endpoint:'),
                    BlockOutline(kind='prose', digest='1ec5dfd87476be01'),
                    BlockOutline(kind='table', claim_id='WIRE.INGEST.HEADERS', projection='table'),
                    BlockOutline(kind='prose', digest='3231921901830b1c'),
                    BlockOutline(kind='code', digest='583787dccbe46194'),
                    BlockOutline(kind='heading', digest='0618c15aff85c825'),
                    BlockOutline(
                        kind='prose', claim_id='WIRE.INGEST.EXTRA_FIELDS', projection='composed',
                        residue='3dbd54f03ecebb08'),
                    BlockOutline(kind='heading', digest='90204440e7af4730'),
                    BlockOutline(
                        kind='prose', claim_id='WIRE.ACTOR.SENSITIVE', projection='composed',
                        residue='841eed537d25b0f4'),
                    BlockOutline(kind='table', claim_id='WIRE.EVENT.TABLE', projection='table'),
                    BlockOutline(kind='heading', digest='abe0d582168011a6'),
                    BlockOutline(kind='table', claim_id='WIRE.INGEST.STATUS', projection='table'),
                ),
            ),
            SectionOutline(
                section_id='callbacks',
                title='3. Callbacks we send you',
                blocks=(
                    BlockOutline(kind='heading', digest='52e82de2fbb9b393'),
                    BlockOutline(
                        kind='prose', claim_id='WIRE.CALLBACK.PATH', projection='paragraph',
                        label='Endpoint:'),
                    BlockOutline(kind='prose', digest='2196d0acbca869f7'),
                    BlockOutline(
                        kind='prose', claim_id='WIRE.CALLBACK.FIELDS', projection='composed',
                        residue='fd2b8fddb0219846'),
                    BlockOutline(
                        kind='prose', claim_id='WIRE.CALLBACK.DECISIONS', projection='composed',
                        residue='efd06031f9154b80'),
                    BlockOutline(
                        kind='prose', claim_id='WIRE.CALLBACK.GATES', projection='composed',
                        residue='60ff773c179a76ed'),
                    BlockOutline(
                        kind='prose', claim_id='WIRE.CALLBACK.OPTIONAL_FIELDS', projection='composed',
                        residue='adcaa34e853e35fc'),
                    BlockOutline(
                        kind='prose', claim_id='WIRE.CALLBACK.OPTIONAL_FIELD_RULE', projection='paragraph'),
                    BlockOutline(kind='heading', digest='ead72c5bc41be710'),
                    BlockOutline(kind='prose', claim_id='WIRE.CALLBACK.DELIVERY', projection='bullets'),
                    BlockOutline(kind='heading', digest='4c375c57c0bba056'),
                    BlockOutline(
                        kind='prose', claim_id='WIRE.CALLBACK.VALIDATION_ORDER', projection='paragraph'),
                    BlockOutline(kind='table', claim_id='WIRE.CALLBACK.VALIDATION', projection='table'),
                    BlockOutline(kind='heading', digest='7ff558b7c1f44148'),
                    BlockOutline(kind='prose', claim_id='WIRE.CALLBACK.RECEIVER_TXN', projection='steps'),
                    BlockOutline(
                        kind='prose', claim_id='WIRE.CALLBACK.ACK_CONSEQUENCE', projection='paragraph'),
                    BlockOutline(kind='heading', digest='3af9f2cc4371da07'),
                    BlockOutline(kind='prose', claim_id='WIRE.CALLBACK.ACK_VS_APPLY', projection='paragraph'),
                    BlockOutline(kind='table', claim_id='WIRE.CALLBACK.EFFECTIVENESS', projection='table'),
                    BlockOutline(kind='table', claim_id='WIRE.CALLBACK.LEGEND', projection='table'),
                    BlockOutline(
                        kind='prose', claim_id='WIRE.CALLBACK.LEGEND_CLOSURE', projection='paragraph'),
                    BlockOutline(kind='heading', digest='66dd2ae94a3b1d1c'),
                    BlockOutline(
                        kind='prose', claim_id='WIRE.CALLBACK.RELEASE_STATE', projection='paragraph'),
                    BlockOutline(kind='table', claim_id='WIRE.CALLBACK.RELEASE', projection='table'),
                    BlockOutline(kind='heading', digest='fc4e84255a41a3a2'),
                    BlockOutline(kind='prose', claim_id='WIRE.CALLBACK.WAIT_BOUND', projection='paragraph'),
                    BlockOutline(
                        kind='prose', claim_id='WIRE.CALLBACK.RETRY', projection='composed',
                        residue='e07949a7ee8f8121'),
                    BlockOutline(kind='prose', claim_id='WIRE.CALLBACK.COMPLETION', projection='paragraph'),
                ),
            ),
            SectionOutline(
                section_id='signing',
                title='4. Request signing (HMAC v2)',
                blocks=(
                    BlockOutline(kind='heading', digest='bedd5628058f3d03'),
                    BlockOutline(
                        kind='code', claim_id='WIRE.SIGN.CANONICAL', projection='numbered_code',
                        residue='44258b84c7d9f360'),
                    BlockOutline(
                        kind='prose', claim_id='WIRE.SIGN.DIRECTIONS', projection='composed',
                        residue='3db54252a0da969e'),
                    BlockOutline(kind='prose', claim_id='WIRE.SIGN.DIRECTION_FORM', projection='paragraph'),
                    BlockOutline(
                        kind='prose', claim_id='WIRE.SIGN.SKEW_SECONDS', projection='paragraph',
                        label='Skew window (seconds):'),
                    BlockOutline(kind='prose', digest='1241961d4e2b11e0'),
                    BlockOutline(
                        kind='prose', claim_id='WIRE.SIGN.V1_SUNSET', projection='paragraph',
                        label='On the v1 sunset dates:'),
                    BlockOutline(kind='heading', digest='0cf745782fb15a3f'),
                    BlockOutline(kind='prose', claim_id='WIRE.SIGN.ROTATION', projection='bullets'),
                    BlockOutline(kind='table', claim_id='WIRE.SIGN.ROTATION_RETIREMENT', projection='table'),
                    BlockOutline(kind='heading', digest='7b5a3ebfc10a96a0'),
                    BlockOutline(
                        kind='prose', claim_id='WIRE.SIGN.COMPANION', projection='composed',
                        residue='62bed443d66d463f'),
                    BlockOutline(kind='prose', claim_id='WIRE.SIGN.COMPANION_PROOF', projection='paragraph'),
                    BlockOutline(kind='heading', digest='c71fc742ef56a692'),
                    BlockOutline(
                        kind='prose', claim_id='WIRE.SIGN.VECTOR', projection='composed',
                        residue='723d2cc6883b5453'),
                ),
            ),
            SectionOutline(
                section_id='ordering',
                title='5. Ordering, and one field you must not sort by',
                blocks=(
                    BlockOutline(kind='heading', digest='eb8c92f5cfa81ddb'),
                    BlockOutline(
                        kind='prose', claim_id='WIRE.ORDERING.NO_DECIDED_AT', projection='paragraph'),
                    BlockOutline(kind='heading', digest='25e8007953ff51d7'),
                    BlockOutline(
                        kind='prose', claim_id='WIRE.ORDERING.SEQUENCE_DOMAINS', projection='bullets'),
                    BlockOutline(
                        kind='prose', claim_id='WIRE.ORDERING.ORDINAL_AUTHORITY', projection='paragraph'),
                    BlockOutline(
                        kind='prose', claim_id='WIRE.ORDERING.INTERIM', projection='paragraph',
                        label='Until activation:'),
                    BlockOutline(
                        kind='prose', claim_id='WIRE.ORDERING.INTEGRITY_MISMATCH', projection='paragraph',
                        label='Note:'),
                ),
            ),
            SectionOutline(
                section_id='retention',
                title='6. Retention',
                blocks=(
                    BlockOutline(kind='heading', digest='62d82fda7569512c'),
                    BlockOutline(
                        kind='prose', claim_id='WIRE.RETENTION.WINDOW_DAYS', projection='paragraph',
                        label='Compliance window (days):'),
                    BlockOutline(kind='table', claim_id='WIRE.RETENTION.BY_KIND', projection='table'),
                ),
            ),
            SectionOutline(
                section_id='checklist',
                title='7. Go-live checklist',
                blocks=(
                    BlockOutline(kind='heading', digest='477a774ed5761ee6'),
                    BlockOutline(kind='table', digest='7662ba50bcac0fc4'),
                ),
            ),
        ),
    ),
    'techcraft-deployment-guide': DocumentOutline(
        document_id='techcraft-deployment-guide',
        sections=(
            SectionOutline(
                section_id='(front matter)',
                title='',
                blocks=(
                    BlockOutline(kind='heading', digest='56ebbb485ec76c87'),
                    BlockOutline(kind='prose', digest='b9b57ddb8bd71d8a'),
                ),
            ),
            SectionOutline(
                section_id='blocker',
                title='Read this before provisioning anything',
                blocks=(
                    BlockOutline(kind='heading', digest='53d619cb818daa55'),
                    BlockOutline(
                        kind='alert', claim_id='OPS.BLOCKER.PRODUCTION_PROVIDERS', projection='alert'),
                    BlockOutline(kind='prose', digest='7cc3ea2a09c0f296'),
                    BlockOutline(kind='prose', digest='ef3453c590155e50'),
                    BlockOutline(kind='prose', digest='37fb3a031f041fca'),
                ),
            ),
            SectionOutline(
                section_id='processes',
                title='1. What you run: one image, six commands',
                blocks=(
                    BlockOutline(kind='heading', digest='99ecc044967d83b7'),
                    BlockOutline(kind='prose', digest='a0fc83c1a80bf8d9'),
                    BlockOutline(kind='table', claim_id='OPS.PROCESS.COMMANDS', projection='table'),
                    BlockOutline(kind='prose', digest='9434d1248b71a3c4'),
                    BlockOutline(
                        kind='prose', claim_id='OPS.PROCESS.DEV_WORKER_BANNED', projection='paragraph'),
                ),
            ),
            SectionOutline(
                section_id='infrastructure',
                title='2. Infrastructure',
                blocks=(
                    BlockOutline(kind='heading', digest='68357a0210721dc9'),
                    BlockOutline(kind='table', claim_id='OPS.INFRA.COMPONENTS', projection='table'),
                ),
            ),
            SectionOutline(
                section_id='configuration',
                title='3. Configuration (KYC_ prefix)',
                blocks=(
                    BlockOutline(kind='heading', digest='9c01b97bf70297ab'),
                    BlockOutline(kind='prose', digest='b0d455ec7d8152cc'),
                    BlockOutline(kind='table', claim_id='OPS.CONFIG.DEFAULTS', projection='table'),
                    BlockOutline(kind='heading', digest='38087bd9789a9520'),
                    BlockOutline(kind='prose', claim_id='OPS.CONFIG.PRODUCTION_FLOORS', projection='bullets'),
                    BlockOutline(
                        kind='code', claim_id='OPS.CONFIG.HMAC_SET', projection='code',
                        residue='eeead37d4b6834fd'),
                    BlockOutline(kind='prose', claim_id='OPS.CONFIG.HMAC_SET_RULE', projection='paragraph'),
                    BlockOutline(
                        kind='prose', claim_id='OPS.CONFIG.ROTATION_KEYS', projection='paragraph',
                        label='Rotation keys:'),
                    BlockOutline(kind='heading', digest='3c791b013d2d89d0'),
                    BlockOutline(kind='prose', claim_id='OPS.CONFIG.M2_GATE', projection='paragraph'),
                ),
            ),
            SectionOutline(
                section_id='health',
                title='4. Health and monitoring',
                blocks=(
                    BlockOutline(kind='heading', digest='b24a5ad3c1e5a9f7'),
                    BlockOutline(kind='table', claim_id='OPS.HEALTH.PROBES', projection='table'),
                ),
            ),
            SectionOutline(
                section_id='releases',
                title='5. Releases and cutovers',
                blocks=(
                    BlockOutline(kind='heading', digest='d6987735ca6011a2'),
                    BlockOutline(kind='prose', claim_id='OPS.RELEASE.CLASSIFICATION', projection='paragraph'),
                    BlockOutline(kind='prose', digest='e25872fbd22d85fd'),
                    BlockOutline(kind='heading', digest='cb798950e6c2011e'),
                    BlockOutline(
                        kind='prose', claim_id='OPS.CUTOVER.EXECUTION_SOURCE', projection='paragraph'),
                    BlockOutline(
                        kind='prose', claim_id='OPS.CUTOVER.PROCEDURES', projection='composed',
                        residue='33b3260daf6c3302'),
                    BlockOutline(
                        kind='prose', claim_id='OPS.CUTOVER.OUTBOX_CEILING', projection='steps',
                        residue='1193b2813e8d6c5b'),
                    BlockOutline(kind='prose', claim_id='OPS.CUTOVER.CEILING_RULE', projection='paragraph'),
                    BlockOutline(
                        kind='prose', claim_id='OPS.HMAC.ROLLOUT_ORDER', projection='steps',
                        residue='b533fa723b3b1f48'),
                    BlockOutline(kind='prose', claim_id='OPS.HMAC.V1_DROP_TIMING', projection='paragraph'),
                ),
            ),
            SectionOutline(
                section_id='rollback',
                title='6. Rollback',
                blocks=(
                    BlockOutline(kind='heading', digest='6caf7d4780aff6d8'),
                    BlockOutline(
                        kind='prose', claim_id='OPS.ROLLBACK.MIGRATION_BOUNDARY', projection='bullets'),
                ),
            ),
            SectionOutline(
                section_id='day2',
                title='7. Day-2 operations',
                blocks=(
                    BlockOutline(kind='heading', digest='9f138db6ef8c5dab'),
                    BlockOutline(kind='prose', claim_id='OPS.RECOVERY.REQUEUE', projection='paragraph'),
                    BlockOutline(kind='prose', digest='e1ac3e1b791231fe'),
                    BlockOutline(kind='prose', digest='08111d7054a86a86'),
                ),
            ),
        ),
    ),
})

def outline(document_id: str) -> DocumentOutline:
    try:
        return OUTLINES[document_id]
    except KeyError:
        raise KeyError(
            f"no release outline for {document_id!r}; a document with no reviewed projection "
            "cannot be published"
        ) from None


def problems(doc, inputs: dict | None = None) -> list[str]:
    """Every way `doc` is not the document its outline describes. Empty means publishable.

    Ordered and total on both sides: a missing section, an extra one, a reordered pair, a block
    of the wrong kind, a claim rendered under a different projection, an edited narration
    paragraph, and a release slot that does not carry the value this release was given are all
    findings. A hollow body fails at the first section.
    """
    found: list[str] = []
    wanted = outline(doc.document_id)
    inputs = inputs or {}
    got_ids = [section.section_id for section in doc.sections]
    want_ids = [section.section_id for section in wanted.sections]
    if got_ids != want_ids:
        return [f"sections are not the reviewed document's: expected {want_ids}, got {got_ids}"]
    for want, got in zip(wanted.sections, doc.sections, strict=True):
        where = want.section_id
        if got.title != want.title:
            found.append(f"{where}: title is {got.title!r}, reviewed as {want.title!r}")
        if len(got.blocks) != len(want.blocks):
            found.append(
                f"{where}: {len(got.blocks)} blocks, reviewed with {len(want.blocks)}")
            continue
        for index, (spec, block) in enumerate(zip(want.blocks, got.blocks, strict=True)):
            at = f"{where}[{index}]"
            if block.kind != spec.kind:
                found.append(f"{at}: {block.kind!r} block, reviewed as {spec.kind!r}")
                continue
            if spec.claim_id:
                if block.claim_id != spec.claim_id:
                    found.append(
                        f"{at}: renders {block.claim_id!r}, reviewed as {spec.claim_id!r}")
                elif spec.projection and block.projection != spec.projection:
                    found.append(
                        f"{at}: {spec.claim_id} rendered as {block.projection!r}, reviewed as "
                        f"{spec.projection!r}")
                else:
                    found.extend(_authored_problems(at, spec, block, doc.registry[block.claim_id]))
                continue
            if block.claim_id:
                found.append(
                    f"{at}: renders {block.claim_id!r}, reviewed as unattributed {spec.kind}")
                continue
            if spec.slots:
                text = "\n".join([*block.lines, *(c for r in block.rows for c in r)])
                missing = [s for s in spec.slots if s not in inputs]
                if missing:
                    found.append(f"{at}: this release supplied no {missing!r}")
                    continue
                # the REVIEWED SENTENCE with this release's values in it, and nothing else
                # (re-audit-6 finding 2). Requiring only that the values appear SOMEWHERE let
                # the front matter say "Do not send answers to <address> by <date>; this
                # address and date are shown only for audit bookkeeping" and publish.
                wanted = spec.template.format(**{s: inputs[s] for s in spec.slots})
                if text != wanted:
                    found.append(
                        f"{at}: is not the reviewed sentence with this release's values in "
                        f"it.\n      reviewed: {wanted!r}\n      rendered: {text!r}")
                continue
            if block_digest(block) != spec.digest:
                found.append(
                    f"{at}: text changed since it was reviewed (reviewed {spec.digest}, now "
                    f"{block_digest(block)}). Read it, then re-pin it in the SAME commit: "
                    f"{(block.lines or ('',))[0][:60]!r}")
    return found


def _authored_problems(at, spec, block, claim) -> list[str]:
    """The words the RENDERER puts inside a claimed block, against what was reviewed.

    Re-audit-7 finding 1: `authority.problems()` covers the registry, the outline covered the
    claim's PLACE, and the rendered lanes compare the page to the model — but the model's own
    renderer-authored lines were reviewed only by the test suite. A label rewritten to
    `Do not use this endpoint:` and a composed lead-in rewritten to `Required body fields:
    case_id only.` both published, every production lane green.
    """
    if block.projection == projection.PARAGRAPH:
        drawn = claim_label(block, claim)
        if drawn != spec.label:
            return [f"{at}: {claim.id} is framed {drawn!r}, reviewed as {spec.label!r}"]
        return []
    text = connective_text(block, claim)
    drawn = "" if is_mechanical(text, block.projection) else residue_digest(text)
    if drawn != spec.residue:
        if not spec.residue:
            return [f"{at}: {claim.id} now says something around its values that nobody reviewed: "
                    f"{' '.join(text.replace(chr(0), '~').split())[:120]!r}"]
        return [f"{at}: the words {claim.id} is wrapped in changed since they were reviewed "
                f"(reviewed {spec.residue}, now {drawn or 'nothing'}): "
                f"{' '.join(text.replace(chr(0), '~').split())[:120]!r}"]
    return []
