"""The typed playbook body model (R-audit-14, `dd72855` finding 1).

The previous projection slice proved byte identity between the document and a SECOND
hand-authored Markdown file — a mirror, not a source: a coherent dual edit published an
untyped instruction with no definition-pin change. This module is the class fix's first
half: ONE typed source and ONE renderer. A playbook section is an ordered tuple of exactly
two block kinds —

- ``Narrative``: human-reviewed prose, structurally BARRED from carrying the operator
  lane (constructing one whose text opens a fence is a ``ValueError``, so a runnable
  block cannot be smuggled in as prose), and scanned by the defense-in-depth command
  classifier at verification;
- ``OperatorInstruction``: the ONLY block kind that renders an operator fence, and it
  renders it exclusively from typed :class:`~docs.contracts.playbook.Command` records —
  there is no free-text way to publish a runnable line.

``render_body`` emits the section bytes the document must equal. Honest scope, stated
once: a Narrative of ordinary English ("Delete all backups.") is indistinguishable from
prose by any machinery this repo has or could have — it is refused not by classification
but by the definition pin, which covers EVERY byte of every block, so any source edit is
the one deliberate re-pin a human reviews.
"""

import re
from dataclasses import dataclass

from markdown_it import MarkdownIt

from docs.contracts.playbook import Command

# ONE real CommonMark block tree, shared by construction and verification (R-audit-15
# finding 1: a line-prefix fence check is not a parser — `>` + an operator fence is a
# real code block inside a blockquote, and indented code and raw <pre> are code blocks
# too, none of which start the line with a fence).
_PARSER = MarkdownIt("commonmark")
_HTML_CODE = re.compile(r"<\s*(pre|code|script|style|textarea)\b", re.IGNORECASE)


def code_blocks(text: str) -> list:
    """Every CODE block a CommonMark reader gets, at ANY container depth.

    Returns `(kind, info, content)` in document order — fenced blocks (```/~~~),
    indented code blocks, and raw-HTML blocks that open a code-ish element. Nesting is
    irrelevant by construction: this walks the parser's own token stream, so a fence
    inside a blockquote, inside nested blockquotes, or inside a list item is found the
    same way a reader finds it. Ordinary blockquotes, lists, headings, and paragraphs
    are not code and are not reported.
    """
    found = []
    for token in _PARSER.parse(text):
        if token.type == "fence":
            found.append(("fence", token.info.strip(), token.content))
        elif token.type == "code_block":
            found.append(("indented", "", token.content))
        elif token.type == "html_block" and _HTML_CODE.search(token.content):
            found.append(("html", "", token.content))
    return found


@dataclass(frozen=True)
class Narrative:
    """Human-reviewed prose lines, rendered verbatim. Never the operator FENCE lane.

    Prose may still carry the document's inline marked-command convention
    (``double-backticks``), and those are typed too: every marked span in the text must
    be declared, in order, as a `Command` in `commands`, or construction refuses. So
    there is no free-text way to publish a runnable line anywhere in a body — fenced or
    inline."""

    text: str
    commands: tuple[Command, ...] = ()

    def __post_init__(self) -> None:
        if type(self.text) is not str or not self.text:
            raise ValueError("a Narrative carries exact non-empty prose text")
        # R-audit-15 finding 1: judged by the REAL block tree, so nesting cannot hide a
        # code block. Prose carries NO code block of any kind — an operator fence is the
        # obvious one, but an indented block, a raw <pre>, or any fence inside a
        # blockquote or list item publishes runnable-looking text just as loudly.
        blocks = code_blocks(self.text)
        if blocks:
            kind, info, content = blocks[0]
            raise ValueError(
                f"a Narrative may not contain a code block ({kind}"
                f"{'/' + info if info else ''}, at any nesting depth): "
                f"{content.strip()[:60]!r} — runnable lines are published only by "
                "OperatorInstruction, from typed Command records"
            )
        if any(type(c) is not Command for c in self.commands):
            raise ValueError("a Narrative's declared commands are typed Command records")
        marked = tuple(re.findall(r"(?<!`)``([^`]+?)``(?!`)", self.text))
        declared = tuple(c.line for c in self.commands)
        if marked != declared:
            raise ValueError(
                "every inline marked command in prose must be declared as a typed "
                f"Command, in order: text publishes {marked}, declared {declared}"
            )


@dataclass(frozen=True)
class OperatorInstruction:
    """The typed operator lane: an operator fence rendered ONLY from Command records.

    ``wraps`` is optional per-command PRESENTATION — the exact backslash-continued lines
    the fence prints. It cannot change what runs: construction refuses any wrap whose
    shell-continuation join differs from the owning Command's canonical line, so the
    Command record stays the sole authority and the wrap is layout alone."""

    commands: tuple[Command, ...]
    wraps: tuple[tuple[str, ...], ...] = ()

    def __post_init__(self) -> None:
        if not self.commands or any(type(c) is not Command for c in self.commands):
            raise ValueError(
                "an OperatorInstruction publishes at least one typed Command record"
            )
        if self.wraps:
            if len(self.wraps) != len(self.commands):
                raise ValueError("wraps, when given, cover every command in order")
            for command, wrapped in zip(self.commands, self.wraps, strict=True):
                joined: list[str] = []
                for raw in wrapped:
                    if joined and joined[-1].endswith("\\"):
                        joined[-1] = joined[-1][:-1].rstrip() + " " + raw.strip()
                    elif raw.strip():
                        joined.append(raw.strip())
                if joined != [command.line]:
                    raise ValueError(
                        "a wrap is presentation only: its continuation-join must equal "
                        f"the typed command line exactly ({command.line!r})"
                    )

    def rendered(self) -> str:
        lines = ["```operator"]
        if self.wraps:
            for wrapped in self.wraps:
                lines.extend(wrapped)
        else:
            lines.extend(c.line for c in self.commands)
        lines.append("```")
        return "\n".join(lines)


def render_body(body: tuple) -> str:
    """The ONE renderer: section bytes from typed blocks, newline-joined verbatim."""
    parts = []
    for block in body:
        if type(block) is Narrative:
            parts.append(block.text)
        elif type(block) is OperatorInstruction:
            parts.append(block.rendered())
        else:
            raise ValueError(
                f"a playbook body holds only Narrative and OperatorInstruction blocks, "
                f"not {type(block).__name__}"
            )
    return "\n".join(parts)
