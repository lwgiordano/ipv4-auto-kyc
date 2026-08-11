"""Deployment, cutover, rollback, and activation obligations.

Same discipline as `wire.py`: literal claims, validated against independent authority by
`tests/unit/test_contract_registry_authority.py`. Step sequences that ARE stored here are stored
as ORDERED tuples so a test can assert both membership and order — a reordered prerequisite is a
real defect (attesting zero publishers before stopping them proves nothing), and a token-presence
check would miss it.

The blocker at the top is the reason this document is titled a staging guide.
"""

from dataclasses import dataclass

from docs.contracts import Claim, ClaimState, Registry

# CUTOVERS ARE NOT SUMMARIZED HERE (re-audit `6feca36..4f23f23` F5).
#
# An earlier version carried hand-written step lists for three procedures. Every one of them
# dropped a control that exists to prevent irreversible damage: the direct trusted-path probes (a
# load-balancer 403 can otherwise certify a broken app as healthy), the flag-only rollback on the
# PR6 image (substituting the prior image mints permanent NULL provenance), and the writer-role
# set plus the 023-compatible image requirement (a pre-7b publisher run against preserved witness
# authority). Tests that assert a first and last phrase pass over every one of those omissions.
#
# A summary of a safety procedure is a second copy that drifts, so these claims now publish what
# an operator needs in order to PLAN — the procedure exists, what blocks starting it, what makes
# it irreversible — and send them to the one authoritative playbook to EXECUTE. You cannot
# truncate what you never restate.
#
# The outbox-ceiling cutover below is the exception, and deliberately so: it is rendered from the
# shipped canonical record in kyc_tool.ops.cutover and validated against it, so it is generated
# from the authority rather than summarized from prose.


@dataclass(frozen=True)
class Procedure:
    """A referenced (not restated) operational procedure.

    `playbook_digest` is what makes the reference load-bearing (re-audit `4f23f23..97deeae`
    finding 8). Asserting that a heading EXISTS proves nothing about the body under it: pointing
    the repo at a DEPLOYMENT.md containing only the three headings passed the verifier. The digest
    is taken over the normalized body of the referenced section, so an empty body, a wrong
    same-named section, or an edit nobody re-reviewed all fail. Re-pinning it is the re-review.

    `rollback` carries the facts a one-line "reversible?" answer loses. PR 5b's rollback is not
    "redeploy the previous image": it mirrors the same maintenance window, it knowingly restores
    the vulnerability the release closed, and its verification is non-mutating ONLY — the
    mutation probes would perform the forgery they are meant to detect.
    """

    name: str
    when: str
    blocks_start: tuple[str, ...]
    irreversible: str
    playbook: str
    playbook_digest: str
    rollback: tuple[str, ...] = ()


FULL_WINDOW = Procedure(
    # Scoped to PR 5b (re-audit `4f23f23..97deeae` finding 8). It read "any release the notes
    # classify as full-maintenance" while pointing at PR5b-specific steps and a PR5b-specific
    # rollback, so a future unrelated full-window release would inherit the wrong procedure.
    name="PR 5b full maintenance window",
    when="The PR 5b reviewer-actor security release. It carries no migration and still requires a "
         "full window, because during any overlap an old replica still honours the forgery the "
         "release closes. Other releases classified full-maintenance get their own procedure; do "
         "not reuse this one.",
    blocks_start=(
        "The reviewed image is published and its digest recorded; every step that runs code is "
        "pinned to that digest, and the recovery module exists only in the new image.",
        "The platform has confirmed IN WRITING that it will pause and buffer every event type for "
        "the window, and re-sign each retry with a fresh timestamp against the same "
        "Idempotency-Key.",
        "You can probe each new replica directly on its trusted path, not through the load "
        "balancer: an edge 403 or 503 must never be read as an application answer.",
    ),
    irreversible="No, but rollback is not a plain redeploy — see the rollback conditions below.",
    rollback=(
        "Rollback MIRRORS THE SAME WINDOW: pause submission, stop all processes together, run the "
        "recovery one-shot pinned to the last image that still contains it, then redeploy the "
        "prior image for API and workers.",
        "It KNOWINGLY RESTORES THE VULNERABILITY this release closed. The prior image has no "
        "actor floor.",
        "Verification is NON-MUTATING ONLY: /readyz, /healthz, and prior-image digest "
        "attestation. Do not run the sensitive-mutation probes against the prior image — on that "
        "image the probe PERFORMS the forgery it is meant to detect rather than finding it.",
        "Keep all submission and the composer blocked until the non-mutating checks pass.",
    ),
    playbook="docs/DEPLOYMENT.md, PR 5b cutover",
    playbook_digest="7ef8d6d7e5939dce09f2dd52876fb8ae41c0af389fff5e2dbe8d9b1dcc394686",
)

BUNDLE_PINNING = Procedure(
    name="Bundle-pinning activation",
    when="Turning on policy-bundle pinning. The migration and provenance columns deploy rolling; "
         "the flag flip does not.",
    blocks_start=(
        "The rolling part is already deployed and the flag is still off.",
        "The policy bundle is seeded and read back successfully.",
        "The pinnable-backlog preflight is green: an unavailable historical bundle dead-letters.",
        "Every old pipeline worker can be stopped and attested at zero. A rolling flip lets peers "
        "resolve different bundles for the same run.",
    ),
    irreversible="Rollback stays ON THE PR6 IMAGE and uses the same drain, flag-off. Substituting "
                 "an earlier image mints permanent NULL provenance on rows decided under the flag.",
    rollback=(
        "Rollback stays on the PR 6 image and uses the same drain with the flag off.",
        "Substituting an earlier image mints permanent NULL provenance on every row decided while "
        "the flag was on.",
    ),
    playbook="docs/DEPLOYMENT.md, PR 6 cutover",
    playbook_digest="7d6ad3c1df2e92ce251244227448ea8fbc64160bc3442369773dd71a3b307f7c",
)

PR7B_CORE = Procedure(
    name="Migrations 013-023",
    # NOT "the ordering-authority schema" (re-audit `4f23f23..97deeae` finding 5). These revisions
    # give the tool local receipt and transition authority plus a best-effort local supersession
    # guard. Platform-wide ordering does not exist until 024, which is unbuilt — and a team that
    # read "ordering-authority schema" here could reasonably treat completing 023 as the
    # activation of ordered delivery and start trusting an order nothing provides.
    when="Moving onto the local receipt/transition-authority schema. It provides best-effort "
         "local supersession only; PLATFORM ordering authority remains absent until 024 is built "
         "and active, and 024 is pending.",
    blocks_start=(
        "Retention is suspended, every active retention task has exited, and you hold "
        "orchestrator evidence that zero are running.",
        "The pre-window diagnostic has run under that suspension and is green. It runs BEFORE any "
        "outage precisely so a missing authoritative callback is found while there is still time "
        "to restore it.",
        "An authoritative backup is available. On diagnostic failure the only paths are restoring "
        "the exact callback row or remaining on the prior revision in "
        "BLOCKED_NO_AUTHORITATIVE_MAPPING. Never fabricate a callback, delete a decision, or fall "
        "back to decided_at.",
        "Every writer role can be stopped and attested, including the API and the pipeline "
        "workers, not only the publishers.",
    ),
    # "018 and above refuse unconditionally" survived here after section 6 was corrected
    # (re-audit `4f23f23..122cc67` finding 5): 018-022 refuse, and 023 is validation-only, so
    # stepping down from head removes its stamp before 022 blocks descent. The boundary is the
    # same either way; the per-revision account was not.
    irreversible="YES, once any delivery witness exists. Migrations 018 through 022 each refuse "
                 "downgrade unconditionally in every environment, and 023 — validation-only — "
                 "drops its stamp and then hits 022. Above that boundary rollback means "
                 "redeploying a reviewed 023-COMPATIBLE image against the schema you are already "
                 "on; an older publisher lacks the receipt contract and must not run against "
                 "preserved evidence.",
    rollback=(
        "Above the 018 boundary the schema does NOT move. Rollback means redeploying a reviewed "
        "023-COMPATIBLE image against the schema you are already on.",
        "An older publisher lacks the receipt contract and must not run against preserved "
        "delivery evidence.",
        "Section 6 states the per-revision downgrade boundary; do not restate it from here.",
    ),
    playbook="docs/DEPLOYMENT.md, PR 7b-core cutover",
    playbook_digest="cfb8944983c416fd1052829c25f8c5f4c7400c677102a21d7a5706bf469c51d8",
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
                  "during an INBOUND rotation overlap. Each entry is a live verification "
                  "credential and carries the active key's floor: at least 32 characters, a "
                  "non-blank key id without surrounding whitespace, and no collision with the "
                  "active key id. A key id repeated in the raw JSON is refused at load: JSON "
                  "keeps only the last, so the earlier secret would vanish silently while the "
                  "peer was still signing with it. Both construction and the production boundary "
                  "refuse a violation. Unset it once the old key is retired. There is NO outbound "
                  "equivalent — the tool signs with one outbound key, so outbound overlap is held "
                  "on the platform's receiver.",
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
            id="OPS.CUTOVER.PROCEDURES",
            value=(FULL_WINDOW, BUNDLE_PINNING, PR7B_CORE),
            authority="docs/DEPLOYMENT.md cutover sections (referenced, never restated)",
            note="Plan from these entries; EXECUTE from the named playbook. Step summaries are "
                 "deliberately absent: a second copy of a safety procedure drifts, and every "
                 "step list we tried dropped a control that prevents irreversible damage.",
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
                "Migrations 018 through 022 each refuse downgrade UNCONDITIONALLY, in every "
                "environment including staging. There is no downgrade-freely rule anywhere.",
                "023 is the exception in form only: it is a validation-only revision, so stepping "
                "down from head removes its validation stamp and does nothing else — and 022 then "
                "blocks any further descent. The boundary holds; the per-revision account is just "
                "not 'everything at 018 and above raises'.",
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
