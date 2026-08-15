"""Deployment, cutover, rollback, and activation obligations.

Same discipline as `wire.py`: literal claims, validated against independent authority by
`tests/unit/test_contract_registry_authority.py`. Step sequences that ARE stored here are stored
as ORDERED tuples so a test can assert both membership and order — a reordered prerequisite is a
real defect (attesting zero publishers before stopping them proves nothing), and a token-presence
check would miss it.

The blocker at the top is the reason this document is titled a staging guide.
"""

from dataclasses import dataclass, field
from pathlib import Path

from docs.contracts import Claim, ClaimState, Registry, plan
from docs.contracts.plan import (
    COMMIT_PAUSE_BUFFER,
    COMMIT_RESIGN_RETRIES,
    PHASE_FLAG_ACTIVATION,
    PHASE_SCHEMA_MAINTENANCE,
    PHASE_SECURITY_FULL_WINDOW,
    PLAN_ROLES,
    AuthoritativeBackup,
    BacklogPreflight,
    FlagStillOff,
    PinnedImage,
    PlatformWrittenCommitment,
    PreWindowDiagnostic,
    ProcedurePlanContract,
    RetentionSuspendedAttested,
    SeedAndReadBack,
    StoppedAttestedZero,
    TrustedPathProbes,
)
from docs.contracts.playbook import (
    BR_SCHEMA_HELD,
    BR_SCHEMA_WALKED,
    ENDING,
    ENDING_RESUMED,
    ENDING_RESUMED_OR_DECLARED_INCIDENT,
    IMAGE,
    IMAGE_CONDITIONAL,
    IMAGE_PRIOR,
    IMAGE_SAME_RELEASE,
    IRREVERSIBLE_PAST_BOUNDARY,
    OUTCOME_REFUSED,
    OUTCOME_SUCCEEDED,
    RESTORES,
    RESTORES_NO,
    RESTORES_YES,
    REVERSIBILITY,
    REVERSIBLE_WITH_CONDITIONS,
    SCHEMA,
    SCHEMA_CONDITIONAL,
    SCHEMA_STAYS,
    VERIFICATION,
    VERIFY_NON_MUTATING,
    VERIFY_NOT_STATED,
    WINDOW,
    WINDOW_SAME,
    Command,
    MigrationSpan,
    PlaybookRef,
    RollbackBranch,
    RollbackContract,
    RollbackFact,
)

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


def _resolved_migration_range(span: MigrationSpan) -> tuple[str, ...]:
    """The ordered revisions a span installs, read off Alembic's own graph.

    Ascending, EXCLUSIVE of the base (the base is where you already are), inclusive of the
    target. Alembic raises for an unbuilt endpoint or a pair that is not ancestor/descendant, so
    a span cannot name a walk the graph does not contain.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    repo = Path(__file__).resolve().parents[2]
    cfg = Config(str(repo / "alembic.ini"))
    declared = cfg.get_main_option("script_location")
    if declared and not Path(declared).is_absolute():
        cfg.set_main_option("script_location", str((repo / declared).resolve()))
    script = ScriptDirectory.from_config(cfg)
    walk = list(script.walk_revisions(base=span.base_revision, head=span.target_revision))
    ordered = tuple(r.revision for r in reversed(walk) if r.revision != span.base_revision)
    if not ordered or ordered[-1] != span.target_revision:
        raise ValueError(f"span {span.base_revision}->{span.target_revision} resolved to nothing")
    return ordered


def _span_display_name(resolved: tuple[str, ...]) -> str:
    return f"Migrations {resolved[0]}-{resolved[-1]}"


@dataclass(frozen=True)
class Procedure:
    """A referenced (not restated) operational procedure.

    `playbook_ref` is what makes the reference load-bearing (re-audit `4f23f23..97deeae` finding
    8, tightened by Wave 1 / F7). Asserting that a heading EXISTS proves nothing about the body
    under it, and the first digest — taken over a whitespace-COLLAPSED body — proved less than it
    claimed: a shell continuation rewritten from backslash-newline to backslash-space, which
    breaks the command, hashed identically. The ref now binds the exact heading LINE (full-line
    equality, unique) and the exact section BYTES (CRLF→LF the only normalization), and carries
    typed `Command` records that an independent parse of the section's backtick spans must
    reproduce argv-for-argv — so a command edit is caught even if someone re-pins the digest over
    it. Re-pinning is still the act of re-review; it is just no longer the only witness.
    `playbook`, the printed pointer, is DERIVED from the ref.

    `rollback` and `irreversible` are DERIVED, not authored (re-audit `4c3015a..eaa3f8f`, the gap I
    declared open when the digest first landed). They used to be hand-written prose sitting beside
    the digest with nothing tying them to it, so rewriting the rollback text to say "redeploy the
    previous image" — the one thing PR 6's playbook calls a defect — left the digest matching and
    every test green. Both now come from `rollback_contract`, whose answers are drawn from a closed
    domain, whose sentences belong to the answers, and each of whose answers quotes the playbook
    sentence it was read from. Passing either field explicitly is refused below: there is no
    free-text rollback field left to mutate.

    `migration_range` is what THIS cutover installs, DERIVED (Wave 1 / F8) by walking Alembic's
    revision graph from `migration_span.base_revision` to its target — the authored tuple it
    replaces was bound only by a subset check, so dropping 013-017 under the unchanged name
    "Migrations 013-023" passed every guard. The visible name must equal the resolved span or
    construction refuses, the release verifier recomputes the walk independently and requires
    exact ordered equality, and the range remains the executable authority for the schema answer
    (each revision's downgrade AST is classified, never trusted). PR 6's flag flip installs
    nothing (its migration deployed earlier, rolling), which is why it carries no span.
    """

    name: str
    plan: ProcedurePlanContract
    playbook_ref: PlaybookRef
    rollback_contract: RollbackContract
    migration_span: MigrationSpan | None = None
    migration_range: tuple[str, ...] = field(default=(), init=False)
    playbook: str = field(default="", init=False)
    when: str = field(default="", init=False)
    blocks_start: tuple[str, ...] = field(default=(), init=False)
    rollback: tuple[str, ...] = field(default=(), init=False)
    irreversible: str = field(default="", init=False)

    def __post_init__(self) -> None:
        # The reversibility answer is what the page prints under "Reversible?"; the remaining
        # answers are the rollback conditions. Splitting them here keeps the page from printing
        # the same derived sentence twice, and `init=False` means neither can be passed in.
        statements = self.rollback_contract.statements()
        object.__setattr__(self, "irreversible", statements[0])
        object.__setattr__(self, "rollback", statements[1:])
        # The page prints the ref's own display — path plus the exact heading's visible text —
        # so the pointer a reader follows and the pointer the digest binds are the same object.
        object.__setattr__(self, "playbook", self.playbook_ref.display)
        # `when` and `blocks_start` are the PLAN's derivations (Wave 1 / F4a): the phase owns the
        # template and the required prerequisite kinds, each kind owns its sentence, and the four
        # bundle-activation inversions Codex ran are unconstructable rather than detectable.
        object.__setattr__(self, "when", self.plan.when())
        object.__setattr__(self, "blocks_start", self.plan.blocks_start())
        # Phase and migration span must agree: a security window carries no migration by its own
        # published rationale, and a schema maintenance IS its migrations.
        if self.plan.phase == PHASE_SECURITY_FULL_WINDOW and self.migration_span is not None:
            raise ValueError("a security full-window procedure claims to carry no migration")
        if self.plan.phase == PHASE_FLAG_ACTIVATION and self.migration_span is not None:
            raise ValueError("a flag activation installs nothing; its migration deployed rolling")
        if self.plan.phase == PHASE_SCHEMA_MAINTENANCE and self.migration_span is None:
            raise ValueError("a schema maintenance without a migration span is not one")
        if self.migration_span is not None:
            resolved = _resolved_migration_range(self.migration_span)
            object.__setattr__(self, "migration_range", resolved)
            # The visible name must BE the resolved span. F8's mutation kept "Migrations 013-023"
            # while the authored range silently dropped 013-017; with the range derived and the
            # name checked against it, the pair cannot disagree and still construct.
            if self.name != _span_display_name(resolved):
                raise ValueError(
                    f"procedure name {self.name!r} disagrees with its resolved span "
                    f"{_span_display_name(resolved)!r}"
                )


FULL_WINDOW = Procedure(
    # Scoped to PR 5b (re-audit `4f23f23..97deeae` finding 8). It read "any release the notes
    # classify as full-maintenance" while pointing at PR5b-specific steps and a PR5b-specific
    # rollback, so a future unrelated full-window release would inherit the wrong procedure.
    name="PR 5b full maintenance window",
    plan=ProcedurePlanContract(
        phase=PHASE_SECURITY_FULL_WINDOW,
        subject_id=plan.SUBJECT_PR5B,
        prerequisites=(
            PinnedImage(evidence="is pinned to that one digest",
                        recovery_module_only_in_new=True),
            PlatformWrittenCommitment(
                commitments=(COMMIT_PAUSE_BUFFER, COMMIT_RESIGN_RETRIES),
                evidence="pause/buffer all event submission"),
            TrustedPathProbes(evidence="trusted path"),
        ),
    ),
    rollback_contract=RollbackContract((
        RollbackFact(REVERSIBILITY, REVERSIBLE_WITH_CONDITIONS,
                     "pins the recovery one-shot to the last image that still contains it"),
        RollbackFact(SCHEMA, SCHEMA_STAYS, "No migration ships with this change"),
        RollbackFact(IMAGE, IMAGE_PRIOR, "redeploy the prior image for API and workers"),
        RollbackFact(RESTORES, RESTORES_YES,
                     "accepting that the prior image restores pre-PR-5b behavior"),
        RollbackFact(VERIFICATION, VERIFY_NON_MUTATING,
                     "Rollback verification is non-mutating only"),
        RollbackFact(WINDOW, WINDOW_SAME, "Rollback mirrors the same window"),
        RollbackFact(ENDING, ENDING_RESUMED, "verify, start workers, resume"),
    )),
    playbook_ref=PlaybookRef(
        path="docs/DEPLOYMENT.md",
        heading="## 9. PR 5b cutover — brief full maintenance window",
        sha256="8f3c7769f696ecb647b00fa550dda3a1d8e10d355f0fb3e7305ed5414e3c8dc8",
        commands=(
            Command(("python", "-m", "kyc_tool.ops.requeue_interrupted_jobs")),
        ),
    ),
)

BUNDLE_PINNING = Procedure(
    name="Bundle-pinning activation",
    plan=ProcedurePlanContract(
        phase=PHASE_FLAG_ACTIVATION,
        subject_id=plan.SUBJECT_BUNDLE_PINNING,
        prerequisites=(
            FlagStillOff(flag_env="KYC_ENFORCE_BUNDLE_PINNING",
                         evidence="KYC_ENFORCE_BUNDLE_PINNING=false"),
            SeedAndReadBack(evidence="seed and read back the on-disk policy bundle"),
            BacklogPreflight(evidence="A nonzero exit blocks the cutover"),
            StoppedAttestedZero(roles=("pipeline_worker",),
                                evidence="confirm zero old workers"),
        ),
    ),
    rollback_contract=RollbackContract((
        RollbackFact(REVERSIBILITY, REVERSIBLE_WITH_CONDITIONS,
                     "mint permanent NULLs — a defect, not a safe fallback"),
        RollbackFact(SCHEMA, SCHEMA_STAYS, "flag-only, no data migration"),
        RollbackFact(IMAGE, IMAGE_SAME_RELEASE, "disabling the flag on the PR6 image"),
        RollbackFact(RESTORES, RESTORES_NO, "never means resuming on a pre-PR6 image"),
        # The PR 6 section restricts nothing about rollback verification. Publishing that silence
        # is the honest answer; inventing a restriction here would be authoring the playbook from
        # the document that is supposed to reference it.
        RollbackFact(VERIFICATION, VERIFY_NOT_STATED),
        RollbackFact(WINDOW, WINDOW_SAME, "via the same drained shape"),
        RollbackFact(ENDING, ENDING_RESUMED,
                     "start workers with KYC_ENFORCE_BUNDLE_PINNING=false"),
    )),
    playbook_ref=PlaybookRef(
        path="docs/DEPLOYMENT.md",
        heading="## 10. PR 6 cutover — bundle-pinning activation",
        sha256="487cfedfdf2cd670b633ff09cdb774e3802406d0b56f425ba9f49a7324f106f4",
        commands=(
            Command(("python", "-m", "kyc_tool.ops.seed_policy_bundle",
                     "--expect-hash", "<sha256>")),
            Command(("python", "-m", "kyc_tool.ops.verify_pinnable_backlog")),
            Command(("python", "-m", "kyc_tool.ops.requeue_interrupted_jobs")),
            Command(("python", "-m", "kyc_tool.ops.activate_bundle_pinning_epoch",
                     "--expect-bundle-hash", "<sha256>", "--expect-engine", "eng-1")),
        ),
    ),
)

PR7B_CORE = Procedure(
    name="Migrations 013-023",
    # NOT "the ordering-authority schema" (re-audit `4f23f23..97deeae` finding 5). These revisions
    # give the tool local receipt and transition authority plus a best-effort local supersession
    # guard. Platform-wide ordering does not exist until 024, which is unbuilt — and a team that
    # read "ordering-authority schema" here could reasonably treat completing 023 as the
    # activation of ordered delivery and start trusting an order nothing provides.
    plan=ProcedurePlanContract(
        phase=PHASE_SCHEMA_MAINTENANCE,
        subject_id=plan.SUBJECT_PR7B_CORE,
        prerequisites=(
            RetentionSuspendedAttested(evidence="retention stays suspended"),
            PreWindowDiagnostic(evidence="the pre-window diagnostic is an early detector"),
            AuthoritativeBackup(evidence="Backup availability is an operator prerequisite"),
            StoppedAttestedZero(
                roles=PLAN_ROLES,
                evidence="Hard-stop and orchestrator-attest zero API, pipeline, outbox"),
        ),
    ),
    # An earlier hand-written "018 and above refuse unconditionally" lived here and was wrong about
    # 023 (re-audit `4f23f23..122cc67` finding 5). That whole class of defect is now structurally
    # impossible in this field: the schema answer is CHOSEN from three options and RECOMPUTED by
    # the tests from every downgrade body in `migration_range`, so a per-revision account cannot
    # drift because there is no per-revision account here to drift. Section 6 still carries it, and
    # is still checked against the migrations.
    migration_span=MigrationSpan(base_revision="012", target_revision="023"),
    rollback_contract=RollbackContract((
        RollbackFact(REVERSIBILITY, IRREVERSIBLE_PAST_BOUNDARY,
                     "018 through 022 refuse unconditionally"),
        RollbackFact(SCHEMA, SCHEMA_CONDITIONAL,
                     "With 018 or anything above it installed there is no schema-downgrade path"),
        RollbackFact(IMAGE, IMAGE_CONDITIONAL,
                     "Rollback after first witness use is a FLAG/IMAGE rollback on the compatible "
                     "schema, never a schema downgrade"),
        RollbackFact(RESTORES, RESTORES_NO, "PROHIBIT the pre-7b image outright"),
        RollbackFact(VERIFICATION, VERIFY_NOT_STATED),
        RollbackFact(WINDOW, WINDOW_SAME, "as drained as the forward cutover"),
        # Totality surfaced this one: three hand-written rollback lines published the image rule
        # and the boundary, and omitted the control this playbook puts in capitals.
        RollbackFact(ENDING, ENDING_RESUMED,
                     "never leave the system stopped or retention frozen"),
    ), branches=(
        # The two sides of the fork the aggregate CONDITIONAL answers name (Wave 1 / F9). Each
        # branch's evidence must sit inside ITS OWN span of the playbook — R5 for refusal, R6 for
        # success — so outcome-B evidence can no longer justify outcome-A's image policy.
        RollbackBranch(
            outcome=OUTCOME_REFUSED,
            facts=(
                RollbackFact(SCHEMA, BR_SCHEMA_HELD,
                             "the DB stays on the witness-authority schema"),
                RollbackFact(IMAGE, IMAGE_SAME_RELEASE,
                             "KEEP or redeploy the reviewed 023-COMPATIBLE image"),
                RollbackFact(RESTORES, RESTORES_NO, "PROHIBIT the pre-7b image outright"),
                RollbackFact(VERIFICATION, VERIFY_NOT_STATED),
                RollbackFact(ENDING, ENDING_RESUMED_OR_DECLARED_INCIDENT,
                             "Do not end stopped"),
            ),
        ),
        RollbackBranch(
            outcome=OUTCOME_SUCCEEDED,
            facts=(
                RollbackFact(SCHEMA, BR_SCHEMA_WALKED, "downgrade SUCCEEDED"),
                RollbackFact(IMAGE, IMAGE_PRIOR, "deploy the recorded prior-image digest"),
                RollbackFact(RESTORES, RESTORES_NO,
                             "Redeploying the pre-7b image BEFORE 013 is applied is also safe"),
                RollbackFact(VERIFICATION, VERIFY_NOT_STATED),
                RollbackFact(ENDING, ENDING_RESUMED,
                             "re-enable retention, autoscaling/restarts, and submissions and "
                             "remove the composer edge block"),
            ),
        ),
    )),
    playbook_ref=PlaybookRef(
        path="docs/DEPLOYMENT.md",
        heading="## 11. PR 7b-core cutover — drained maintenance window (migration 013)",
        sha256="1a56e982c41690ad42fa0170f21a24a5124e377a0df6e24492bf1d71be38b6a6",
        # Every operator-run command the section publishes, as parsed argv. The placeholders
        # (`<file.json>`, `<id>`, `<sha256>`) are the section's own literal text.
        commands=(
            Command(("python", "-m", "kyc_tool.ops.verify_pr7b_core_backfill")),
            Command(("python", "-m", "kyc_tool.ops.verify_pr7b_ops_prerequisites",
                     "--expect-revision", "012")),
            Command(("python", "-m", "kyc_tool.ops.restore_pr7b_core_callback",
                     "--evidence", "<file.json>", "--expect-original-id", "<id>",
                     "--expect-manifest-digest", "<sha256>")),
            Command(("sha256sum", "<file.json>")),
            Command(("python", "-m", "kyc_tool.ops.repair_outbox_sequence")),
            Command(("python", "-m", "kyc_tool.ops.requeue_interrupted_jobs")),
            Command(("python", "-m", "alembic", "-c", "alembic.ini", "upgrade", "head")),
            Command(("python", "-m", "kyc_tool.ops.reset_interrupted_outbox_claims")),
            Command(("python", "-m", "alembic", "-c", "alembic.ini", "downgrade", "012")),
        ),
    ),
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


def procedure_projection(procedure: Procedure) -> tuple:
    """The COMPLETE structured projection of one procedure — every operational fact it publishes,
    in one canonical tuple (re-audit `1826661..b5c7a83` finding 3).

    The cooperating typed parts (plan, ref, contract, profile) could previously be swapped or
    weakened ONE AT A TIME: a transplanted plan, a deleted prerequisite parameter, a swapped
    branch ending, an imperative slipped into the subject map, an irrelevant-but-unique evidence
    quote — each left every focused bind green. The release verifier hashes THIS projection and
    holds it to a reviewed per-procedure pin, so any divergence anywhere in the definition is one
    located failure, and changing a definition is a re-pin — the act of review. No English is
    parsed anywhere in it.
    """
    plan_contract = procedure.plan
    prerequisites = tuple(
        (type(p).__name__,
         tuple((f.name, getattr(p, f.name)) for f in _dataclass_fields(p)))
        for p in plan_contract.prerequisites
    )
    contract = procedure.rollback_contract
    aggregate = tuple((f.question, f.answer, f.evidence) for f in contract.facts)
    branches = tuple(
        (b.outcome, b.span_marker, tuple((f.question, f.answer, f.evidence) for f in b.facts))
        for b in contract.branches
    )
    span = procedure.migration_span
    return (
        procedure.name,
        plan_contract.phase,
        plan_contract.subject_id,
        plan_contract.subject,
        procedure.playbook_ref.path,
        procedure.playbook_ref.heading,
        tuple(c.line for c in procedure.playbook_ref.commands),
        (span.base_revision, span.target_revision) if span is not None else None,
        prerequisites,
        aggregate,
        branches,
    )


def _dataclass_fields(value):
    from dataclasses import fields

    return fields(value)
