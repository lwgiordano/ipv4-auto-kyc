"""Typed rollback contracts for referenced operational procedures.

Re-audit `4f23f23..122cc67` finding 5, second pass. The first pass gave `Procedure` a
`playbook_digest`, which binds the reviewed Markdown body — but `rollback` stayed a hand-written
tuple of prose sitting BESIDE that digest. Nothing tied the two together, so editing the tuple to
say "rollback means redeploying the previous image" left the digest matching and the suite green,
while the document told an operator to do the exact thing the playbook prohibits.

Three things change here.

1. **The question set is closed and TOTAL.** Every procedure answers every rollback question, so a
   procedure cannot stay silent on a control. That is not hypothetical: the previous prose gave PR
   5b four rollback lines, bundle-pinning two, and PR 7b-core three, and NONE of them published
   whether the procedure must end resumed — the single control PR 7b's playbook puts in capitals
   ("never leave the system stopped or retention frozen"). Totality found that gap; no reviewer had.

2. **Answers come from a closed domain, and the published sentence belongs to the ANSWER, not to
   the author.** `statements()` derives the prose. There is no free-text rollback field left to
   mutate, which is what made the original attack work.

3. **Every answer quotes the playbook.** `evidence` must appear verbatim inside the digest-bound
   body (`tests/unit/test_contract_registry_authority.py` checks this against the file). So an
   answer must point at the sentence it came from, and the digest proves that sentence was read.

What this does NOT do, stated plainly because the distinction is the whole finding: quoting the
playbook proves the playbook ADDRESSES the question, not that it answers it the way the contract
says. A verbatim quote cannot be machine-read for agreement. The answers' correctness rests on the
three checks that ARE mechanical — the structural invariants below, the migration-derived schema
answer (`schema` is computed from the downgrade ASTs and compared, never trusted), and the reviewed
digest — plus the evidence quote making a wrong answer visible to a human reader in one glance,
side by side with the playbook's own words on the page.
"""

from dataclasses import dataclass, field

# ── the closed question set ───────────────────────────────────────────────────────────────────
# Each is a question an operator must be able to answer BEFORE starting, because getting it wrong
# during an incident is unrecoverable. Adding a question here forces every procedure to answer it.
REVERSIBILITY = "reversibility"
SCHEMA = "schema"
IMAGE = "image"
RESTORES = "restores"
VERIFICATION = "verification"
WINDOW = "window"
ENDING = "ending"

ROLLBACK_QUESTIONS = (REVERSIBILITY, SCHEMA, IMAGE, RESTORES, VERIFICATION, WINDOW, ENDING)

QUESTION_PROMPTS = {
    REVERSIBILITY: "Can it be undone at all?",
    SCHEMA: "Does the schema move?",
    IMAGE: "Which image do you land on?",
    RESTORES: "Does rolling back restore something this release closed?",
    VERIFICATION: "What may you run to verify it?",
    WINDOW: "Does it need the same window as the forward cutover?",
    ENDING: "What state must you end in?",
}

# ── closed answer domains ─────────────────────────────────────────────────────────────────────
# The published sentence belongs to the answer. A procedure selects; it does not write.
REVERSIBLE_PLAINLY = "plainly"
REVERSIBLE_WITH_CONDITIONS = "with_conditions"
IRREVERSIBLE_PAST_BOUNDARY = "irreversible_past_a_boundary"

SCHEMA_STAYS = "stays"
SCHEMA_FROZEN = "frozen"
SCHEMA_CONDITIONAL = "conditional"
SCHEMA_DOWNGRADES = "downgrades"

IMAGE_PRIOR = "prior"
IMAGE_SAME_RELEASE = "same_release"
IMAGE_CONDITIONAL = "conditional"

RESTORES_YES = "yes"
RESTORES_NO = "no"

VERIFY_NON_MUTATING = "non_mutating_only"
VERIFY_UNRESTRICTED = "unrestricted"
VERIFY_NOT_STATED = "not_stated"

WINDOW_SAME = "same"
WINDOW_LIGHTER = "lighter"

ENDING_RESUMED = "resumed"
ENDING_MAY_STOP = "may_remain_stopped"
ENDING_RESUMED_OR_DECLARED_INCIDENT = "resumed_or_declared_incident"

# Branch-level schema results (Wave 1 / F9). A BRANCH is a downgrade outcome already resolved, so
# its schema answer is a result, not a possibility — the aggregate CONDITIONAL answer names the
# fork, the branch answers name each side of it. Aggregate facts may not use these, and branch
# facts may use nothing else for the schema question.
BR_SCHEMA_HELD = "held"
BR_SCHEMA_WALKED = "walked"
BRANCH_SCHEMA_ANSWERS = frozenset({BR_SCHEMA_HELD, BR_SCHEMA_WALKED})

OUTCOME_REFUSED = "downgrade_refused"
OUTCOME_SUCCEEDED = "downgrade_succeeded"
BRANCH_OUTCOMES = (OUTCOME_REFUSED, OUTCOME_SUCCEEDED)

# The verbatim playbook marker that opens each outcome's span — a CLOSED map (gate audit
# `6c4f54a..91fbde3` finding 7). When branches authored their own markers, pointing the SUCCEEDED
# branch at R5 made both spans begin at the refusal marker, so the success branch could cite the
# refusal branch's evidence. Deriving the marker from the outcome removes the slot; the verifier
# additionally requires each marker exactly once in the section, in BRANCH_OUTCOMES order.
OUTCOME_MARKERS = {
    OUTCOME_REFUSED: "R5. ROLLBACK OUTCOME A",
    OUTCOME_SUCCEEDED: "R6. ROLLBACK OUTCOME B",
}
OUTCOME_TITLES = {
    OUTCOME_REFUSED: "If the downgrade is REFUSED (outcome A)",
    OUTCOME_SUCCEEDED: "If the downgrade SUCCEEDS all the way to the base (outcome B)",
}

BRANCH_QUESTIONS = (SCHEMA, IMAGE, RESTORES, VERIFICATION, ENDING)

ANSWERS: dict[str, dict[str, str]] = {
    REVERSIBILITY: {
        REVERSIBLE_PLAINLY: "Yes — redeploy the previous image and you are back.",
        REVERSIBLE_WITH_CONDITIONS: "Yes, but NOT by a plain redeploy. The conditions below are "
                                    "part of the procedure, not caveats on it; a plain redeploy "
                                    "here does damage the procedure exists to avoid.",
        IRREVERSIBLE_PAST_BOUNDARY: "NOT past the boundary this cutover crosses. Once it is "
                                    "crossed, 'rollback' means landing on a compatible image "
                                    "against the schema you are already on — the schema itself "
                                    "does not come back.",
    },
    SCHEMA: {
        SCHEMA_STAYS: "The schema does not move: this cutover installs no migration to reverse.",
        SCHEMA_FROZEN: "The schema does not come back. Every revision this cutover installs "
                       "refuses downgrade outright, in every environment.",
        SCHEMA_CONDITIONAL: "Whether the schema moves depends on how far it already got. Above the "
                            "unconditionally-refusing revisions there is no downgrade path at all, "
                            "and the walk down is reachable only on a history that never crossed "
                            "them.",
        SCHEMA_DOWNGRADES: "Rollback walks the schema down; every revision it installed reverses.",
    },
    IMAGE: {
        IMAGE_PRIOR: "You land on the PREVIOUS image.",
        IMAGE_SAME_RELEASE: "You stay on THIS release's image and change only its configuration; "
                            "substituting an earlier image is a defect, not a safe fallback.",
        IMAGE_CONDITIONAL: "Which image is legal depends on the branch you end up in, and the "
                           "older image is prohibited outright on the branch that preserves "
                           "evidence.",
    },
    RESTORES: {
        RESTORES_YES: "YES — rolling back KNOWINGLY reopens what this release closed. That is a "
                      "deliberate, temporary acceptance, not a neutral undo.",
        RESTORES_NO: "No: the sanctioned rollback reopens nothing this release closed.",
    },
    VERIFICATION: {
        VERIFY_NON_MUTATING: "NON-MUTATING CHECKS ONLY. A probe that exercises the closed "
                             "behaviour would PERFORM it on the rolled-back image rather than "
                             "detect it.",
        VERIFY_UNRESTRICTED: "The playbook's normal post-deploy checks apply, including the "
                             "mutating ones.",
        VERIFY_NOT_STATED: "The playbook states no restriction on rollback verification. Treat "
                           "that as unreviewed rather than as permission, and confirm with the "
                           "release owner before running a mutating probe.",
    },
    WINDOW: {
        WINDOW_SAME: "It needs the SAME coordinated window as the forward cutover — the same "
                     "pause, the same full stop, the same attestation.",
        WINDOW_LIGHTER: "It does not need the forward cutover's window.",
    },
    ENDING: {
        ENDING_RESUMED: "You must end FULLY RESUMED: every writer started, retention re-enabled, "
                        "submissions and the composer unblocked. Ending stopped is not a rollback, "
                        "it is an outage.",
        ENDING_MAY_STOP: "Remaining stopped is an accepted outcome of this procedure.",
        ENDING_RESUMED_OR_DECLARED_INCIDENT: "End FULLY RESUMED, or in a DELIBERATELY DECLARED "
                                             "maintenance incident while the forward fix is "
                                             "applied. Never end silently stopped.",
    },
}

ANSWERS[SCHEMA][BR_SCHEMA_HELD] = (
    "the schema STAYS on the witness-authority revision you are already on."
)
ANSWERS[SCHEMA][BR_SCHEMA_WALKED] = (
    "the schema has walked all the way down to the base revision."
)

# Answers that mean "the playbook is silent". They are the only ones allowed to carry no quote, and
# they must carry none — otherwise a quote could be attached to a claim of silence.
SILENT_ANSWERS = frozenset({VERIFY_NOT_STATED})


@dataclass(frozen=True)
class RollbackFact:
    """One answer, plus the playbook sentence it was read from."""

    question: str
    answer: str
    evidence: str = ""

    def __post_init__(self) -> None:
        if self.question not in ROLLBACK_QUESTIONS:
            raise ValueError(f"unknown rollback question {self.question!r}")
        if self.answer not in ANSWERS[self.question]:
            raise ValueError(
                f"{self.question}: {self.answer!r} is not one of "
                f"{sorted(ANSWERS[self.question])}"
            )
        quoted = bool(self.evidence.strip())
        if self.answer in SILENT_ANSWERS and quoted:
            raise ValueError(
                f"{self.question}: {self.answer!r} asserts the playbook is silent, so it cannot "
                "also quote the playbook"
            )
        if self.answer not in SILENT_ANSWERS and not quoted:
            raise ValueError(
                f"{self.question}: {self.answer!r} must quote the playbook sentence it came from"
            )

    @property
    def statement(self) -> str:
        """What the document publishes: the answer's own sentence, plus the playbook's words.

        Every published character is therefore either a reviewed constant from `ANSWERS` or a
        verbatim quote the tests locate inside the digest-bound body. Nothing here is free prose.
        """
        sentence = ANSWERS[self.question][self.answer]
        if self.evidence:
            return f'{sentence} Playbook: "{self.evidence}"'
        return sentence


@dataclass(frozen=True)
class RollbackBranch:
    """One resolved downgrade outcome, with its own total answer set (Wave 1 / F9).

    The flat contract was TOTAL but not BRANCH-SAFE: PR 7b's playbook has two mutually exclusive
    outcomes — R5, downgrade REFUSED (evidence preserved, schema held, pre-7b image PROHIBITED)
    and R6, downgrade SUCCEEDED (walked to base, prior image legal) — and a flat fact bag could
    select the prior image on outcome-B evidence while outcome-A's prohibition sat beside it.
    A branch binds its answers to ONE outcome, its evidence to that outcome's exact span of the
    playbook (`span_marker` — DERIVED from the closed OUTCOME_MARKERS map, never authored, so
    two branches cannot alias one span; gate finding 7), and the outcome-specific laws are
    construction refusals:

      * REFUSED means the schema HELD and the prior image is FORBIDDEN — evidence survived, and
        an older publisher must not run against it.
      * SUCCEEDED means the schema WALKED; only here may the prior image be selected.
      * IMAGE_CONDITIONAL is refused in any branch: a branch IS the condition resolved.
    """

    outcome: str
    facts: tuple[RollbackFact, ...]
    span_marker: str = field(default="", init=False)

    def __post_init__(self) -> None:
        if self.outcome not in BRANCH_OUTCOMES:
            raise ValueError(f"unknown branch outcome {self.outcome!r}")
        object.__setattr__(self, "span_marker", OUTCOME_MARKERS[self.outcome])
        asked = [f.question for f in self.facts]
        missing = [q for q in BRANCH_QUESTIONS if q not in asked]
        extra = [q for q in asked if q not in BRANCH_QUESTIONS]
        duplicated = {q for q in asked if asked.count(q) > 1}
        if missing or extra or duplicated:
            raise ValueError(
                f"branch {self.outcome}: missing={missing} extra={extra} "
                f"duplicated={sorted(duplicated)}"
            )
        schema_answer = self.answer(SCHEMA)
        if schema_answer not in BRANCH_SCHEMA_ANSWERS:
            raise ValueError(
                f"branch {self.outcome}: schema must be a RESULT ({sorted(BRANCH_SCHEMA_ANSWERS)}),"
                f" not the aggregate possibility {schema_answer!r}"
            )
        if self.answer(IMAGE) == IMAGE_CONDITIONAL:
            raise ValueError(f"branch {self.outcome}: a branch is the condition resolved; "
                             "IMAGE_CONDITIONAL has nothing left to depend on")
        if self.outcome == OUTCOME_REFUSED:
            if schema_answer != BR_SCHEMA_HELD:
                raise ValueError("a REFUSED downgrade cannot claim the schema walked")
            if self.answer(IMAGE) == IMAGE_PRIOR:
                raise ValueError(
                    "the prior image on the REFUSED branch: evidence survived, and an older "
                    "publisher lacks the receipt contract — this is the exact cross-branch "
                    "confusion the branch model exists to refuse"
                )
        if self.outcome == OUTCOME_SUCCEEDED and schema_answer != BR_SCHEMA_WALKED:
            raise ValueError("a SUCCEEDED downgrade cannot claim the schema held")

    def answer(self, question: str) -> str:
        for fact in self.facts:
            if fact.question == question:
                return fact.answer
        raise KeyError(question)

    def statements(self) -> tuple[str, ...]:
        title = OUTCOME_TITLES[self.outcome]
        return (f"{title}: " + " ".join(
            self._fact(q).statement for q in BRANCH_QUESTIONS),)

    def _fact(self, question: str):
        for fact in self.facts:
            if fact.question == question:
                return fact
        raise KeyError(question)  # pragma: no cover — totality enforced above


@dataclass(frozen=True)
class RollbackContract:
    """A TOTAL set of rollback answers for one procedure.

    The invariants below are structural — they hold between answers, with no text matching — so
    they bite on exactly the mutations that made the original attack work. See
    `tests/unit/test_contract_registry_authority.py` for the table that proves each one fails.
    """

    facts: tuple[RollbackFact, ...]
    branches: tuple[RollbackBranch, ...] = ()

    def __post_init__(self) -> None:
        asked = [f.question for f in self.facts]
        missing = [q for q in ROLLBACK_QUESTIONS if q not in asked]
        if missing:
            raise ValueError(f"rollback contract does not answer {missing}")
        duplicated = {q for q in asked if asked.count(q) > 1}
        if duplicated:
            raise ValueError(f"rollback contract answers {sorted(duplicated)} more than once")

        # The aggregate answers name possibilities; branch schema RESULTS may not appear there.
        # Checked FIRST: a category error should not be masked by a coupling invariant firing on
        # the same malformed fact.
        for fact in self.facts:
            if fact.question == SCHEMA and fact.answer in BRANCH_SCHEMA_ANSWERS:
                raise ValueError("the aggregate schema answer cannot be a branch result")

        quotes = [f.evidence.strip().lower() for f in self.facts if f.evidence.strip()]
        reused = {q for q in quotes if quotes.count(q) > 1}
        if reused:
            raise ValueError(
                f"the same playbook sentence is cited for more than one question: {sorted(reused)}"
                " — one clause cannot be the evidence for every answer"
            )

        # A rollback that knowingly reopens a closed hole MUST restrict verification. This is the
        # PR 5b lesson as an invariant rather than as a paragraph: on that image the sensitive
        # probe performs the forgery it is meant to find.
        if self.answer(RESTORES) == RESTORES_YES and self.answer(VERIFICATION) != (
            VERIFY_NON_MUTATING
        ):
            raise ValueError(
                "a rollback that restores a closed vulnerability must declare non-mutating "
                "verification only"
            )

        # Staying on this release's own image cannot reopen what this release closed.
        if self.answer(IMAGE) == IMAGE_SAME_RELEASE and self.answer(RESTORES) != RESTORES_NO:
            raise ValueError(
                "staying on this release's image cannot restore what this release closed"
            )

        # A branch-dependent image answer needs two branches to depend on. Only a CONDITIONAL
        # schema has them; a schema that never moves, or never comes back, has exactly one.
        if self.answer(SCHEMA) != SCHEMA_CONDITIONAL and self.answer(IMAGE) == IMAGE_CONDITIONAL:
            raise ValueError(
                "the schema cannot move, so there is no second branch for the image to depend on"
            )

        # A schema that is CONDITIONAL or FROZEN is describing a boundary, and a boundary is what
        # makes a cutover irreversible past it. These two answers cannot disagree.
        if (self.answer(SCHEMA) in (SCHEMA_CONDITIONAL, SCHEMA_FROZEN)) != (
            self.answer(REVERSIBILITY) == IRREVERSIBLE_PAST_BOUNDARY
        ):
            raise ValueError(
                "a schema answer that describes a boundary and an irreversible-past-a-boundary "
                "answer are the same boundary seen twice; publishing one without the other is a "
                "contradiction"
            )

        # "Plainly reversible" is a promise that a redeploy suffices. It cannot coexist with an
        # answer that says the rollback reopens a hole or needs the full coordinated window.
        if self.answer(REVERSIBILITY) == REVERSIBLE_PLAINLY and (
            self.answer(RESTORES) == RESTORES_YES or self.answer(WINDOW) == WINDOW_SAME
        ):
            raise ValueError(
                "this rollback is not plainly reversible: it either reopens a closed hole or needs "
                "the forward cutover's window"
            )

        # Branches exist exactly when the schema answer is CONDITIONAL: that answer names a fork,
        # and a fork published without its resolved sides is the F9 gap; branches on an unforked
        # schema are answers to a question nobody asked.
        if (self.answer(SCHEMA) == SCHEMA_CONDITIONAL) != bool(self.branches):
            raise ValueError(
                "a CONDITIONAL schema requires resolved branches, and branches require a "
                "CONDITIONAL schema"
            )
        outcomes = [b.outcome for b in self.branches]
        if len(set(outcomes)) != len(outcomes):
            raise ValueError(f"duplicate branch outcomes: {outcomes}")

    def _fact(self, question: str) -> RollbackFact:
        for fact in self.facts:
            if fact.question == question:
                return fact
        raise KeyError(question)  # pragma: no cover — totality is enforced above

    def answer(self, question: str) -> str:
        return self._fact(question).answer

    def evidence(self, question: str) -> str:
        return self._fact(question).evidence

    def statements(self) -> tuple[str, ...]:
        """The published rollback prose: the aggregate answers in question order, then one line
        per resolved branch. Derived, never authored."""
        aggregate = tuple(self._fact(q).statement for q in ROLLBACK_QUESTIONS)
        for branch in self.branches:
            aggregate += branch.statements()
        return aggregate


@dataclass(frozen=True)
class Command:
    """One operator command, as the playbook's own marked span publishes it.

    `argv` is the parsed vector; `line` is the EXACT source text of the marked span (after the
    one supported backslash-newline continuation join). The inventory comparison runs on `line`
    bytes (re-audit `1826661..b5c7a83` finding 4): a lookalike with an NBSP or a raw newline is
    not the reviewed command, and there is no root restriction — `rm`, `psql`, `aws`, whatever
    appears in a marked span must be typed here or the section fails, because a destructive
    command is exactly the one the old python/alembic filter waved past.
    """

    argv: tuple[str, ...]
    line: str = ""

    def __post_init__(self) -> None:
        if not self.argv or any(type(a) is not str or not a for a in self.argv):
            raise ValueError("argv must be non-empty exact strings")
        if self.line == "":
            object.__setattr__(self, "line", " ".join(self.argv))
        if self.line.split() != list(self.argv):
            raise ValueError("line and argv must agree token-for-token")


@dataclass(frozen=True)
class PlaybookRef:
    """An exact reference into an operational playbook (Wave 1 / F7).

    `heading` is the FULL heading line, matched by exact string equality against exactly one line
    of the document — a substring match accepted any same-named section, which is how a wrong
    section could satisfy the old pointer. `sha256` is over the EXACT UTF-8 bytes of the section
    — HEADING LINE INCLUDED (re-audit finding 11 corrected the stale text here; the gate fold
    brought the heading inside the digest), up to the next same-or-higher-level CommonMark ATX
    heading — with CRLF→LF the only permitted normalization. The old digest collapsed ALL
    whitespace first, so a shell
    continuation rewritten from a backslash-newline to a backslash-space — which hands the shell a
    literal backslash argument and breaks the command — hashed identically and passed review.
    Exact bytes make every byte a reviewed byte.
    """

    path: str
    heading: str
    sha256: str
    commands: tuple[Command, ...] = ()
    # R-audit-13 finding 1: the document section is a PROJECTION of this typed source
    # file, byte for byte. The document stops being an authoring lane entirely — an
    # instruction appended to the section alone, whatever its case, arity, or vocabulary,
    # breaks projection identity and no digest re-pin can restore it; changing what the
    # projection permits means authoring the typed source, in its own reviewed, pinned
    # lane. Empty only for refs predating the projection surface.
    body_source: str = ""

    def __post_init__(self) -> None:
        # CommonMark ATX only (re-audit `1826661..b5c7a83` finding 11): one to six hashes
        # followed by required whitespace, not code-indented — `##NOT-A-HEADING` is an ordinary
        # line, and treating it as a section anchor let a non-heading bound a reviewed span.
        stripped = self.heading.lstrip(" ")
        indent = len(self.heading) - len(stripped)
        depth = len(stripped) - len(stripped.lstrip("#"))
        if ("\n" in self.heading or indent > 3 or not 1 <= depth <= 6
                or not stripped[depth:depth + 1].isspace()):
            raise ValueError(
                "heading must be one exact CommonMark ATX heading line: 1-6 hashes, a space, "
                "not code-indented"
            )
        if len(self.sha256) != 64 or set(self.sha256) - set("0123456789abcdef"):
            raise ValueError("sha256 must be 64 lowercase hex characters")

    @property
    def display(self) -> str:
        """What the page prints: the path plus the heading's visible text."""
        return f"{self.path} § {self.heading.lstrip('#').strip()}"


@dataclass(frozen=True)
class MigrationSpan:
    """What a cutover INSTALLS, as graph endpoints rather than an authored list (Wave 1 / F8).

    `migration_range` used to be a hand-written tuple, and the bind on it was a subset check —
    so writing `("018", ..., "023")` under the unchanged visible name "Migrations 013-023"
    dropped five revisions from the published account with every guard green. Endpoints cannot
    understate: the range is DERIVED by walking Alembic's own revision graph from base to target
    (`docs/contracts/operations.py` resolves it; the release verifier recomputes it through its
    separately-hardened script-directory helper and requires exact ordered equality), and the
    visible name is derived from the resolved range, so name and range cannot disagree by
    construction. A disconnected, reversed, or unbuilt endpoint refuses at import.
    """

    base_revision: str
    target_revision: str

    def __post_init__(self) -> None:
        for name, value in (("base_revision", self.base_revision),
                            ("target_revision", self.target_revision)):
            if type(value) is not str or not value.strip():
                raise ValueError(f"{name} must be a nonempty revision id")
        if self.base_revision == self.target_revision:
            raise ValueError("a span from a revision to itself installs nothing")
