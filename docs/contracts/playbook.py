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

from dataclasses import dataclass

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
    },
}

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
class RollbackContract:
    """A TOTAL set of rollback answers for one procedure.

    The invariants below are structural — they hold between answers, with no text matching — so
    they bite on exactly the mutations that made the original attack work. See
    `tests/unit/test_contract_registry_authority.py` for the table that proves each one fails.
    """

    facts: tuple[RollbackFact, ...]

    def __post_init__(self) -> None:
        asked = [f.question for f in self.facts]
        missing = [q for q in ROLLBACK_QUESTIONS if q not in asked]
        if missing:
            raise ValueError(f"rollback contract does not answer {missing}")
        duplicated = {q for q in asked if asked.count(q) > 1}
        if duplicated:
            raise ValueError(f"rollback contract answers {sorted(duplicated)} more than once")

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
        """The published rollback prose, in question order. Derived, never authored."""
        return tuple(self._fact(q).statement for q in ROLLBACK_QUESTIONS)


@dataclass(frozen=True)
class Command:
    """One executable command the referenced playbook section MUST contain, as parsed argv.

    Wave 1 / F7's second half. The exact-bytes digest proves the section was re-reviewed after ANY
    edit; it cannot prove the command inside it still parses to what the operator needs. These
    records are compared against an INDEPENDENT parse of the section's backtick spans — argv by
    argv — so a command edit that someone re-pins the digest over is still caught unless the typed
    record moves with it, which is a second, visible act.
    """

    argv: tuple[str, ...]

    def __post_init__(self) -> None:
        if len(self.argv) < 2:
            raise ValueError("a command record needs at least an interpreter and a target")
        if self.argv[0] not in ("python", "alembic"):
            raise ValueError(f"unrecognised command root {self.argv[0]!r}")


@dataclass(frozen=True)
class PlaybookRef:
    """An exact reference into an operational playbook (Wave 1 / F7).

    `heading` is the FULL heading line, matched by exact string equality against exactly one line
    of the document — a substring match accepted any same-named section, which is how a wrong
    section could satisfy the old pointer. `sha256` is over the EXACT UTF-8 bytes of the section
    body (heading line excluded, up to the next same-or-higher-level heading), with CRLF→LF the
    only permitted normalization. The old digest collapsed ALL whitespace first, so a shell
    continuation rewritten from a backslash-newline to a backslash-space — which hands the shell a
    literal backslash argument and breaks the command — hashed identically and passed review.
    Exact bytes make every byte a reviewed byte.
    """

    path: str
    heading: str
    sha256: str
    commands: tuple[Command, ...] = ()

    def __post_init__(self) -> None:
        if not self.heading.startswith("#") or "\n" in self.heading:
            raise ValueError("heading must be the exact single heading line, hashes included")
        if len(self.sha256) != 64 or set(self.sha256) - set("0123456789abcdef"):
            raise ValueError("sha256 must be 64 lowercase hex characters")

    @property
    def display(self) -> str:
        """What the page prints: the path plus the heading's visible text."""
        return f"{self.path} § {self.heading.lstrip('#').strip()}"
