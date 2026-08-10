"""Renders the TechCraft Staging Integration and Production Readiness Guide from
`docs.contracts.operations`.

Run from the repo root:  python -m docs.generators.techcraft_deployment_guide

The title is deliberate. A production launch is impossible today (see
OPS.BLOCKER.PRODUCTION_PROVIDERS, which the tests prove by executing the provider factories), so
this document stands a staging environment up and tells you what production still needs.
"""

from docs.contracts.operations import OPERATIONS
from docs.generators.render import INCH, Doc, escape

OUT = "techcraft-deployment-guide.pdf"

REQUIRED_CLAIMS = (
    "OPS.BLOCKER.PRODUCTION_PROVIDERS",
    "OPS.PROCESS.COMMANDS", "OPS.PROCESS.DEV_WORKER_BANNED", "OPS.INFRA.COMPONENTS",
    "OPS.CONFIG.DEFAULTS", "OPS.CONFIG.PRODUCTION_FLOORS", "OPS.CONFIG.HMAC_SET",
    "OPS.CONFIG.ROTATION_KEYS", "OPS.CONFIG.M2_GATE",
    "OPS.HEALTH.PROBES",
    "OPS.RELEASE.CLASSIFICATION", "OPS.CUTOVER.FULL_WINDOW_STEPS",
    "OPS.CUTOVER.BUNDLE_PINNING_STEPS", "OPS.CUTOVER.PR7B_PREWINDOW_STEPS",
    "OPS.CUTOVER.OUTBOX_CEILING", "OPS.ROLLBACK.MIGRATION_BOUNDARY",
    "OPS.HMAC.ROLLOUT_ORDER", "OPS.RECOVERY.REQUEUE",
)


def build() -> Doc:
    doc = Doc(OPERATIONS)
    doc.title("KYC Tool — Staging Integration and Production Readiness Guide")
    doc.p("<b>Audience: the TechCraft team that will host and operate the KYC tool.</b> The "
          "Platform Integration Contract is the companion document and covers the wire protocol.")

    doc.h1("Read this before provisioning anything")
    doc.claim_alert("OPS.BLOCKER.PRODUCTION_PROVIDERS")
    doc.p("Everything below stands up a working staging environment and lists what production "
          "will require. Treat the production columns as targets to build toward, not as "
          "settings to apply on a launch date.")

    doc.p("IPv4.Global maintains the code and cuts tagged releases. You pull a release, build the "
          "image, and redeploy. Nobody edits code on the server, and schema or data changes "
          "happen only through the runbook playbooks, because the audit trail assumes the repo "
          "is the truth.")
    doc.p("Three references ship inside each release and go deeper than this guide: "
          "docs/DEPLOYMENT.md (full procedures), docs/RUNBOOK.md (failure playbooks, the "
          "complete configuration table, the migration refusal index), and docs/ALERTS.md.")

    # ── processes ──────────────────────────────────────────────────────────────────────────────
    doc.h1("1. What you run: one image, six commands")
    doc.p("The repo Dockerfile builds a single image (digest-pinned base, locked dependency tree, "
          "so two builds of one commit resolve identically). Every process is that image with a "
          "different command.")
    doc.claim_table("OPS.PROCESS.COMMANDS", ("Process", "Command", "Notes"),
                    [1.15 * INCH, 2.9 * INCH, 2.65 * INCH])
    doc.space()
    doc.p("Disable the image's HTTP healthcheck on worker containers; they serve no HTTP.")
    doc.claim_paragraph("OPS.PROCESS.DEV_WORKER_BANNED")

    # ── infrastructure ─────────────────────────────────────────────────────────────────────────
    doc.h1("2. Infrastructure")
    doc.claim_table("OPS.INFRA.COMPONENTS", ("Component", "Requirement", "Why"),
                    [1.05 * INCH, 2.7 * INCH, 2.95 * INCH])

    # ── configuration ──────────────────────────────────────────────────────────────────────────
    doc.h1("3. Configuration (KYC_ prefix)")
    doc.p("The full commented sample is .env.example and the complete table is in RUNBOOK. The "
          "production kill switch validates everything at boot and prints every violation at "
          "once, so one failed boot gives you the whole list.")
    defaults = OPERATIONS.value("OPS.CONFIG.DEFAULTS")
    doc._record("OPS.CONFIG.DEFAULTS")
    doc.table(("Setting", "Default"),
              tuple((f"KYC_{name.upper()}", str(value)) for name, value in defaults.items()),
              [3.6 * INCH, 3.1 * INCH])
    doc.space()
    doc.h2("Bounds production enforces")
    doc.claim_bullets("OPS.CONFIG.PRODUCTION_FLOORS")
    hmac_set = OPERATIONS["OPS.CONFIG.HMAC_SET"]
    doc._record("OPS.CONFIG.HMAC_SET")
    doc.h2(f"The HMAC set: all {len(hmac_set.value)} values are required together")
    doc.code("\n".join(hmac_set.value))
    doc.p(escape(hmac_set.note))
    doc.claim_paragraph("OPS.CONFIG.ROTATION_KEYS", prefix="<b>Rotation keys: </b>")
    doc.h2("The one flag nobody flips from this guide")
    doc.claim_paragraph("OPS.CONFIG.M2_GATE")

    # ── health ─────────────────────────────────────────────────────────────────────────────────
    doc.h1("4. Health and monitoring")
    doc.claim_table("OPS.HEALTH.PROBES", ("Surface", "Contract", "Action"),
                    [1.5 * INCH, 2.5 * INCH, 2.7 * INCH])

    # ── releases ───────────────────────────────────────────────────────────────────────────────
    doc.h1("5. Releases and cutovers")
    doc.claim_paragraph("OPS.RELEASE.CLASSIFICATION")
    doc.p("<b>Rolling release:</b> pull the tag, build, set any new environment variables from "
          "the notes, run the migration task (a no-op when the release carries none), rolling "
          "restart API then workers, then run the section 4 checks.")
    doc.p("The procedures below are summaries of the authoritative playbooks in "
          "docs/DEPLOYMENT.md. Run them from that document, which carries the exact commands, "
          "refusal sentinels, and abort paths. The step ORDER shown here is asserted by tests, "
          "because in each of these a reordered step is the failure.")

    doc.h2("Full maintenance window")
    doc.claim_steps("OPS.CUTOVER.FULL_WINDOW_STEPS")
    doc.why(escape(OPERATIONS["OPS.CUTOVER.FULL_WINDOW_STEPS"].note))

    doc.h2("Bundle-pinning activation")
    doc.claim_steps("OPS.CUTOVER.BUNDLE_PINNING_STEPS")
    doc.why(escape(OPERATIONS["OPS.CUTOVER.BUNDLE_PINNING_STEPS"].note))

    doc.h2("Migration 013-023 pre-window (before any outage begins)")
    doc.claim_steps("OPS.CUTOVER.PR7B_PREWINDOW_STEPS")
    doc.why(escape(OPERATIONS["OPS.CUTOVER.PR7B_PREWINDOW_STEPS"].note))

    doc.h2("Changing the outbox attempt ceiling")
    doc.claim_steps("OPS.CUTOVER.OUTBOX_CEILING")
    doc.why(escape(OPERATIONS["OPS.CUTOVER.OUTBOX_CEILING"].note))

    doc.h2("HMAC v2 rollout order")
    doc.claim_steps("OPS.HMAC.ROLLOUT_ORDER")
    doc.why(escape(OPERATIONS["OPS.HMAC.ROLLOUT_ORDER"].note))

    # ── rollback ───────────────────────────────────────────────────────────────────────────────
    doc.h1("6. Rollback")
    doc.claim_bullets("OPS.ROLLBACK.MIGRATION_BOUNDARY")

    # ── day 2 ──────────────────────────────────────────────────────────────────────────────────
    doc.h1("7. Day-2 operations")
    doc.claim_paragraph("OPS.RECOVERY.REQUEUE")
    doc.p("<b>Three limits worth knowing before an incident.</b> A dead POC email cannot be "
          "requeued, because its token was scrubbed when the row went terminal; recovery is a "
          "fresh poc.submitted. recalculate.requested re-scores stored evidence but does not "
          "re-run the broker screen, so after a blocklist update re-send the original evidence "
          "event. A registry outage needs no action: runs complete as partial and nothing wrong "
          "is emitted.")
    doc.p("<b>Crons:</b> retention daily, and the staged-evidence sweeper daily or weekly with a "
          "dry run first. The sweeper only ever touches the tool's own adapter-raw/ namespace.")
    return doc


def main() -> str:
    doc = build()
    return doc.build(OUT, "KYC Tool — Staging Integration and Production Readiness Guide")


if __name__ == "__main__":
    print(main())
