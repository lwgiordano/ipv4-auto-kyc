"""What each claim MUST put on the page, derived from the claim alone.

Re-audit `4f23f23..122cc67` finding 3. The document model recorded the lines the RENDERER had just
computed, so the PDF was compared against an oracle the renderer wrote: editing `claim_paragraph`
to display something other than its claim changed the page and the expectation together, and the
suite stayed green.

So content derivation lives here, with no reportlab import and no knowledge of layout. The
renderer receives these lines and draws them; it does not decide them. The rendering tests call
these same functions DIRECTLY from the registry — not through the renderer — so a renderer that
displays anything else is compared against what the claim actually says and fails.

Projections are deliberately few and dumb. A projection that took a formatting callback would put
the renderer back in charge of the answer.
"""

import json
import re
from dataclasses import dataclass, fields, is_dataclass

# Every way a claim may be projected onto the page. Closed on purpose: a new presentation needs a
# new named projection here, which is a place a reviewer looks, rather than a new code path in the
# renderer, which is not.
PARAGRAPH = "paragraph"  # value is a scalar -> one line
BULLETS = "bullets"  # value is a sequence of strings -> one line each
STEPS = "steps"  # value is an ORDERED sequence -> numbered lines
CODE = "code"  # value is a sequence of literals -> one fixed-width line each
NUMBERED_CODE = "numbered_code"  # same, numbered, when the ORDER is the point
ALERT = "alert"  # value is a sequence of strings, rendered as a warning block
TABLE = "table"  # value is a sequence of row tuples/dataclasses
COMPOSED = "composed"  # the caller builds the rows; every leaf of the value must still appear

PROJECTIONS = frozenset(
    {PARAGRAPH, BULLETS, STEPS, CODE, NUMBERED_CODE, ALERT, TABLE, COMPOSED}
)


def leaf_strings(value, published_fields: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Every string a value carries, flattened, in order.

    Used for COMPOSED blocks, where the caller assembles rows from a claim plus its own connective
    words: the claim's own leaves must still be on the page even though the row layout is the
    caller's.

    `published_fields` restricts dataclass traversal to the attributes a block actually prints.
    Some fields exist to be CHECKED, not shown — `Procedure.playbook_digest` is the reviewed-body
    hash and `PendingInput.authority` is the internal spec reference — and requiring them on the
    page would be requiring the document to publish its own bookkeeping. A block must NAME what it
    publishes, so omitting a field is a declaration a reviewer can see rather than a silent gap.
    """
    if isinstance(value, bool):
        # A boolean's printed form is always a phrase — `jitter=False` is published as "no jitter",
        # `becomes_effective=True` as "YES". Requiring the literal "False" on the page would
        # require the document to print Python. The authority tests bind these flags to the code,
        # and dedicated rendering tests bind the phrase to the flag.
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, bytes):
        return (value.decode(),)
    if is_dataclass(value) and not isinstance(value, type):
        available = {f.name for f in fields(value)}
        unknown = set(published_fields) - available
        if unknown:
            raise ValueError(
                f"{type(value).__name__} has no field(s) {sorted(unknown)}; a published-field list "
                "that names nothing checks nothing"
            )
        # A dataclass may declare its published surface ONCE, at the type (`PUBLISHED_FIELDS`),
        # instead of at every call site. A checked-not-shown field (`Transition.when`,
        # `Procedure.playbook_digest`) is then invisible to every leaf consumer by default; an
        # explicit call-site list still wins, and still validates against the real fields.
        declared = getattr(type(value), "PUBLISHED_FIELDS", None)
        names = published_fields or declared or tuple(f.name for f in fields(value))
        out: list[str] = []
        for name in names:
            out.extend(leaf_strings(getattr(value, name), published_fields))
        return tuple(out)
    if isinstance(value, dict):
        # VALUES only. A dict's keys are the claim's internal field names — `base_seconds`,
        # `platform_to_tool` — and the document prints the values under its own labels. Requiring
        # the keys would require the page to publish the registry's variable names. Where keys ARE
        # published (the settings-defaults table prints them as KYC_ variables) a dedicated
        # rendering test asserts each one.
        out = []
        for item in value.values():
            out.extend(leaf_strings(item, published_fields))
        return tuple(out)
    if isinstance(value, (list, tuple, set, frozenset)):
        out = []
        for item in value:
            out.extend(leaf_strings(item, published_fields))
        return tuple(out)
    return (str(value),)


def expected_lines(claim, projection: str) -> tuple[str, ...]:
    """The exact text lines this claim must contribute, in order."""
    if projection not in PROJECTIONS:
        raise ValueError(f"unknown projection {projection!r}")
    if projection == PARAGRAPH:
        # A Statement publishes the sentence its fact SELECTED (Wave-2 audit finding 1); its
        # `text` is derived at construction, so there is no authored string here to project.
        if is_dataclass(claim.value) and not isinstance(claim.value, type):
            declared = getattr(type(claim.value), "PUBLISHED_FIELDS", ())
            if declared == ("text",):
                return (claim.value.text,)
        if isinstance(claim.value, (list, tuple, dict, set, frozenset)):
            raise TypeError(
                f"{claim.id} holds {type(claim.value).__name__}; a paragraph projection would "
                "publish its Python repr"
            )
        return (str(claim.value),)
    if projection in (BULLETS, CODE, ALERT):
        return tuple(str(item) for item in claim.value)
    if projection in (STEPS, NUMBERED_CODE):
        return tuple(f"{n}. {step}" for n, step in enumerate(claim.value, 1))
    if projection in (TABLE, COMPOSED):
        return ()  # tables are compared row-wise; see expected_rows
    raise AssertionError(projection)  # pragma: no cover — PROJECTIONS is closed


def expected_rows(claim, projection: str) -> tuple[tuple, ...]:
    """The body rows this claim must contribute, header row excluded.

    Which attribute lands in which column is the CLAIM's, taken from its own column schema
    (re-audit-3 finding 1). It used to be a `row_fields` argument threaded from the generator
    call and recorded on the Block the checks then read, so a caller could re-aim the projection
    and publish true values under true headers in false pairs. There is no argument to pass.
    """
    if projection != TABLE:
        return ()
    rows = []
    for row in claim.value:
        if is_dataclass(row) and not isinstance(row, type):
            names = claim.row_fields
            if not names:
                raise ValueError(
                    f"{claim.id}: {type(row).__name__} rows are projected by name, so every "
                    "column must declare the attribute beneath it"
                )
            rows.append(tuple(str(getattr(row, name)) for name in names))
        elif isinstance(row, (tuple, list)):
            rows.append(tuple(str(cell) for cell in row))
        else:
            rows.append((str(row),))
    return tuple(rows)


def expected_matrix(claim) -> tuple[tuple[str, ...], ...]:
    """The COMPLETE table this claim publishes: header row first, then every body row, in order.

    Wave 2 F5 (`4cb2cb7` finding 5): the previous comparison read tables back as bags of leaves,
    so reversed rows, swapped Effective?/Why values, and a condition moved to its neighbour all
    certified. The matrix is the unit of authority now — ordered, column-indexed, multiplicity
    included — and it is derived HERE, from the claim alone, so the renderer contributes no cell
    and no column title. A claim rendered as a table must declare its `columns`; every row must
    match the declared width exactly.
    """
    if not claim.columns:
        raise ValueError(
            f"{claim.id} is rendered as a table but declares no columns; the registry, "
            "not the renderer, owns a table's column titles and their display roles"
        )
    headers = tuple(str(cell) for cell in claim.headers)
    rows = expected_rows(claim, TABLE)
    for row in rows:
        if len(row) != len(headers):
            raise ValueError(
                f"{claim.id}: a row of width {len(row)} does not fit the declared "
                f"{len(headers)}-column header {headers}: {row}"
            )
    return (headers, *rows)


# ── the typed COMPOSED projection: field-bound occurrences, never string subtraction ──────────────
#
# Re-audit-9 finding 1. A composed block used to be a markup string the generator assembled from
# claim fields with f-strings, and the verifier recovered provenance afterwards by SUBTRACTING
# every string equal to any claim leaf from the finished text. Subtraction knows that some text
# equal to a leaf occurred; it does not know which FIELD supplied it. `WIRE.CALLBACK.RETRY`
# carries both attempts=8 and worst_case_minutes=27, so rewriting the rendered `8 attempts` to
# `27 attempts` — registry untouched — erased to the same residue, and the governed contract
# published a false operational number with every production lane green.
#
# A composed block is therefore TYPED now: an ordered sequence of parts, each part an ordered
# sequence of segments, each segment either reviewed literal markup (`Lit`) or an explicit claim
# field reference (`Ref`) naming its path and one of a CLOSED set of formatters. The renderer
# draws the join and nothing else; the release verifier recomputes the join from the REGISTRY and
# requires the block's visible lines to equal it, so the `attempts` slot provably rendered
# `value{attempts}` and not some other number that happens to live in the same claim.

_VISIBLE_TAG = re.compile(r"<[^>]+>")


def visible_markup_text(markup: str) -> str:
    """What a reader sees, given the mini-HTML handed to reportlab. `<b>` never reaches the
    page; entities do, decoded."""
    text = str(markup).replace("<br/>", "\n")
    text = _VISIBLE_TAG.sub("", text)
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _escape_markup(value: str) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@dataclass(frozen=True)
class Lit:
    """Reviewed literal markup — the renderer's words, pinned through the outline."""

    text: str

    def __post_init__(self) -> None:
        if type(self.text) is not str:
            raise ValueError("a literal segment is a string")


@dataclass(frozen=True)
class Ref:
    """One claim field: a path into the claim's value and a formatter from the closed set."""

    path: str
    formatter: str = "text"

    def __post_init__(self) -> None:
        if self.formatter not in COMPOSED_FORMATTERS:
            raise ValueError(
                f"{self.formatter!r} is not a composed formatter; the closed set is "
                f"{sorted(COMPOSED_FORMATTERS)}"
            )


# name -> (renders-escaped-markup?, callable). The escaped ones are legal only in prose parts,
# the raw ones only in code parts: markup in a code block and unescaped text in prose are both
# ways to draw something the template does not say.
COMPOSED_FORMATTERS = {
    "text": (True, lambda v: _escape_markup(v)),
    "comma_list": (True, lambda v: _escape_markup(", ".join(str(x) for x in v))),
    "seconds_list": (True, lambda v: _escape_markup(", ".join(f"{x}s" for x in v))),
    "enumerated": (True, lambda v: "  ".join(
        f"({n}) {_escape_markup(x)}" for n, x in enumerate(v, 1))),
    "raw": (False, lambda v: str(v)),
    "lines": (False, lambda v: "\n".join(str(x) for x in v)),
    "utf8": (False, lambda v: v.decode()),
}

_PROSE_KINDS = ("p", "why")
_RAW_KINDS = ("code", "atomic_code", "wrap")
_PATH_TOKEN = re.compile(r"\{([^{}]+)\}|\[(\d+)\]|\.([A-Za-z_][A-Za-z0-9_]*)")


def resolve_path(value, path: str):
    """The value at `path`: `{key}` mapping lookups, `[i]` sequence indices, `.attr` attributes,
    in any order; the empty path is the value itself. Refuses a path the value does not have —
    a reference that resolves to nothing renders nothing, silently, which is how a field goes
    missing from a contract."""
    if not path:
        return value
    consumed = 0
    for token in _PATH_TOKEN.finditer(path):
        if token.start() != consumed:
            raise ValueError(f"unparseable path {path!r} at offset {consumed}")
        consumed = token.end()
        key, index, attr = token.groups()
        try:
            if key is not None:
                value = value[key]
            elif index is not None:
                value = value[int(index)]
            else:
                value = getattr(value, attr)
        except (KeyError, IndexError, AttributeError, TypeError) as exc:
            raise ValueError(f"path {path!r} does not resolve on this claim value") from exc
    if consumed != len(path):
        raise ValueError(f"unparseable path {path!r} at offset {consumed}")
    return value


def composed_part_text(claim, kind: str, segments) -> str:
    """The exact text one part draws, derived from the claim and the reviewed template alone."""
    if kind not in (*_PROSE_KINDS, *_RAW_KINDS):
        raise ValueError(f"{claim.id}: unknown composed part kind {kind!r}")
    out = []
    for segment in segments:
        if type(segment) is Lit:
            out.append(segment.text)
            continue
        if type(segment) is not Ref:
            raise ValueError(
                f"{claim.id}: a composed segment is Lit or Ref, not {segment!r}")
        escaped, formatter = COMPOSED_FORMATTERS[segment.formatter]
        if escaped and kind in _RAW_KINDS:
            raise ValueError(
                f"{claim.id}: {segment.formatter!r} renders markup and this is a {kind} part")
        if not escaped and kind in _PROSE_KINDS:
            raise ValueError(
                f"{claim.id}: {segment.formatter!r} renders raw text into prose markup, which "
                "is how an unescaped value draws something the template does not say")
        resolved = resolve_path(claim.value, segment.path)
        try:
            out.append(formatter(resolved))
        except Exception as exc:  # noqa: BLE001 — a template naming a value its formatter
            # cannot render is a refusal, not a crash: the release reports it by name
            raise ValueError(
                f"{claim.id}: {segment.formatter!r} cannot render the value at "
                f"{segment.path!r} ({type(resolved).__name__})"
            ) from exc
    return "".join(out)


def composed_lines(claim, parts) -> tuple[str, ...]:
    """The exact visible lines a composed block must display, in order."""
    lines: list[str] = []
    for kind, segments in parts:
        text = composed_part_text(claim, kind, segments)
        lines.extend((text if kind in _RAW_KINDS else visible_markup_text(text)).split("\n"))
    return tuple(line for line in lines if line.strip())


def serialize_composed(parts) -> str:
    """A CANONICAL, INJECTIVE form of the template — what the outline digest pins.

    Re-audit-10 finding 1. The first encoding concatenated kinds and segments with unescaped
    control characters, and `Lit` accepts every exact string — so one Lit whose text was the
    serialized suffix of the honest template encoded identically to the whole typed tree. The
    reviewed digest matched, the template contained no Ref at all, and the governed page printed
    serializer material in place of the registry's retry values.

    JSON with sorted-key-free fixed shapes is injective over this structure: every segment is a
    typed array — `["L", text]` or `["R", path, formatter]` — and every part is
    `[kind, [segments...]]`. A literal may contain any character, delimiters included; it is a
    JSON string, so it cannot escape its position. Two distinct typed trees cannot encode
    identically, and a test proves the old witness now digests differently.
    """
    return json.dumps(
        [[kind, [["L", segment.text] if type(segment) is Lit
                 else ["R", segment.path, segment.formatter]
                 for segment in segments]]
         for kind, segments in parts],
        ensure_ascii=False, separators=(",", ":"))
