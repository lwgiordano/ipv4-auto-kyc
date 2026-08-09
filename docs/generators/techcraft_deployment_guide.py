"""Generate the TechCraft deployment guide PDF."""

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Table,
    TableStyle,
)

OUT = "techcraft-deployment-guide.pdf"

styles = getSampleStyleSheet()
H1 = ParagraphStyle("H1x", parent=styles["Heading1"], fontSize=15, spaceBefore=16, spaceAfter=6,
                    textColor=colors.HexColor("#1a1a2e"))
H2 = ParagraphStyle("H2x", parent=styles["Heading2"], fontSize=12, spaceBefore=12, spaceAfter=4,
                    textColor=colors.HexColor("#1a1a2e"))
BODY = ParagraphStyle("Bodyx", parent=styles["Normal"], fontSize=9.5, leading=13, spaceAfter=5)
WHY = ParagraphStyle("Why", parent=BODY, leftIndent=10, textColor=colors.HexColor("#444444"),
                     fontSize=9, leading=12)
CODE = ParagraphStyle("Code", parent=styles["Code"], fontSize=8, leading=10.5, leftIndent=8,
                      spaceAfter=5, backColor=colors.HexColor("#f4f4f4"))
CELL = ParagraphStyle("Cell", parent=BODY, fontSize=8.5, leading=11, spaceAfter=0)
CELLB = ParagraphStyle("CellB", parent=CELL, fontName="Helvetica-Bold")

TBL = TableStyle([
    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#bbbbbb")),
    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8e8f0")),
    ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ("LEFTPADDING", (0, 0), (-1, -1), 5),
    ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ("TOPPADDING", (0, 0), (-1, -1), 3),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
])


def tbl(rows, widths):
    data = [[Paragraph(c, CELLB) for c in rows[0]]] + [
        [Paragraph(c, CELL) for c in r] for r in rows[1:]
    ]
    t = Table(data, colWidths=widths, repeatRows=1)
    t.setStyle(TBL)
    return t


def p(text, style=BODY):
    return Paragraph(text, style)


story = []

story.append(Paragraph("KYC Tool — Deployment Guide for TechCraft", styles["Title"]))
story.append(p("<b>Audience: the TechCraft team that will host and operate the KYC tool.</b> "
               "The Platform Integration Contract is the companion document and covers the wire "
               "protocol."))
story.append(p("IPv4.Global maintains the code and cuts tagged releases. You pull a release, "
               "build the image, and redeploy. Nobody edits code on the server, and schema or "
               "data changes happen only through the runbook playbooks, because the audit trail "
               "assumes the repo is the truth. Every release's notes answer three questions: "
               "does it include a migration, does it add env vars, does it change the contract."))
story.append(p("Three references ship inside each release and go deeper than this guide: "
               "docs/DEPLOYMENT.md (full procedures), docs/RUNBOOK.md (failure playbooks, the "
               "complete config table, the migration refusal index), and docs/ALERTS.md "
               "(alerting rules)."))

# ---------------------------------------------------------------- section 1
story.append(Paragraph("1. What you run: one image, six commands", H1))
story.append(p("The repo Dockerfile builds a single image (digest-pinned python:3.11-slim base, "
               "locked dependency tree, so two builds of one commit resolve identically). Every "
               "process is that image with a different command:"))
story.append(tbl(
    [["Process", "Command", "Notes"],
     ["API", "(image default) uvicorn on :8000", "scale horizontally; LB health = /readyz"],
     ["Migrations", "alembic upgrade head", "one-shot task per deploy; no-op when the release "
      "has no migration"],
     ["Pipeline worker", "python -m kyc_tool.workers.pipeline_worker",
      "scale horizontally; per-case ordering is enforced by the database, so extra workers are "
      "safe"],
     ["Outbox publisher", "python -m kyc_tool.workers.outbox_worker",
      "delivers decision callbacks + verification emails"],
     ["Retention", "python -m kyc_tool.workers.retention", "daily cron; the only sanctioned "
      "data remover"],
     ["Evidence sweeper", "python -m kyc_tool.ops.sweep_staged_evidence --apply",
      "daily/weekly cron; deletes crash-orphaned staged evidence; dry-run without --apply"]],
    [1.15 * inch, 2.9 * inch, 2.65 * inch]))
story.append(p("Disable the image's HTTP healthcheck on worker containers; they serve no HTTP. "
               "The dev_worker role exists for local development only: it wires fixture "
               "adapters, and <b>production boot refuses it categorically</b> — do not create a "
               "task definition for it."))

# ---------------------------------------------------------------- section 2
story.append(Paragraph("2. Infrastructure prerequisites", H1))
story.append(tbl(
    [["Component", "Requirement", "Why"],
     ["PostgreSQL", "RDS 14+, one database",
      "the DB is the ordering and crash-safety authority: job queue, outbox, per-case locks all "
      "live here"],
     ["Object store", "S3 bucket per environment",
      "raw upstream evidence and uploaded documents. IAM: your upload path writes uploads/…; "
      "the tool owns adapter-raw/… (staging + sweeper). Deploy with KYC_OBJECT_STORE=s3"],
     ["Compute", "ECS/Fargate (or EC2) services for section 1's processes",
      "all stateless; nothing persists in a container"],
     ["Secrets", "AWS Secrets Manager (or equivalent) injecting env vars",
      "secrets travel only via environment; nothing secret is logged"],
     ["Email", "SES (KYC_EMAIL_PROVIDER=ses)",
      "POC verification emails to RIR-listed addresses; the dev sink writes files and is "
      "refused in production"],
     ["Egress", "HTTPS to Companies House, GLEIF, the five RIR RDAP services, and the "
      "platform callback URL",
      "the tool throttles its own registry calls; CH_API_KEY required, ARIN_API_KEY optional "
      "(raises RDAP limits)"],
     ["Load balancer", "route /readyz as the target health check",
      "returns 503 until DB connectivity, migration version, and storage access all pass"],
     ["Backups", "RDS automated backups + point-in-time recovery; versioning on the S3 bucket",
      "the database holds the audit trail and every decision's evidence chain, and the "
      "compliance window is seven years — losing it is not recoverable from anywhere else"]],
    [1.05 * inch, 2.7 * inch, 2.95 * inch]))

# ---------------------------------------------------------------- section 3
story.append(Paragraph("3. Configuration that matters (KYC_ prefix)", H1))
story.append(p("Full commented sample in .env.example; full table in RUNBOOK. The production "
               "kill switch validates everything at boot and lists <b>every</b> violation at "
               "once — a bad deploy fails loudly instead of running quietly broken. The "
               "load-bearing set:"))
story.append(tbl(
    [["Variable", "Production value", "Why"],
     ["KYC_ENVIRONMENT", "production", "arms the kill switch; staging runs development until "
      "real providers land there"],
     ["KYC_DATABASE_URL", "postgresql+psycopg://…", ""],
     ["KYC_PLATFORM_CALLBACK_URL", "your HTTPS base", "boot refuses HTTP, localhost, or any "
      "query/fragment"],
     ["HMAC set (8 vars)", "v1 legacy secret; v2 inbound key_id + secret; v2 outbound key_id + "
      "secret; both v1 sunset dates; the observation window",
      "boot refuses a partial set; secrets 32+ chars; sunset dates must be tz-aware ISO-8601. "
      "The dates are agreed with TechCraft at cutover, and the inbound one only takes effect "
      "once the observation window has recorded zero v1 traffic"],
     ["KYC_OBJECT_STORE / KYC_S3_BUCKET", "s3 / your bucket", "fs store is dev-only, refused "
      "in production"],
     ["KYC_OCR_ENGINE / KYC_EMAIL_PROVIDER / KYC_ADAPTERS_PROFILE",
      "real engine / ses / real", "the three stubs are refused in production"],
     ["KYC_READ_AUTH_REQUIRED / KYC_UI_ADMIN_TOKEN", "true / 32+ char token",
      "the admin token guards the always-on ops requeue endpoints, so production requires it "
      "even with the /ui console off"],
     ["KYC_ENFORCE_POSITIVE_DECISIONS", "false at launch",
      "temporary safety hold: computed approvals route to manual review until the validator "
      "hardening milestone; flipping it is a deliberate, coordinated change"],
     ["KYC_JOB_LEASE_SECONDS", "default 120 (floor 30 in production)",
      "the worker heartbeat runs at lease/4; a tiny lease races the reaper"],
     ["KYC_OUTBOX_LEASE_SECONDS + KYC_OUTBOX_HTTP_TIMEOUT_SECONDS + margin",
      "defaults 300 / 10 / 1.0; lease must exceed 4 × timeout + margin",
      "each delivery attempt has a 4×timeout hard wall; the rule guarantees a claim cannot "
      "expire mid-attempt — boot refuses violations"],
     ["KYC_OUTBOX_MAX_ATTEMPTS", "default 8 — NOT hot-swappable",
      "any change, up or down, is the section 6 drained cutover; a rolling restart runs two "
      "ceilings against the same rows and can irreversibly burn POC email retries"],
     ["KYC_ADAPTER_HARD_KILL_BOUNDARY", "false (opt-in)",
      "fork-per-call hard kill of adapter fetches at the plan deadline; bounds worker occupancy "
      "against hostile slow upstreams at a per-fetch fork cost"],
     ["KYC_RETENTION_DAYS", "2555 (7 years)", "compliance window; retention redacts delivered "
      "callback/email bodies in place past it"]],
    [1.7 * inch, 2.35 * inch, 2.65 * inch]))
story.append(p("Never set KYC_AUTH_DISABLED outside local dev; production boot refuses it.", WHY))

# ---------------------------------------------------------------- section 4
story.append(Paragraph("4. First deploy, per environment", H1))
story.append(tbl(
    [["#", "Step", "Detail"],
     ["1", "Provision", "section 2 components"],
     ["2", "Exchange keys", "HMAC key ids + secrets both directions with the integration "
      "contract's owner, via encrypted channel"],
     ["3", "Set env", "section 3; verify staging values by hand — development mode's /readyz "
      "does NOT vet config"],
     ["4", "Migrate", "run the alembic upgrade head task to completion"],
     ["5", "Start", "API + workers; wire the LB to /readyz; disable container healthcheck on "
      "workers"],
     ["6", "Verify", "GET /readyz 200 on every instance; GET /healthz returns the policy "
      "bundle hash — it must match the release notes"],
     ["7", "Activate v1 observation", "one-shot: python -m "
      "kyc_tool.ops.activate_hmac_v1_observation — without it the v1 sunset clock never "
      "starts, by design"],
     ["8", "Smoke test", "send one signed kyb.run_requested (script in docs/PLATFORM_BRIEFING "
      "§7) and confirm the decision callback arrives"]],
    [0.35 * inch, 1.6 * inch, 4.75 * inch]))

# ---------------------------------------------------------------- section 5
story.append(Paragraph("5. Releases", H1))
story.append(p("<b>Default path, which covers most releases:</b> pull the tag, build, set any "
               "new env vars from the notes, run the migration task (safe when the release has "
               "none), rolling restart API then workers, then run the section 6 checks. "
               "Rollback for a no-migration release is redeploying the previous image tag."))
story.append(p("<b>When the notes say the migration is not hot-compatible:</b> brief cutover — "
               "stop the processes, migrate, start the new image. Do not assume compatibility "
               "the notes do not claim; an old replica against a new schema fails writes."))
story.append(p("<b>Schema rollback policy:</b> alembic downgrades exist, but several shipped "
               "migrations are deliberately forward-only once real delivery evidence exists — "
               "their downgrades refuse with grep-able sentinels (indexed in RUNBOOK § "
               "Migration refusal sentinels) rather than destroy immutable audit records. After "
               "that point, rollback means redeploying the previous reviewed image on the "
               "schema you already have, never walking the schema down. Staging may downgrade "
               "freely; in production, check with IPv4.Global first."))
story.append(Paragraph("The drained cutover", H2))
story.append(p("Changing KYC_OUTBOX_MAX_ATTEMPTS in either direction:"))
story.append(Paragraph(
    "1. disable autoscaling and rolling restart<br/>"
    "2. stop ALL publishers of roles: outbox_worker, dev_worker<br/>"
    "3. attest zero publishers running of roles: outbox_worker, dev_worker<br/>"
    "4. attest every new task definition carries KYC_OUTBOX_MAX_ATTEMPTS<br/>"
    "5. start publishers of roles: outbox_worker, dev_worker", CODE))
story.append(p("Set KYC_OUTBOX_MAX_ATTEMPTS_ATTESTED to the reviewed target in the new task "
               "definitions during the window (a publisher whose live ceiling disagrees with "
               "its own attested value refuses to boot) and unset it afterward. It is a "
               "per-process check only; steps 2-3 are the fleet control.", WHY))

# ---------------------------------------------------------------- section 6
story.append(Paragraph("6. Health, monitoring, alerts", H1))
story.append(tbl(
    [["Surface", "Contract", "Action"],
     ["GET /healthz", "liveness + the policy bundle hash",
      "hash must match the release notes; a hash change without a deploy is an incident — "
      "policy files are immutable per release"],
     ["GET /readyz", "503 until DB, migration version, and storage access pass; in production "
      "it also runs the full config kill switch",
      "a failing readyz means the tool is refusing traffic on purpose; fix the cause, do not "
      "route around it"],
     ["GET /v1/metrics", "JSON gauges", "alert when jobs_by_status.dead &gt; 0, "
      "outbox_by_status.dead &gt; 0, runs_by_state.FAILED grows, any adapter error_rate "
      "spikes, or event_to_decision_seconds.p95 exceeds budget (10s light / 120s full runs)"],
     ["GET /v1/metrics.prom", "same signals, Prometheus exposition",
      "scrape target; docs/ALERTS.md has the ready-made rules"]],
    [1.15 * inch, 2.75 * inch, 2.8 * inch]))

# ---------------------------------------------------------------- section 7
story.append(Paragraph("7. Day-2 operations", H1))
story.append(p("<b>Recovery endpoints (always mounted, bearer = KYC_UI_ADMIN_TOKEN):</b> POST "
               "/v1/ops/requeue/job/{id} and /v1/ops/requeue/outbox/{id}. The job requeue is "
               "deliberately strict: it grants a fixed retry budget, refuses when a newer "
               "same-case job exists (submit recalculate.requested instead), and verifies that "
               "the failed run it resets belongs to the same case. Treat a 409 as the guard "
               "doing its job and investigate the pairing rather than forcing past it."))
story.append(p("<b>Three limits worth knowing before an incident:</b> a dead poc_email cannot "
               "be requeued (its token was scrubbed when it died; recovery is a fresh "
               "poc.submitted). recalculate.requested re-scores existing evidence but does not "
               "re-run the broker screen; after a blocklist update, re-send the original "
               "evidence event. A registry outage needs no action — runs complete as partial "
               "and nothing wrong is emitted."))
story.append(p("<b>Crons:</b> retention daily; the staged-evidence sweeper daily or weekly "
               "(dry-run first; it only ever touches the tool's own adapter-raw/ namespace, "
               "never uploads/). <b>Escalation:</b> dead-letter growth you cannot requeue, any "
               "migration refusal sentinel, or a /healthz hash surprise goes to IPv4.Global "
               "with the sentinel text or metric snapshot."))

# ---------------------------------------------------------------- section 8
story.append(Paragraph("8. What refuses to boot", H1))
story.append(p("Production startup refuses, rather than degrading, on any of these: a stub "
               "provider (OCR, email, or adapters profile); missing or short (&lt; 32 char) "
               "HMAC secrets, or an incomplete v2 set; a naive or malformed sunset date; an "
               "HTTP, localhost, or query-bearing callback URL; a missing KYC_UI_ADMIN_TOKEN; "
               "KYC_AUTH_DISABLED; KYC_JOB_LEASE_SECONDS below 30; an outbox lease that does "
               "not clear 4 × HTTP timeout + margin; a zero lease margin; the dev_worker role; "
               "an attested-versus-live outbox ceiling mismatch during a cutover window."))
story.append(p("The startup log prints every violation it finds, so one failed boot tells you "
               "the whole list and you fix it in a single pass."))

doc = SimpleDocTemplate(OUT, pagesize=letter, leftMargin=0.75 * inch, rightMargin=0.75 * inch,
                        topMargin=0.7 * inch, bottomMargin=0.7 * inch,
                        title="KYC Tool — Deployment Guide for TechCraft")
doc.build(story)
print(OUT)
