#!/usr/bin/env python3
"""Build `docs/TECHCRAFT_HANDOFF.md`: the three TechCraft documents in one file.

    .venv/bin/python scripts/build_techcraft_handoff.py [--out PATH]

The output is a pure function of the three sources, so rebuilding an unchanged tree
rewrites the same bytes. Each source is copied whole, headings untouched, so every
"§N" cross-reference between the documents still lands where it says it does.
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
This file is generated: it stacks the three source documents in reading order and
changes nothing inside them. Start with Part 1 for what the tool does and who does
what. Part 2 is the wire contract your engineers build against. Part 3 is for whoever
runs the service. Section numbers belong to the source files, so a pointer such as
"`docs/PLATFORM_INTEGRATION.md` §5" means the same section here. Edit a source and
rebuild with `.venv/bin/python scripts/build_techcraft_handoff.py`."""


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
