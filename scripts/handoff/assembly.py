"""Assemble the seven reviewed public documents into one handoff reference."""

from __future__ import annotations

from pathlib import Path

PUBLIC_DOCUMENTS = (
    ("Product and integration briefing", "PLATFORM_BRIEFING.md"),
    ("Platform integration reference", "PLATFORM_INTEGRATION.md"),
    ("Deployment guide", "DEPLOYMENT.md"),
    ("Operations runbook", "RUNBOOK.md"),
    ("Alert reference", "ALERTS.md"),
    ("Salesforce mapping", "SALESFORCE_MAPPING.md"),
    ("Production readiness", "PRODUCTION_READINESS.md"),
)


def _body_without_title(text: str) -> str:
    """Drop a standalone document's first H1 beneath the combined part heading."""
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines = lines[1:]
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    while lines and not lines[0].strip():
        lines = lines[1:]
    while lines and not lines[-1].strip():
        lines = lines[:-1]
    return "\n".join(lines)


def build_combined_handoff(docs_dir: Path, *, label: str | None = None, commit: str | None = None) -> str:
    """Build the combined handoff from every reviewed public document."""
    bodies = []
    for part_title, filename in PUBLIC_DOCUMENTS:
        path = docs_dir / filename
        if not path.is_file():
            raise FileNotFoundError(f"required public handoff document is missing: {path}")
        bodies.append((part_title, filename, path.read_text(encoding="utf-8")))

    provenance = (
        f"Release: `{label}`\n\nSource revision: `{commit}`"
        if label and commit
        else "Supported environment: closed staging."
    )
    sections = [
        "# IPv4.Global KYC/KYB — Integration and Operations Reference",
        "",
        provenance,
        "Production use is not supported by this release.",
        "Section numbers restart in each part.",
        "",
        "## Contents",
        "",
    ]
    for number, (part_title, filename, _text) in enumerate(bodies, 1):
        sections.append(f"- Part {number}: {part_title} (`docs/{filename}`)")
    for number, (part_title, _filename, text) in enumerate(bodies, 1):
        sections.extend(["", f"# Part {number}: {part_title}", "", _body_without_title(text)])
    return "\n".join(sections) + "\n"


def write_combined_handoff(
    docs_dir: Path,
    output: Path | None = None,
    *,
    label: str | None = None,
    commit: str | None = None,
) -> Path:
    """Write the combined handoff and return its path."""
    destination = output or docs_dir / "TECHCRAFT_HANDOFF.md"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(build_combined_handoff(docs_dir, label=label, commit=commit), encoding="utf-8")
    return destination
