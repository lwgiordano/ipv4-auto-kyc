"""Published safety statements, SELECTED by a fact rather than authored beside one.

Wave-2 audit finding 1 (`0253fed`). Every one of these sentences used to live in `Claim.note`:
free prose, deliberately subtracted from the authority map, with no verifier reading it. The
only control was a digest recorded beside the prose, so a lie and its re-pin landed in one edit
and self-certified — and two notes were even classified as verifier-bound while their verifiers
never read them. Codex published the reproductions: an early-2xx receiver note and an
"the named playbook is optional" cutover note both certified.

`Claim.note` is gone. Each of those sentences is now its own claim whose value is a
`Statement`, and a statement is not written — it is CHOSEN:

- a `Fact` names a question about the running system, the CLOSED set of answers it can have,
  and, per answer, the distinguishing tokens a sentence claiming that answer must carry (and
  must not borrow from a sibling answer — the cross-binding rule the condition legend already
  uses);
- a `Statement` names its fact, DECLARES the answer that holds today, and supplies one sentence
  per possible answer. The published `text` is `alternatives[answer]`, computed at construction:
  there is no free text field to edit.

What that buys, precisely. To publish "a 2xx before your commit is fine", the statement must
declare `answer="recoverable"` — and `tests/unit/test_contract_registry_authority.py` executes
the publisher and asserts the observed answer is `unrecoverable`, so the claim fails. To keep
`answer="unrecoverable"` and simply reword its sentence into the lie, the token discipline
refuses it: the sentence must carry that answer's own token and none of its sibling's. The
alternatives are total over the answer domain, so the inconvenient branch cannot be deleted
either, and both readings sit side by side in the source where a reviewer sees what each answer
would publish.

Honest scope, stated once. This binds MEANING to an executed fact; it cannot make English
self-verifying. A reviewer editing this module can still change what a fact's tokens mean, in
code, in a diff — and that is the terminus every control in this repo reaches. The difference
from a note is that the terminus is now reviewed code beside an executed probe, rather than a
prose field with a digest that the same edit updates.
"""

import copy
from dataclasses import dataclass, field, fields
from types import MappingProxyType
from typing import ClassVar


@dataclass(frozen=True)
class Fact:
    """One question about the running system, its closed answers, and each answer's tokens."""

    id: str
    question: str
    # answer -> the distinguishing terms a sentence claiming that answer must carry. Every
    # answer needs at least one, and no term may be shared between two answers of one fact:
    # a token that fits both readings distinguishes nothing.
    tokens: dict

    def __post_init__(self) -> None:
        if not self.id or not self.question.strip():
            raise ValueError("a fact carries an id and the question it answers")
        if len(self.tokens) < 2:
            raise ValueError(
                f"{self.id}: a fact with one possible answer cannot distinguish anything"
            )
        seen: dict = {}
        for answer, terms in self.tokens.items():
            if not terms or any(not t.strip() for t in terms):
                raise ValueError(f"{self.id}/{answer}: every answer needs a distinguishing term")
            for term in terms:
                if term.casefold() in seen:
                    raise ValueError(
                        f"{self.id}: {term!r} is claimed by both {seen[term.casefold()]!r} and "
                        f"{answer!r}, so it distinguishes nothing"
                    )
                seen[term.casefold()] = answer
        object.__setattr__(self, "tokens", MappingProxyType(dict(self.tokens)))

    def answers(self) -> tuple[str, ...]:
        return tuple(self.tokens)

    def __deepcopy__(self, memo):
        """Read-only maps are not deep-copyable, so a frozen record that exposes one says how it
        is copied. Used by the metamorphic proof, which tampers with a COPY."""
        clone = object.__new__(type(self))
        for f in fields(self):
            value = getattr(self, f.name)
            object.__setattr__(
                clone, f.name,
                MappingProxyType(copy.deepcopy(dict(value), memo))
                if isinstance(value, MappingProxyType) else copy.deepcopy(value, memo))
        memo[id(self)] = clone
        return clone


@dataclass(frozen=True)
class Statement:
    """A published sentence, selected by its fact's answer. `text` is derived, never authored."""

    fact: Fact
    answer: str
    alternatives: dict
    text: str = field(default="", init=False)

    PUBLISHED_FIELDS: ClassVar[tuple[str, ...]] = ("text",)

    def __post_init__(self) -> None:
        if type(self.fact) is not Fact:
            raise ValueError("a statement is selected by a typed Fact")
        if self.answer not in self.fact.tokens:
            raise ValueError(
                f"{self.fact.id}: {self.answer!r} is not one of its answers "
                f"{self.fact.answers()}"
            )
        if set(self.alternatives) != set(self.fact.tokens):
            raise ValueError(
                f"{self.fact.id}: alternatives must cover every answer exactly "
                f"({self.fact.answers()}), not {tuple(self.alternatives)} — a missing branch is "
                "the one a reviewer never gets to compare"
            )
        for answer, sentence in self.alternatives.items():
            if type(sentence) is not str or not sentence.strip():
                raise ValueError(f"{self.fact.id}/{answer}: every answer publishes a sentence")
            own = self.fact.tokens[answer]
            if not any(term.casefold() in sentence.casefold() for term in own):
                raise ValueError(
                    f"{self.fact.id}/{answer}: the sentence published for this answer carries "
                    f"none of its own terms {own} — it does not say what the answer says"
                )
            for sibling, terms in self.fact.tokens.items():
                if sibling == answer:
                    continue
                borrowed = [t for t in terms if t.casefold() in sentence.casefold()]
                if borrowed:
                    raise ValueError(
                        f"{self.fact.id}/{answer}: this sentence borrows {borrowed} from the "
                        f"{sibling!r} answer, so the reader cannot tell which one it states"
                    )
        object.__setattr__(self, "alternatives", MappingProxyType(dict(self.alternatives)))
        object.__setattr__(self, "text", self.alternatives[self.answer])

    def __deepcopy__(self, memo):
        clone = object.__new__(type(self))
        for f in fields(self):
            value = getattr(self, f.name)
            object.__setattr__(
                clone, f.name,
                MappingProxyType(copy.deepcopy(dict(value), memo))
                if isinstance(value, MappingProxyType) else copy.deepcopy(value, memo))
        memo[id(self)] = clone
        return clone


# ── the facts these documents publish, and what each possible answer must say ────────────────────
#
# `tokens` is the cross-binding rule the condition legend already uses: a sentence claiming an
# answer must carry that answer's own distinguishing term and none of a sibling's, so a reworded
# alternative cannot quietly state the opposite of the answer it is filed under.
_FACT_SOURCE = {
    "ack_before_commit": (
        "what happens to a decision the platform 2xx-acknowledges before its own commit?",
        {"unrecoverable": ("unrecoverable",), "recovered": ("we retry", "recovers")},
    ),
    "direction_token_form": (
        "does the direction field verify against prose, or only its literal token?",
        {"literal_only": ("Literal tokens",), "prose_ok": ("prose also verifies",)},
    ),
    "companion_is_executed": (
        "does our suite execute the shipped signer file's own bytes against the published vector?",
        {"executed": ("executes the file's exact bytes",), "unread": ("is not executed",)},
    ),
    "optional_field_handling": (
        "what must a receiver do with the optional callback fields?",
        {"tolerate_and_preserve": ("Tolerate and preserve",), "may_drop": ("may drop",)},
    ),
    "validation_order": (
        "does validation precede classification, or follow it?",
        {"validate_first": ("VALIDATE BEFORE YOU CLASSIFY",),
         "classify_first": ("classify first",)},
    ),
    "ack_vs_apply": (
        "are acknowledging a callback and applying it the same decision?",
        {"different_decisions": ("are different decisions",), "same_decision": ("are the same",)},
    ),
    "legend_closure": (
        "is the condition vocabulary closed to the legend's tokens?",
        {"closed": ("exactly these tokens",), "open": ("other terms may appear",)},
    ),
    "release_protocol_state": (
        "can the manual-release protocol be exercised today?",
        {"post_024_only": ("POST-024 ONLY",), "live": ("live today",)},
    ),
    "ordering_ordinal": (
        "do both per-case ordinals order decisions?",
        {"only_one_orders": ("only one of them orders decisions",),
         "both_order": ("either ordinal orders",)},
    ),
    "obligation_resolution": (
        "can an emailed answer resolve an O1-O4 obligation today?",
        {"unresolvable": ("remain PENDING",), "resolvable": ("clears the obligation",)},
    ),
    "hmac_set_completeness": (
        "does production accept a partial HMAC variable set?",
        {"refuses_partial": ("refuses a partial set",), "accepts_partial": ("accepts a subset",)},
    ),
    "procedure_execution_source": (
        "where does an operator execute a cutover from?",
        {"playbook_only": ("EXECUTE from the named playbook",),
         "summaries_ok": ("the named playbook is optional",)},
    ),
    "ceiling_change_window": (
        "may an outbox ceiling change go out as a rolling restart?",
        {"needs_this_procedure": ("Any change to the ceiling, up or down, follows this",),
         "rolling_ok": ("a rolling restart is fine",)},
    ),
    "v1_drop_timing": (
        "when does the publisher stop signing v1?",
        {"at_the_sunset_date": ("the moment the outbound date passes",),
         "after_confirmation": ("only once you confirm",)},
    ),
}

FACTS: dict = {
    fact_id: Fact(id=fact_id, question=question, tokens=tokens)
    for fact_id, (question, tokens) in _FACT_SOURCE.items()
}


def statement(fact_id: str, answer: str, alternatives: dict) -> Statement:
    """One published sentence, selected by `answer` from the alternatives for `fact_id`."""
    return Statement(fact=FACTS[fact_id], answer=answer, alternatives=alternatives)
