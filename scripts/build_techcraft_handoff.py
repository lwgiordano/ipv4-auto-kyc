#!/usr/bin/env python3
"""Build `docs/TECHCRAFT_HANDOFF.md`: the three TechCraft documents in one file.

    .venv/bin/python scripts/build_techcraft_handoff.py [--out PATH]

The output is a pure function of the three sources, so rebuilding an unchanged tree
rewrites the same bytes. Each source is copied whole, headings untouched.
Section references are local to the named source document, not globally numbered.
`tests/unit/test_techcraft_handoff_doc.py` fails when the checked-in copy drifts.
"""

import argparse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO_ROOT / "docs" / "TECHCRAFT_HANDOFF.md"

# Reading order: what the tool is, then the wire, then running it.
PARTS = (
    ("Part 1", "docs/PLATFORM_BRIEFING.md"),
    ("Part 2", "docs/PLATFORM_INTEGRATION.md"),
    ("Part 3", "docs/DEPLOYMENT.md"),
)

COVER_TITLE = "# TechCraft handoff"
HOW_TO_READ = """\
This release is for closed staging, not production. It verifies a registrant's
connection to a company and returns a decision for the platform to handle.

Start with **Part 1**: what the tool does, what TechCraft builds, and the decisions
and access needed from each team. Integration developers use **Part 2** for events,
signatures, callback handling and Salesforce reads. Operators use **Part 3** for
deployment and recovery. You do not need to read all three parts to answer the
product questions.

The three source documents are included in full below. Section numbers restart in
each part: `PLATFORM_INTEGRATION.md` §5 means §5 within Part 2. Older deployment
procedures are retained for upgrades; they are not a claim that production is ready."""


def outline(text: str) -> tuple[str, list[str]]:
    """The document's own title and its `## ` headings. Fenced blocks are skipped, so a
    shell comment inside an example never reads as a heading."""
    title, sections, in_fence = "", [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        elif in_fence:
            continue
        elif line.startswith("## "):
            sections.append(line[3:].strip())
        elif line.startswith("# ") and not title:
            title = line[2:].strip()
    return title, sections


def build(root: Path = REPO_ROOT) -> str:
    bodies = [(label, rel, (root / rel).read_text(encoding="utf-8")) for label, rel in PARTS]
    out = [COVER_TITLE, "", HOW_TO_READ, "", "## Contents", ""]
    for label, rel, text in bodies:
        title, sections = outline(text)
        # Label, then path, then the source's own title. No dash separator: a title that
        # carries an em-dash of its own would make the line read as machine cadence.
        out.append(f"- **{label}** (`{rel}`): {title}")
        out.extend(f"  - {section}" for section in sections)
    for label, _rel, text in bodies:
        out += ["", f"# {label}", "", text.strip()]
    return "\n".join(out) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="where to write the document")
    args = parser.parse_args()
    args.out.write_text(build(), encoding="utf-8")
    print(args.out)


if __name__ == "__main__":
    main()
