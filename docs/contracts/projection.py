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

from dataclasses import fields, is_dataclass

# Every way a claim may be projected onto the page. Closed on purpose: a new presentation needs a
# new named projection here, which is a place a reviewer looks, rather than a new code path in the
# renderer, which is not.
PARAGRAPH = "paragraph"  # value is a scalar -> one line
BULLETS = "bullets"  # value is a sequence of strings -> one line each
STEPS = "steps"  # value is an ORDERED sequence -> numbered lines
CODE = "code"  # value is a sequence of literals -> one fixed-width line each
NUMBERED_CODE = "numbered_code"  # same, numbered, when the ORDER is the point
ALERT = "alert"  # value is a sequence of strings, rendered as a warning block
NOTE = "note"  # the claim's note, not its value
TABLE = "table"  # value is a sequence of row tuples/dataclasses
COMPOSED = "composed"  # the caller builds the rows; every leaf of the value must still appear

PROJECTIONS = frozenset(
    {PARAGRAPH, BULLETS, STEPS, CODE, NUMBERED_CODE, ALERT, NOTE, TABLE, COMPOSED}
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
    if projection == NOTE:
        if not claim.note.strip():
            raise ValueError(f"{claim.id} has no note to project")
        return (claim.note,)
    if projection == PARAGRAPH:
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


def expected_rows(claim, projection: str, row_fields: tuple[str, ...] = ()) -> tuple[tuple, ...]:
    """The body rows this claim must contribute, header row excluded.

    `row_fields` names the dataclass attributes to project, in column order, when the claim's rows
    are dataclasses rather than tuples.
    """
    if projection != TABLE:
        return ()
    rows = []
    for row in claim.value:
        if is_dataclass(row) and not isinstance(row, type):
            rows.append(tuple(str(getattr(row, name)) for name in row_fields))
        elif isinstance(row, (tuple, list)):
            rows.append(tuple(str(cell) for cell in row))
        else:
            rows.append((str(row),))
    return tuple(rows)
