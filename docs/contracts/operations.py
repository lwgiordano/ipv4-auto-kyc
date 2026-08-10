"""Deployment, cutover, rollback, and activation obligations.

Same discipline as `wire.py`: literal claims, validated against independent authority by
`tests/unit/test_contract_registry_authority.py`. Cutover step sequences are stored as ORDERED
tuples so a test can assert both membership and order — a reordered prerequisite is a real defect
(attesting zero publishers before stopping them proves nothing), and a token-presence check would
miss it.

The blocker at the top is the reason this document is titled a staging guide.
"""

from docs.contracts import Claim, ClaimState, Registry

# Ordered cutover step records. The canonical outbox-ceiling cutover lives in
# kyc_tool.ops.cutover and is compared against the shipped record, not restated.
PR5B_FULL_WINDOW = (
    "Publish the reviewed image and record its digest; every step below that runs code is pinned "
    "to that one digest.",
    "Confirm with the platform team, in writing, that they will pause and buffer EVERY event type "
    "for the window and re-sign each retry with a fresh timestamp against the same "
    "Idempotency-Key.",
    "Pause all platform event submission and edge-block the ops composer.",
    "Stop the API pool and the pipeline-worker pool together, as one action, with no graceful "
    "drain; confirm both are at zero before continuing.",
    "Recover interrupted jobs with the one-shot recovery module from the pinned image, before any "
    "new worker starts.",
    "Start the new API replicas with workers still at zero, and probe each one.",
    "Start the new workers.",
    "Resume platform submission and drain their buffer.",
)

PR6_BUNDLE_PINNING = (
    "Deploy the rolling part first: the additive migration and provenance columns, flag still off.",
    "Seed the policy bundle and read it back.",
    "Run the pinnable-backlog preflight.",
    "Stop every old pipeline worker and attest zero are running.",
    "Recover interrupted rows.",
    "Start flag-on workers.",
    "Require the bundle_pinning_ready attestation on every replica before resuming.",
    "Resume, then CAS the durable epoch.",
)

PR7B_CORE_PREWINDOW = (
    "Suspend the retention schedule.",
    "Terminate every active retention task and wait for it to exit.",
    "Capture orchestrator evidence that zero retention tasks are running.",
    "Run the pre-window backfill diagnostic while retention stays suspended.",
    "On failure ABORT before any outage: restore the exact callback row from authoritative backup, "
    "or remain on the prior revision in BLOCKED_NO_AUTHORITATIVE_MAPPING. Never fabricate a "
    "callback, delete a decision, or fall back to decided_at.",
    "Only then stop and attest every writer role and proceed with the window.",
)

OPERATIONS = Registry(
    name="operations",
    claims=(
        # ── the blocker ───────────────────────────────────────────────────────────────────────
        Claim(
            id="OPS.BLOCKER.PRODUCTION_PROVIDERS",
            value=(
                "This tool cannot run in production yet, and no configuration makes it.",
                "The production kill switch refuses to boot on the stub OCR engine, the stub "
                "email provider, and the fixture adapter profile.",
                "Every non-stub value raises NotImplementedError inside the worker's provider "
                "factories: the OCR engine selector, the adapter-profile registry, and the email "
                "sender factory each implement the stub path only.",
                "So a production configuration is refused by config, and a production-safe "
                "configuration crashes the pipeline worker at wiring. The real provider contracts "
                "are pending work (ROADMAP PR 9a-c) and their external interfaces are recorded as "
                "unknown.",
                "Everything in this guide is therefore a STAGING deployment plus the production "
                "readiness you will need. Do not schedule a production launch from it.",
            ),
            authority="kyc_tool.workers.pipeline_worker._make_ocr_engine / build_adapters + "
                      "kyc_tool.outbox.emails.make_email_sender + "
                      "kyc_tool.config.production_config_violations",
            state=ClaimState.BLOCKED,
        ),
        # ── processes and infrastructure ──────────────────────────────────────────────────────
        Claim(
            id="OPS.PROCESS.COMMANDS",
            value=(
                ("API", "uvicorn kyc_tool.api.app:create_app --factory --host 0.0.0.0 --port 8000",
                 "image default; scale horizontally; load balancer health checks /readyz"),
                ("Migrations", "alembic upgrade head",
                 "one-shot per deploy; a no-op when the release carries none"),
                ("Pipeline worker", "python -m kyc_tool.workers.pipeline_worker",
                 "scale horizontally; per-case ordering is enforced by the database, so extra "
                 "workers are safe"),
                ("Outbox publisher", "python -m kyc_tool.workers.outbox_worker",
                 "delivers decision callbacks and verification emails"),
                ("Retention", "python -m kyc_tool.workers.retention",
                 "daily cron; the only sanctioned remover of data"),
                ("Evidence sweeper", "python -m kyc_tool.ops.sweep_staged_evidence --apply",
                 "daily or weekly cron; deletes crash-orphaned staged evidence; omit --apply for "
                 "a dry run"),
            ),
            authority="module existence under src/kyc_tool + Dockerfile command comments",
        ),
        Claim(
            id="OPS.PROCESS.DEV_WORKER_BANNED",
            value="dev_worker is a development role that wires fixture adapters. Production boot "
                  "refuses it categorically. Do not create a task definition for it.",
            authority="kyc_tool.config.validate_process_role (_DEV_ONLY_ROLES)",
        ),
        Claim(
            id="OPS.INFRA.COMPONENTS",
            value=(
                ("PostgreSQL", "RDS 14+, one database",
                 "the database is the ordering and crash-safety authority: job queue, outbox, and "
                 "per-case locks all live here"),
                ("Object store", "one S3 bucket per environment",
                 "the platform writes uploads/; the tool owns adapter-raw/ for staging and "
                 "sweeping. Separate write scopes keep either side from clobbering the other's "
                 "evidence"),
                ("Compute", "ECS/Fargate or EC2 services for the processes above",
                 "all stateless; nothing persists inside a container"),
                ("Secrets", "a secret manager injecting environment variables",
                 "secrets travel only via environment, and nothing secret is logged"),
                ("Email", "a real provider is required in production and none is implemented yet",
                 "see the blocker; staging uses the file or logging sink"),
                ("Egress", "HTTPS to Companies House, GLEIF, the five RIR RDAP services, and the "
                 "platform callback URL",
                 "the tool throttles its own registry calls"),
                ("Backups", "RDS automated backups with point-in-time recovery, and versioning on "
                 "the bucket",
                 "the database holds the audit trail and every decision's evidence chain under a "
                 "seven-year window, and a cutover abort path can require restoring an exact row "
                 "from authoritative backup"),
            ),
            authority="docs/DEPLOYMENT.md §3 + kyc_tool.config storage settings",
        ),
        # ── configuration ─────────────────────────────────────────────────────────────────────
        Claim(
            id="OPS.CONFIG.DEFAULTS",
            value={
                "job_lease_seconds": 120,
                "job_max_attempts": 5,
                "job_recovery_attempt_grant": 5,
                "outbox_lease_seconds": 300,
                "outbox_http_timeout_seconds": 10,
                "outbox_max_attempts": 8,
                "outbox_backoff_base_seconds": 10,
                "retention_days": 2555,
                "adapter_max_response_bytes": 5242880,
                "poc_token_ttl_hours": 72,
            },
            authority="kyc_tool.config.Settings field defaults",
        ),
        Claim(
            id="OPS.CONFIG.PRODUCTION_FLOORS",
            value=(
                "job_lease_seconds is floored at 30 in production: the worker heartbeat runs at a "
                "quarter of the lease, and a tiny lease races the reaper.",
                "outbox_lease_seconds must exceed 4 x outbox_http_timeout_seconds plus the lease "
                "margin, because each delivery attempt carries a 4x hard wall; boot refuses a "
                "violation.",
                "The lease margin must be greater than zero in production.",
                "ui_admin_token is required even with the console disabled, because it "
                "authenticates the always-mounted ops requeue endpoints.",
            ),
            authority="kyc_tool.config.NUMERIC_SETTINGS + production_config_violations",
        ),
        Claim(
            id="OPS.CONFIG.HMAC_SET",
            value=("KYC_PLATFORM_HMAC_SECRET", "KYC_HMAC_INBOUND_KEY_ID",
                   "KYC_HMAC_INBOUND_SECRET", "KYC_HMAC_OUTBOUND_KEY_ID",
                   "KYC_HMAC_OUTBOUND_SECRET", "KYC_HMAC_V1_INBOUND_SUNSET_AT",
                   "KYC_HMAC_V1_OUTBOUND_SUNSET_AT", "KYC_HMAC_V1_OBSERVATION_WINDOW_DAYS"),
            authority="kyc_tool.config.production_config_violations HMAC block",
            note="Production refuses a partial set. Secrets are at least 32 characters and sunset "
                 "dates must be timezone-aware ISO-8601.",
        ),
        Claim(
            id="OPS.CONFIG.ROTATION_KEYS",
            value="KYC_HMAC_INBOUND_EXTRA_KEYS is a JSON object of key_id to secret, used only "
                  "during a rotation overlap. Each entry is a live verification credential and "
                  "carries the active key's floor: at least 32 characters, a non-blank key id "
                  "without surrounding whitespace, no duplicate, and no collision with the active "
                  "key id. Both construction and the production boundary refuse a violation. "
                  "Unset it once the old key is retired.",
            authority="kyc_tool.config.hmac_extra_key_violations",
        ),
        Claim(
            id="OPS.CONFIG.M2_GATE",
            value="KYC_ENFORCE_POSITIVE_DECISIONS stays false in production, and nobody flips it "
                  "from this guide. While it is false, computed approvals route to manual review. "
                  "Flipping it is the M2 hard stop and requires an explicit approval gate: the M4 "
                  "backlog complete, a staging end-to-end run on real adapters, and the platform "
                  "cutovers complete. The gate is deliberately the whole backlog rather than a "
                  "named subset, so that no prerequisite is omitted by reading a shorter list. "
                  "The flag itself is permanent, is enabled per environment, and its wiring is "
                  "never removed.",
            authority=".agents/ROADMAP.md M2 + docs/DEPLOYMENT.md §2",
        ),
        # ── health ────────────────────────────────────────────────────────────────────────────
        Claim(
            id="OPS.HEALTH.PROBES",
            value=(
                ("GET /healthz", "liveness, and returns the policy bundle hash",
                 "the hash must match the release notes; a change without a deploy is an incident, "
                 "because policy files are immutable per release"),
                ("GET /readyz", "readiness: database connectivity, migration version, storage "
                 "access, and in production the full configuration kill switch",
                 "a failing readyz means the tool is refusing traffic deliberately; fix the cause "
                 "rather than routing around it"),
                ("GET /v1/metrics and /v1/metrics.prom", "operational gauges",
                 "alert on dead jobs, dead outbox rows, FAILED run growth, adapter error rates, "
                 "and the event-to-decision p95"),
            ),
            authority="kyc_tool.api.app health routes + routes_metrics",
        ),
        # ── releases, cutovers, rollback ──────────────────────────────────────────────────────
        Claim(
            id="OPS.RELEASE.CLASSIFICATION",
            value="Every release declares itself rolling or full-maintenance INDEPENDENTLY of "
                  "whether it carries a migration. Migration presence does not classify a "
                  "release: the reviewer-actor security boundary shipped with no migration and "
                  "still required a full window, because during any overlap an old replica still "
                  "honours the forgery the release closes.",
            authority="docs/architecture-decisions.md ADR-004 + docs/DEPLOYMENT.md §9",
        ),
        Claim(
            id="OPS.CUTOVER.FULL_WINDOW_STEPS",
            value=PR5B_FULL_WINDOW,
            authority="docs/DEPLOYMENT.md §9 (ordered)",
            note="Order is load-bearing: stopping both pools together removes the overlap a "
                 "sequenced stop leaves open, and recovery runs before any new worker starts.",
        ),
        Claim(
            id="OPS.CUTOVER.BUNDLE_PINNING_STEPS",
            value=PR6_BUNDLE_PINNING,
            authority="docs/DEPLOYMENT.md §10 (ordered)",
            note="Flipping bundle pinning with a rolling pool lets peers resolve different "
                 "bundles, and an unavailable historical bundle dead-letters.",
        ),
        Claim(
            id="OPS.CUTOVER.PR7B_PREWINDOW_STEPS",
            value=PR7B_CORE_PREWINDOW,
            authority="docs/DEPLOYMENT.md §11 step 0 (ordered)",
            note="The diagnostic runs BEFORE any outage precisely so a missing authoritative "
                 "callback is discovered while there is still nothing to restore under time "
                 "pressure.",
        ),
        Claim(
            id="OPS.CUTOVER.OUTBOX_CEILING",
            value=(
                "disable autoscaling and rolling restart",
                "stop ALL publishers of roles: outbox_worker, dev_worker",
                "attest zero publishers running of roles: outbox_worker, dev_worker",
                "attest every new task definition carries KYC_OUTBOX_MAX_ATTEMPTS",
                "start publishers of roles: outbox_worker, dev_worker",
            ),
            authority="kyc_tool.ops.cutover.OUTBOX_MAX_ATTEMPTS_CUTOVER (canonical record)",
            note="Any change to the ceiling, up or down, follows this. A rolling restart runs two "
                 "ceilings against the same rows and can irreversibly burn POC email retries.",
        ),
        Claim(
            id="OPS.ROLLBACK.MIGRATION_BOUNDARY",
            value=(
                "A release with no migration rolls back by redeploying the previous image tag.",
                "Migrations 013 through 023 are forward-only once any delivery witness exists. "
                "Their downgrades refuse with stable sentinels rather than destroying immutable "
                "delivery evidence.",
                "Migrations 018 and above refuse downgrade UNCONDITIONALLY, in every environment "
                "including staging. There is no downgrade-freely rule anywhere.",
                "Above that boundary the supported rollback is redeploying the previous reviewed "
                "image against the schema you are already on. The schema does not move.",
                "A pre-7b image is legal only after a successful walk down to 012, which is only "
                "possible on a schema that never reached 018.",
            ),
            authority="docs/DEPLOYMENT.md §6 + the migration downgrade sentinels",
        ),
        Claim(
            id="OPS.HMAC.ROLLOUT_ORDER",
            value=(
                "Deploy with both v1 sunset dates in the future so both schemes are accepted.",
                "Run a v2 INBOUND smoke against a real signature.",
                "Run a v2-ONLY callback end-to-end in staging and get explicit written sign-off "
                "from the receiving team; the outbound sunset is gated on that sign-off and is "
                "never inferred from inbound telemetry.",
                "Only then activate the inbound v1 observation clock.",
                "Keep the legacy v1 secret configured through BOTH sunsets.",
            ),
            authority="docs/architecture-decisions.md ADR-003 + kyc_tool.outbox.publisher v1 "
                      "dual-emit",
            note="The publisher drops v1 the moment the outbound date passes, so an unready "
                 "receiver starts rejecting callbacks until they dead-letter.",
        ),
        Claim(
            id="OPS.RECOVERY.REQUEUE",
            value="The always-mounted ops endpoints POST /v1/ops/requeue/job/{id} and "
                  "/v1/ops/requeue/outbox/{id} take the admin bearer token. The job requeue is "
                  "deliberately strict: it grants a fixed retry budget, refuses when a newer "
                  "same-case job exists, refuses while any same-case job is running, and verifies "
                  "that the failed run it resets belongs to the same case. Treat a 409 as the "
                  "guard working and investigate the pairing.",
            authority="kyc_tool.ops.requeue_service.requeue_dead_job",
        ),
    ),
)
