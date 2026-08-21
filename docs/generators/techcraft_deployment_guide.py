"""Renders the TechCraft Staging Integration and Production Readiness Guide from
`docs.contracts.operations`.

Run from the repo root:  python -m docs.generators.techcraft_deployment_guide

The title is deliberate. A production launch is impossible today (see
OPS.BLOCKER.PRODUCTION_PROVIDERS, which the tests prove by executing the provider factories), so
this document stands a staging environment up and tells you what production still needs.
"""

from docs.contracts.operations import OPERATIONS
from docs.generators.render import INCH, Doc, DocumentManifest, escape

# The document's own identity: the title every page footer and the PDF metadata publish, owned
# here rather than passed to build() (Wave-2 audit finding 3).
MANIFEST = DocumentManifest(
    title="KYC Tool — Staging Integration and Production Readiness Guide",
    out="techcraft-deployment-guide.pdf",
)
OUT = MANIFEST.out

REQUIRED_CLAIMS = (
    "OPS.BLOCKER.PRODUCTION_PROVIDERS",
    "OPS.PROCESS.COMMANDS",
    "OPS.PROCESS.DEV_WORKER_BANNED",
    "OPS.INFRA.COMPONENTS",
    "OPS.CONFIG.DEFAULTS",
    "OPS.CONFIG.PRODUCTION_FLOORS",
    "OPS.CONFIG.HMAC_SET",
    "OPS.CONFIG.ROTATION_KEYS",
    "OPS.CONFIG.M2_GATE",
    "OPS.HEALTH.PROBES",
    "OPS.RELEASE.CLASSIFICATION",
    "OPS.CUTOVER.PROCEDURES",
    "OPS.CUTOVER.OUTBOX_CEILING",
    "OPS.ROLLBACK.MIGRATION_BOUNDARY",
    "OPS.HMAC.ROLLOUT_ORDER",
    "OPS.RECOVERY.REQUEUE",
)


def build() -> Doc:
    doc = Doc(OPERATIONS, MANIFEST)
    doc.title()
    doc.p(
        "<b>Audience: the TechCraft team that will host and operate the KYC tool.</b> The "
        "Platform Integration Contract is the companion document and covers the wire protocol."
    )

    doc.section("blocker", "Read this before provisioning anything")
    doc.claim_alert("OPS.BLOCKER.PRODUCTION_PROVIDERS")
    doc.p(
        "Everything below stands up a working staging environment and lists what production "
        "will require. Treat the production columns as targets to build toward, not as "
        "settings to apply on a launch date."
    )

    doc.p(
        "IPv4.Global maintains the code and cuts tagged releases. You pull a release, build the "
        "image, and redeploy. Nobody edits code on the server, and schema or data changes "
        "happen only through the runbook playbooks, because the audit trail assumes the repo "
        "is the truth."
    )
    doc.p(
        "Three references ship inside each release and go deeper than this guide: "
        "docs/DEPLOYMENT.md (full procedures), docs/RUNBOOK.md (failure playbooks, the "
        "complete configuration table, the migration refusal index), and docs/ALERTS.md."
    )

    # ── processes ──────────────────────────────────────────────────────────────────────────────
    doc.section("processes", "1. What you run: one image, six commands")
    doc.p(
        "The repo Dockerfile builds a single image (digest-pinned base, locked dependency tree, "
        "so two builds of one commit resolve identically). Every process is that image with a "
        "different command."
    )
    doc.claim_table("OPS.PROCESS.COMMANDS", [1.15 * INCH, 2.9 * INCH, 2.65 * INCH])
    doc.space()
    doc.p("Disable the image's HTTP healthcheck on worker containers; they serve no HTTP.")
    doc.claim_paragraph("OPS.PROCESS.DEV_WORKER_BANNED")

    # ── infrastructure ─────────────────────────────────────────────────────────────────────────
    doc.section("infrastructure", "2. Infrastructure")
    doc.claim_table("OPS.INFRA.COMPONENTS", [1.05 * INCH, 2.7 * INCH, 2.95 * INCH])

    # ── configuration ──────────────────────────────────────────────────────────────────────────
    doc.section("configuration", "3. Configuration (KYC_ prefix)")
    doc.p(
        "The full commented sample is .env.example and the complete table is in RUNBOOK. The "
        "production kill switch validates everything at boot and prints every violation at "
        "once, so one failed boot gives you the whole list."
    )
    doc.claim_table("OPS.CONFIG.DEFAULTS", [3.6 * INCH, 3.1 * INCH], code_columns=(0,))
    doc.space()
    doc.h2("Bounds production enforces")
    doc.claim_bullets("OPS.CONFIG.PRODUCTION_FLOORS")
    hmac_set = OPERATIONS["OPS.CONFIG.HMAC_SET"]
    doc.claim_code(
        "OPS.CONFIG.HMAC_SET",
        heading=f"The HMAC set: all {len(hmac_set.value)} values are required together",
    )
    doc.claim_note("OPS.CONFIG.HMAC_SET")
    doc.claim_paragraph("OPS.CONFIG.ROTATION_KEYS", prefix="<b>Rotation keys: </b>")
    doc.h2("The one flag nobody flips from this guide")
    doc.claim_paragraph("OPS.CONFIG.M2_GATE")

    # ── health ─────────────────────────────────────────────────────────────────────────────────
    doc.section("health", "4. Health and monitoring")
    doc.claim_table("OPS.HEALTH.PROBES", [1.5 * INCH, 2.5 * INCH, 2.7 * INCH])

    # ── releases ───────────────────────────────────────────────────────────────────────────────
    doc.section("releases", "5. Releases and cutovers")
    doc.claim_paragraph("OPS.RELEASE.CLASSIFICATION")
    doc.p(
        "<b>Rolling release:</b> pull the tag, build, set any new environment variables from "
        "the notes, run the migration task (a no-op when the release carries none), rolling "
        "restart API then workers, then run the section 4 checks."
    )
    doc.h2("Non-rolling procedures: plan here, execute from the playbook")
    doc.claim_note("OPS.CUTOVER.PROCEDURES")
    # Full-width blocks, not a five-column table. The table was cramped and, worse, unprintable:
    # at every column split that fit the page, BLOCKED_NO_AUTHORITATIVE_MAPPING — a sentinel an
    # operator greps for — was too wide for its cell, and a prose cell clips rather than wraps
    # such a word. Prerequisites are whole sentences and need the page width.
    parts = []
    for procedure in OPERATIONS.value("OPS.CUTOVER.PROCEDURES"):
        parts.append(("p", f"<b>{escape(procedure.name)}</b> — {escape(procedure.when)}"))
        parts.append(("why", "<b>What must be true before you start:</b> " + "  ".join(
            f"({n}) {escape(condition)}"
            for n, condition in enumerate(procedure.blocks_start, 1))))
        parts.append(("why", f"<b>Reversible?</b> {escape(procedure.irreversible)}"))
        parts.append(("why", "<b>Rolling back:</b> " + "  ".join(
            f"({n}) {escape(fact)}" for n, fact in enumerate(procedure.rollback, 1))))
        parts.append(("why", f"<b>Playbook:</b> {escape(procedure.playbook)}"))
    # the ref itself (exact heading + exact-bytes sha256 + command records) is checked, not
    # printed; the page shows only the derived pointer.
    doc.claim_mixed("OPS.CUTOVER.PROCEDURES", parts, published_fields=(
        "name", "when", "blocks_start", "irreversible", "rollback", "playbook"))

    doc.claim_steps("OPS.CUTOVER.OUTBOX_CEILING", heading="Changing the outbox attempt ceiling")
    doc.claim_note("OPS.CUTOVER.OUTBOX_CEILING")

    doc.claim_steps("OPS.HMAC.ROLLOUT_ORDER", heading="HMAC v2 rollout order")
    doc.claim_note("OPS.HMAC.ROLLOUT_ORDER")

    # ── rollback ───────────────────────────────────────────────────────────────────────────────
    doc.section("rollback", "6. Rollback")
    doc.claim_bullets("OPS.ROLLBACK.MIGRATION_BOUNDARY")

    # ── day 2 ──────────────────────────────────────────────────────────────────────────────────
    doc.section("day2", "7. Day-2 operations")
    doc.claim_paragraph("OPS.RECOVERY.REQUEUE")
    doc.p(
        "<b>Three limits worth knowing before an incident.</b> A dead POC email cannot be "
        "requeued, because its token was scrubbed when the row went terminal; recovery is a "
        "fresh poc.submitted. recalculate.requested re-scores stored evidence but does not "
        "re-run the broker screen, so after a blocklist update re-send the original evidence "
        "event. A registry outage needs no action: runs complete as partial and nothing wrong "
        "is emitted."
    )
    doc.p(
        "<b>Crons:</b> retention daily, and the staged-evidence sweeper daily or weekly with a "
        "dry run first. The sweeper only ever touches the tool's own adapter-raw/ namespace."
    )
    return doc


def main() -> str:
    doc = build()
    return doc.build(MANIFEST.out)


if __name__ == "__main__":
    print(main())
