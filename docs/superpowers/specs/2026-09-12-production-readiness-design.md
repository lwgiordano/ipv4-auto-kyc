# TechCraft Production Readiness Design

**Status:** Authorized for staged implementation by the user's “okay do all that”
and subsequent “continue”. External inputs remain prerequisites where named.

**Implementation base:** PR #2, branch
`claude/project-setup-standing-rules-5w1fwh`

**Normative authority:** `KYC_Tool_Build_Package/machine_readable/*.json` wins
over prose. The package remains byte-unmodified. Recorded corrections in
`AUDIT_FINDINGS.md`, accepted architecture decisions, and the current migration
chain remain binding.

## 1. Goal

Turn the existing KYC/KYB engine and operations console into a production-ready
TechCraft integration without rewriting the decision engine or moving
platform-owned responsibilities into the tool.

Production-ready means all of the following are true:

1. TechCraft can generate or validate an integration client from a
   machine-readable contract.
2. Every configured production process boots without fixture-only providers or
   a selected `NotImplementedError` path.
3. TechCraft can prove event submission, idempotency, callback receipt,
   callback commitment, review actions, POC verification, and Salesforce
   projection behavior through an executable conformance suite.
4. Saved destination-field mappings are available through a stable public API
   and can be consumed by TechCraft without depending on `/ui/api`.
5. Platform-authoritative callback ordering is activated only after migration
   025's signed bootstrap reconciles the platform accepted-run ledger.
6. A real-provider staging rehearsal, load/soak run, and restore exercise meet
   explicit acceptance gates.
7. CI, local PostgreSQL testing, deployment documentation, and runbooks agree
   with the behavior being shipped.

## 2. Boundaries that do not change

- The platform remains the hub. Work starts only from platform events.
- The KYC tool gathers evidence, validates it deterministically, records checks,
  scores cases, and publishes decisions.
- The platform enforces decisions, owns customer/reviewer experiences, and owns
  the accepted-run ledger.
- Salesforce remains a one-way platform-owned mirror. The tool exposes a
  projection but never reads or writes Salesforce records.
- Checks remain immutable and supersedable.
- Existing runs remain bound to the configuration and policy bundle that
  created them.
- Website verification remains a human review task.
- Floqer remains discovery-only and can never award approval-grade points by
  itself.
- Positive enforcement remains off until the roadmap's M2 gate is satisfied.
- The frozen normative package and frozen migration revisions are not repaired
  in place.
- Existing authentication, authorization, evidence-containment, and audit
  controls are preserved even though this program is graded primarily on
  functionality.

## 3. Architectural shape

Keep the current modular monolith and its distinct process roles:

- API: accepts events and serves public/read/operations APIs.
- Pipeline worker: resolves inputs, runs adapters, validates evidence, writes
  checks, scores, and decides.
- Outbox publisher: delivers callbacks and POC email.
- Retention worker: applies governed retention.
- PostgreSQL: cases, events, runs, immutable evidence/check history, decisions,
  configuration revisions, queues, and outbox authority.
- S3-compatible object storage: uploads and raw adapter evidence.
- Operations console: human review, diagnosis, and live configuration.

Do not split these into microservices. The missing production value is at the
external interfaces, not in internal process decomposition.

## 4. Delivery units and dependency order

Each unit is independently reviewable and releasable. A unit receives a written
plan, RED-first tests, a task implementer, a separate task reviewer, parent
verification, and an adversarial whole-unit review before release.

### Unit 0 — Baseline and developer tooling

Scope:

- Verify that CI, handoff tooling, and new bus entries all target PR #2, now
  declared as the implementation base in `AGENT_BUS.md` during this design phase.
- Fix the current deployment-document migration-ownership invariant.
- Make local PostgreSQL discovery use `KYC_TEST_DATABASE_URL` first, then
  `pg_config --bindir`/`PATH`, then explicit fallback directories including
  Homebrew. Never silently skip database tests.
- Run the full product suite against PostgreSQL and preserve the existing
  import/lint/drift gates.
- Record current API/schema/provider baseline probes as executable tests rather
  than prose observations.

Acceptance:

- PR #2 CI is green.
- The full suite runs on CI and on this macOS checkout with PostgreSQL 16.
- A missing PostgreSQL installation produces one actionable error; a present
  Homebrew installation is discovered automatically.
- The repository and bus name the same implementation branch.

### Unit 1 — Machine-readable platform contract

Create one authoritative typed platform-event model:

- Nine event-specific envelope variants discriminated by `event_type`.
- Shared `occurred_at` and actor fields.
- Event-specific payload types drawn from the existing Pydantic models.
- Inbound payload extensions remain allowed where the accepted contract says
  they are allowed; envelope extensions remain refused.

The live endpoint continues authenticating the exact raw bytes, then validates
those same bytes through the authoritative union. The same union supplies the
OpenAPI request body and exported JSON Schema; the runtime parser and published
contract may not have separately authored field lists.

Publish:

- Request schema for `POST /v1/cases/{case_id}/events`.
- Path and the existing idempotency, timestamp, key-id, and signature headers.
  The current request protocol has no wire-version header; artifact versioning
  must not silently add a required request header or payload field.
- Exact response models for queued, replayed, rejected, and validation outcomes.
- Decision-callback JSON Schema from the authoritative callback encoder.
- A review-task-change contract. The default design is a separate platform
  webhook for task opened/completed/cancelled transitions; if TechCraft chooses
  governed polling instead, that choice and its cursor/freshness semantics must
  be an approved answer artifact rather than an undocumented substitution.
- Stable examples generated from typed fixtures.
- A versioned contract manifest containing artifact digests and the active wire
  version.

Acceptance:

- `/openapi.json` contains a non-null request body and every event variant.
- A contract test instantiates every published example and submits it through
  the real parser.
- Runtime-only or document-only additions fail a closed-set parity test.
- A generated client can identify every required header and response status
  without consulting prose.

### Unit 2 — Public Salesforce projection contract

Add:

`GET /v1/cases/{case_id}/salesforce-projection`

The endpoint is a read-only platform integration surface. It returns:

- `case_id`
- authoritative decision row/run identity, or an explicit unresolved state
- `mapping_revision`
- `configuration_revision`
- destination-keyed `fields`
- stable source identities for each field
- field nullability and type metadata
- projection timestamp

It calls the existing `project_salesforce_fields` function. `/ui/api` may render
the same projection, but it is not the integration authority. Mapping saves
alter subsequent projection reads only; they do not rewrite decisions, callback
bodies, old runs, or Salesforce records.

Recommended consumption model: TechCraft pulls this projection after an
enforced decision and after a material check notification. This avoids mutating
the immutable callback contract and makes retry/idempotency ownership explicit
on the platform side.

Acceptance:

- A saved mapping revision changes destination keys on the next public
  projection read.
- Historical decision bytes and existing run pins remain unchanged.
- Unknown/unresolved decision authority is represented explicitly and never
  projected as a fabricated value.
- A TechCraft fixture consumer idempotently upserts a Salesforce sandbox/mock by
  platform case id.
- No integration test consumes `/ui/api`.

### Unit 3 — Production provider profile

Complete the existing `real` provider profile. It is configuration, not a
second pipeline. Before enabling it, implement PR 9b's per-adapter output
models and `INVALID_RESPONSE` handling: malformed provider data is archived,
marks the run partial, and creates no check. Every canonical adapter must have
an explicit result; missing adapters may not be silently skipped.

Required capabilities:

| Capability | Production implementation |
|---|---|
| Object storage | Existing S3-compatible store |
| Platform email evidence | Existing event-backed adapter |
| Companies House | Existing HTTP adapter with production credentials |
| GLEIF | Existing HTTP adapter |
| RIR ORG-ID/RDAP | Existing live RIR strategies |
| RIR POC directory | New live directory over the selected RIR/RDAP data; hidden email creates the existing manual-review task |
| Document extraction | `platform_extracted_json_v1`, validating TechCraft-extracted fields stored in S3 |
| POC email | New SES sender unless TechCraft explicitly elects a platform-owned sender contract before this task begins |
| Floqer | Live client after the vendor contract is supplied; an explicitly approved disabled mode must report unavailable evidence and cannot count as full provider completion |
| Website | Existing human review task |
| Broker policy | Existing versioned local policy/configuration |

The document decision deliberately avoids adding Textract when the accepted
TechCraft flow already writes extracted JSON to shared storage. The production
provider validates content type, bounded size, schema, and required provenance;
it is not the permissive development `json_scan` selector under a new name.

Floqer is discovery-only but contributes the evidence for the deterministic
LinkedIn check. Disabling it reduces coverage; it is not equivalent to shipping
the original adapter catalog. Any reduced launch scope requires an explicit
recorded decision.

The existing roadmap recommends Textract while the platform integration guide
records platform-owned extraction. Resolve that contradiction with TechCraft
before selecting the production document engine; the JSON path above is a
proposal, not evidence that TechCraft has accepted a new production contract.

SES remains a candidate only if it meets PR 9c's provider idempotency and
ambiguous-success requirements. Carry the outbox identity through the sender
interface and verify actual provider semantics; an email header alone is not
proof of deduplication. Preserve hard deadlines, lease margins, overlap proofs,
bounded resources, and bounded shutdown.

Acceptance:

- Every process boots with `environment=production` and the `real`
  profile.
- No selected path raises `NotImplementedError`.
- One recorded-real fixture per provider passes normalization and validator
  boundaries.
- Upstream unavailable/timeout/rate-limit outcomes produce partial runs without
  false failing checks.
- A POC token is sent only to a live-directory RIR-listed email and completes a
  full verification round trip.
- Raw responses and extracted evidence land in the configured S3 namespaces.
- Live RIR strategies produce the relationship/conflict flags consumed by the
  validators, backed by recorded real examples per RIR.
- Readiness validates registered provider capabilities and required settings;
  arbitrary non-stub strings cannot certify an unwireable worker.

### Unit 4 — TechCraft conformance kit

Ship a CLI and reusable library that can run in two modes:

1. Offline contract mode: validates TechCraft-produced event/callback fixtures
   against the versioned schemas.
2. Staging mode: submits namespaced test events to the tool and observes a
   TechCraft test receiver.

The kit reports each obligation as `pass`, `fail`, `blocked`, or
`not_applicable`, with evidence. It covers:

- all event variants and exact response semantics
- queued versus idempotent replay behavior
- malformed/incomplete payloads
- manual approval and website review
- POC token delivery and verification
- callback commit-before-2xx
- duplicate callback delivery
- receiver outage and retry
- current pre-025 unordered-callback behavior
- post-025 ordered-callback behavior when activation is active
- review-task discovery or delivery, including task completion reflected back to
  the platform
- Salesforce projection retrieval and mapping revision adoption

The live mode requires a caller-supplied test namespace and refuses to operate
on unscoped production case ids.

Acceptance:

- A deliberately early-acknowledging receiver fails the commitment test.
- A receiver that dedupes only by arrival time fails the replay test.
- The repository's reference receiver passes every applicable test.
- Output is both human-readable and machine-readable JSON.
- The kit version is bound to the contract-manifest digest it tested.

### Unit 5 — Platform-answer artifact and ordering activation 025

This unit cannot begin implementation until TechCraft supplies the four pending
input classes already reserved in `docs/contracts/wire.py` and the roadmap:

- accepted-run ledger authority and query semantics
- automatic/manual/released effective-source representation
- signing principal and key-registry binding
- finite manifest/response limits and recovery/release-id rules

First ship the versioned answer-artifact schema and verifier described by
PR 7b-inputs. Only a verified artifact can satisfy the 025 planning gate.

Then implement the accepted 025 architecture without weakening it:

- machine-parsed process-role matrix
- immutable bootstrap artifact storage
- manifest export
- begin/record/activate CAS commands
- platform-signed bootstrap response
- high-water reconciliation
- runtime activation phase reader
- wire `decision_sequence`
- `integrity_mismatch` terminal behavior
- drained cutover and forward-only-after-use recovery

Acceptance:

- Content screens alone cannot resolve a platform obligation.
- A complete, signed answer artifact is required before migration-plan tests can
  turn green.
- The platform and tool reconcile every included case before activation.
- Duplicate, delayed, and reversed deliveries cannot replace a newer accepted
  decision.
- Manual approval remains effective until the governed release protocol returns
  automatic authority.
- Interrupted bootstrap and activation paths follow a tested recovery matrix.

### Unit 6 — Remaining roadmap prerequisites

The production program must also complete the existing ordered migration chain:
PR 6b / 026 (revalidate old checks under changed validators), PR 7a / 027 (durable
lease token and single-runner backstop), PR 8 / 028 (structured object references
and immutable source evidence), and PR 10b / 029 (numeric CHECKs, fleet cutover
attestation, and its remaining operations scope). Preserve their reserved order.
Provider development can use fixtures before 028, but production document
activation depends on immutable source-evidence adoption.

PR 10b also owns the contracted rollout-observation endpoint, external-call
authority/executor residuals, typed operations shape contracts, and the explicit
evidence-refresh extension. Reconcile existing implementations before planning
each item; do not rebuild a capability already proved present. The full M4
backlog, not only 025, remains a prerequisite for M2.

### Unit 7 — Real staging, capacity, and cutover proof

Run the complete system against TechCraft staging and the selected providers.
The scenario matrix includes:

- approve
- approve with buying locked
- insufficient evidence/manual review
- blocked-broker reject
- manual approval
- website pass and fail
- POC email unavailable/manual fallback
- POC delivery and verification
- duplicate events
- worker crash/reclaim
- provider timeout/rate limit/outage
- callback outage and recovery
- Salesforce sandbox projection adoption
- configuration change with older pinned runs still active
- ordering bootstrap and activation

Add a load harness that distinguishes adapter-light from full runs. Record event
ingestion, queue delay, adapter latency, event-to-decision latency, callback
latency, queue depth, retry counts, and dead-letter counts.

The default latency acceptance inherited from the build package is:

- adapter-light p95 below 10 seconds
- full-run p95 below 120 seconds, excluding human/POC wait time

Concurrency and daily-volume acceptance are finite signed deployment inputs
from TechCraft. The harness is parameterized; production sign-off refuses while
those values are absent.

Complete:

- normal and peak load runs
- provider degradation run
- callback outage run
- worker restart run
- agreed-duration soak run
- backup/restore rehearsal
- interrupted cutover rehearsal

Acceptance:

- Every scenario is visible in the platform, tool audit history, and Salesforce
  sandbox where applicable.
- No scenario depends on database hand-editing.
- Agreed latency and capacity targets pass.
- Dead-letter and backlog alerts fire and recover as documented.
- The tested image digest, schema head, configuration revision, contract digest,
  and provider profile are recorded together.
- The runbook has been executed against staging, not merely reviewed.
- CI tests the locked deployment dependencies and builds/boots the actual image.
- A full database plus object-store restore proves their consistency and records
  recovery time/data loss against agreed RTO/RPO; a single-row repair is not a
  disaster-recovery drill.

## 5. TechCraft-owned inputs

TechCraft supplies these through governed configuration or signed artifacts, not
chat text or committed secrets:

1. Staging and production callback base URLs.
2. Key ids and key-registry/signing ownership for each wire direction.
3. Accepted-run ledger schema and effective-source semantics.
4. Shared S3 bucket/key conventions and IAM ownership.
5. Salesforce sandbox access and the platform service that consumes the public
   projection.
6. Review-task integration choice: the default task-change webhook, or an
   explicit polling contract with cursor, freshness, and missed-poll recovery.
7. Confirmation that document extraction is platform-owned; otherwise an
   explicit choice of OCR provider replaces `platform_extracted_json_v1` in a
   separately approved provider task.
8. Confirmation that SES is tool-owned POC delivery; otherwise a typed
   platform-owned delivery contract replaces it.
9. Finite expected daily volume, peak concurrency, soak duration, and recovery
   objectives.

The repository work can proceed through Units 0, 1, 2, and the offline portion
of Unit 4 while these inputs are collected. Unit 3 can build its default profile
with recorded fixtures, but real staging credentials remain a deployment gate.
The answer-artifact verifier is developed before it can accept an artifact;
025 itself cannot be planned or coded before the verified inputs and process-role
matrix satisfy the existing gate.

## 6. Documentation authority

The current sentence "Per-case order is preserved" in
`docs/PLATFORM_INTEGRATION.md` is false before activation 025. Unit 0 replaces it
with the current interim receiver rule and points to the authoritative contract.

The TechCraft handoff must distinguish:

- implemented now
- implemented but not activated
- awaiting TechCraft configuration
- awaiting TechCraft contract decisions
- deliberately out of scope

No PDF or deployment guide may call the service production-ready until the
machine-readable contract, provider profile, 025 state, and staging evidence all
agree.

## 7. Test and audit strategy

Every behavior change follows RED → GREEN → REFACTOR:

- A regression first reproduces the missing or contradictory behavior.
- The test must fail for the intended reason before production code changes.
- The smallest implementation makes it pass.
- Focused tests run first, then full PostgreSQL tests, lint, import boundaries,
  drift guards, and `git diff --check`.

Each delivery unit receives:

1. One implementation subagent per independently reviewable plan task.
2. A separate task reviewer checking specification compliance and code quality.
3. Parent verification of the diff and complete gates.
4. A whole-unit adversarial reviewer.
5. A range-anchored bus RELEASE and independent Claude/Codex re-audit.

No agent report substitutes for direct parent verification. Current user-owned
untracked files remain untouched.

## 8. Production go/no-go gate

Production is **NO-GO** if any item below is false:

- PR CI and full PostgreSQL suite are green.
- A fresh database migrates to the declared production head.
- Every production process boots with no fixture-selected capability.
- All event variants pass the TechCraft conformance suite.
- The platform receiver commits before returning 2xx and passes replay tests.
- POC verification works end to end with the selected live directory and sender.
- Salesforce mapping changes reach the sandbox through the public projection.
- Ordering 025 is bootstrapped and active for production automatic decisions.
- The full M4 backlog and platform cutover are complete before enabling M2.
  A separately labelled human-review pilot keeps positive enforcement off and
  does not count as completion of this production program.
- Real-provider staging and the signed capacity targets pass.
- Restore and interrupted-cutover rehearsals pass.
- Executable contracts, public schemas, deployed behavior, and handoff documents
  agree.

## 9. Explicit non-goals

- Replacing the console with TechCraft's platform UI.
- Letting this tool write Salesforce.
- Automated website judgment.
- Fuzzy broker matching or new risk decisions.
- New paid enrichment sources beyond an approved Floqer contract.
- Microservice decomposition.
- Changing historical decisions when configuration or mappings change.
- Enabling M2 before all existing roadmap prerequisites are met.
