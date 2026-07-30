# KYC Tool Build Package — Audit Findings

Audit of `KYC_Tool_Build_Package/` (v2, platform-driven), performed before implementation
per `00_AGENT_BRIEF.md`: *"The JSON files are normative; if prose and JSON ever disagree,
the JSON wins and you must flag the discrepancy."*

The package is committed **unmodified**. Every correction below is encoded in code and
test fixtures only, each tagged with its finding ID (e.g. `AUDIT:B1`).

Severity: 🔴 would produce wrong behavior if built naively · 🟡 ambiguity needing a
documented choice · 🔵 hygiene/wording.

---

## A. Prose ↔ JSON conflicts (JSON wins, per the brief)

### 🔴 A1 — Decision table: prose row conditions don't cover the input space
- `02_SCORING_AND_DECISIONS.md §3` defines `manual_review_insufficient` as
  "Score < 100 AND no hard block".
- `machine_readable/decision_policy.json` defines it as
  "otherwise (score < 100 **or a non-broker gate failing**, with no hard block)".
- A case with score ≥ 100 but a failing legal-proof, control-proof, or no-hard-conflict
  gate matches **no row** of the prose table. Golden case **G6** (hard conflict at high
  score → stays `manual_review_insufficient`) is only satisfiable under the JSON
  semantics.
- **Resolution**: the decision engine implements the JSON: priority-ordered rules with
  `manual_review_insufficient` as the total-function catch-all.

### 🔴 A2 — "Four hard gates" is wrong: there are five
- `00_AGENT_BRIEF.md` (mission #5) and `01_ARCHITECTURE.md §2.8` say "four hard gates".
- Five gates appear in every normative artifact: `scoring_rubric.json.hard_gates`
  (5 keys), the decision-callback schema (5 booleans: `score_met`, `legal_proof`,
  `control_proof`, `broker_ok`, `no_hard_conflict`), `07_TEST_PLAN.md`
  ("all 2^5 combinations"), and the PDF (5 gate chips).
- **Resolution**: five gates are built. The word "four" is treated as a prose typo.

### 🟡 A3 — Adapter identity/granularity mismatch
- Prose `03_ADAPTERS_AND_EVIDENCE.md` says "the eight adapters", groups all registries
  into one adapter, and names RIR adapters `arin_rdap`, `ripe`, `apnic_rdap`,
  `lacnic_rdap`, `afrinic_rdap`.
- `machine_readable/adapter_catalog.json` (normative) has **nine** entries:
  `companies_house` and `gleif` are separate, and there is a **single** `rir_rdap`
  adapter covering all five RIRs.
- **Resolution**: the catalog's nine `adapter_id`s are canonical. `rir_rdap` is one
  adapter module with five per-RIR strategy classes ("per-RIR quirks isolated",
  per Phase 3).

### 🟡 A4 — Ghost registry source
- `scoring_rubric.json` lists `state_country_registry` as an `official_registry_match`
  source; no such adapter exists in `adapter_catalog.json`.
- **Resolution**: v1 registry evidence comes from Companies House and GLEIF only.
  State/country registries are a future adapter (additive: new adapter, same check type).

### 🟡 A5 — `approved_manual` has no Salesforce representation
- `state_machine.json` includes case status `approved_manual`;
  `salesforce_sync_fields.json.KYC_Status__c` has no corresponding value.
- **Resolution** (for the platform-team mapping doc): `approved_manual` maps to
  `KYC_Status__c = "Account Approved"` + `Platform_Action_Taken__c = "Manual Approve"`
  + `Manual_Approved_By__c` / `Manual_Approved_At__c`.

### 🔵 A6 — Callback delivery wording
- `01_ARCHITECTURE.md §4.9` says "exactly-once semantics via event IDs";
  `04_API_AND_DATA_MODEL.md §2` says at-least-once with platform dedupe on
  (`case_id`, `run_id`).
- **Resolution**: at-least-once delivery from a transactional outbox; consumers dedupe.
  No component claims exactly-once. **PR 7b-core exception:** eligible non-superseded
  callbacks remain at-least-once and platform-deduped; a callback proven obsolete by a
  higher **locally-stamped** delivery is terminally suppressed (zero sends), audited, and
  retained under the governed `superseded` lifecycle (decision callbacks ONLY — a
  `poc_email` can never enter `superseded`; DB-enforced). This is a **best-effort local
  suppression** — NOT exactly-once and NOT platform-authoritative. THREE residual reverts
  remain until 7b-activation: send-before-stamp; cross-replica; and a queued automatic
  callback delivered AFTER a later manual approval (manual rows carry `run_id NULL`, no
  callback, and no sequence, so the local guard sees no higher locally-published
  automatic sequence).

## B. Spec bugs

### 🔴 B1 — Golden case G7 cannot cross the threshold as written
- G7: G3 state (verified_email +10, website +10 = 20) → `org_id.submitted` (+25) →
  `document.uploaded` (+25) → registry match (+25) = **95 < 100**. The case never
  reaches the threshold, yet G7 expects it to cross 100. Its expected
  "`approve_buy_locked` → `approve`" tail also contradicts the ORG-ID check having
  passed mid-sequence (with ORG-ID passed, the first threshold crossing goes straight
  to `approve`).
- **Resolution** (`AUDIT:B1` in fixtures): the golden fixture adds an `email.verified`
  company-domain event (+25 → 120) and splits the case:
  - **G7a** — evidence crosses 100 *without* ORG-ID → `approve_buy_locked`
  - **G7b** — `org_id.submitted` then passes → `approve` (platform enables buying)
  This preserves G7's intent: the dynamic loop auto-clears `manual_review_insufficient`
  and exercises the buy-lock upgrade path.

### 🟡 B2 — Golden case G2 arithmetic (acknowledged in the doc)
- G1 minus ORG-ID = 90 < 100; the doc says to construct with document +25 instead.
- **Resolution**: G2 fixture = company email (25+10) + registry (25) + website (10) +
  LinkedIn (20) + document (25) = 115, no ORG-ID → `approve_buy_locked`.

### 🔴 B3 — `hard_block` is used but never defined
- The reject condition is "exact_match_blocked_broker OR **hard_block**" in both prose
  and `decision_policy.json`, but no document defines `hard_block`. G6 proves a hard
  *conflict* is **not** a hard block (it yields `manual_review_insufficient`, not
  `reject`).
- **Resolution**: in v1, `reject` fires **only** on an exact blocked-broker match.
  The decision engine's `RejectReason` enum is extensible so a future `hard_block`
  source is additive. Flagged to the spec owner for definition.

### 🔵 B4 — "Only all-pass approves" understates the decision inputs
- `07_TEST_PLAN.md` says gates are 2^5 and "only all-pass approves", but approval also
  branches on a sixth input: whether a live ORG-ID check passed (`approve` vs
  `approve_buy_locked`).
- **Resolution**: the gate matrix test runs 2^5 gate combinations × org_id ∈
  {passed, not-passed}: all-pass × passed → `approve`; all-pass × not-passed →
  `approve_buy_locked`; anything else → never an approval.

## C. Under-specifications (stubbed behind interfaces, `TODO(integration)`)

### 🟡 C1 — No `registration` event exists
- The brief and `01 §2.1` list *registration* as a lifecycle event that invokes the
  tool; the event enum (9 types) and `platform_events.json` contain no such event, and
  SF statuses "Registered"/"Email Verification Pending" precede any tool involvement.
- **v1 behavior**: the tool creates a case lazily on the first event received for a
  `case_id`. Whether the platform will send a registration-time event is an open
  question for the platform team.

### 🟡 C2 — POC token round-trip path unspecified — RESOLVED (PR 4)
- The tool stores `token_hash`; the `poc.token_verified` payload carries "token id".
  Nothing said who hosts the confirmation link or how the raw token is validated.
- **v1 behavior**: token emails link to the **platform**; the platform posts
  `poc.token_verified` including the raw token; the tool validates hash + expiry.
  The email sender and link format sit behind interfaces.
- **PR 4 hardening**: the verification email now carries the minted token's id as a
  "Verification reference"; the platform echoes it back as `token_id`, so the tool
  matches the exact minted row by **`id` + `digest`** (both required). The raw token
  is scrubbed to its digest at **ingestion** (`events.ingest._scrub_secrets`) — the
  events table never stores it — and the idempotency `payload_hash` is taken from the
  original envelope, so replay/409 detection is unaffected. See D7 for the binding +
  single-use rules the validator then enforces.

### 🟡 C3 — Manual-approve callback semantics
- The decision enum has exactly four values; none represents manual approval, and the
  platform has already enforced it before the tool hears about it.
- **v1 behavior**: `reviewer.manual_approve` produces **no decision callback**. The tool
  records the audit row, sets case status `approved_manual`, recomputes
  `buy_enablement` (still locked without a passed ORG-ID), and returns the case state
  in the ingestion response.

### 🟡 C4 — Unknown external contracts
- Floqer response shape, platform callback URL, platform email-verification fetch API,
  outbound email provider, production OCR engine.
- **v1 behavior**: each sits behind a small interface with a fixture/fake
  implementation, configured by environment, marked `TODO(integration)`.

## D. Design tightenings adopted (not contradictions — hardening)

- **D1 — Broker gate runs on every event.** The spec runs it "first on every full run";
  single-evidence events (`org_id.submitted`, `poc.submitted`) introduce identifiers
  that are broker match classes (`rir_org_ids`, `poc_handles`). The gate is local and
  free, so every run type starts with it.
- **D2 — Website-task dedupe.** Repeat `kyb.run_requested` events must not create
  duplicate open website tasks: task creation is skipped when an open task or a live
  `website_verified` check exists.
- **D3 — One live check per (case, check_type)**, enforced by a partial unique index
  (`WHERE superseded_by_check_id IS NULL`), making supersession races impossible to
  persist.
- **D4 — `POST /v1/review-tasks/{id}/complete` synthesizes a `website.review_completed`
  event** internally, so both completion paths share one idempotent, audited pipeline.
- **D5 — One email verification can create two checks** (`verified_email` +10 and
  `verified_company_email` +25), per the coexistence rule in `02 §1`.
- **D6 — Approval-grade validators fail closed (remediation PR 3).** The pass
  rules in `03 §3–§7` require full field matching, but the v1 validators skipped
  absent fields and could PASS on partial evidence — a document showing only a
  matching name, an ORG-ID whose *returned* handle differed from the submitted
  one, a company-email trusted from a caller-supplied `domain` instead of the
  address, or a POC associated-by-default when no ORG-ID/resource was submitted.
  PR 3 enforces the spec's stated rules: every pass-rule field is required on both
  sides; **missing** data routes to `needs_review` (distinct submission- vs
  evidence-incomplete codes), **mismatched** data `fail`s. Specific tightenings:
  ORG-ID requires returned==submitted handle and drops the shared postal-token
  address shortcut (two unrelated addresses sharing a digit token no longer
  match); email derives the domain from the address and rejects a payload whose
  separate `domain` contradicts it; `rir_poc` requires a verified association
  target. Gate 5's trigger set is centralized in
  `scoring.HARD_CONFLICT_REASON_CODES` (explicit allow-list), and a document
  contradicting the registry stamps `hard_conflict` so it fails the gate.
- **D7 — Identity-bound, single-use POC proof (remediation PR 4).** A POC token
  now proves exactly one identity — `(case, token_id, digest, rir, poc_handle,
  org/resource)` — and exactly once. The token is minted **bound** to the submitted
  identity (`poc_tokens.{rir,org_handle,resource}`, migration 009) and the validator
  re-checks that binding against the **current** run snapshot, so a POC/ORG change
  makes a previously minted token fail (`poc_token_binding_mismatch`); a `consumed_at`
  stamp set atomically with verification makes it single-use
  (`poc_token_consumed`). Crucially, identity invalidation
  (`checkstore.supersede_stale_identity_proof`) runs in the decide transaction
  **before** new checks are written and **independent of whether the revalidation
  adapter succeeded** — submitting a different ORG-ID/POC while RDAP is failing drops
  the stale `org_id_match`/`poc_verified` (and their points) to `needs_review` rather
  than leaving stale positives live. This closes the gap where identity-bound points
  outlived the identity that earned them.
- **D8 — Path-bound HMAC v2, per-case idempotency, and two deviations
  (remediation PR 5a).** v1 signs only `{timestamp}.{body}`, leaving `case_id`
  (in the URL path) unsigned — a captured signed event could be redirected to
  another case within the skew window and auto-approve it (this is the M2 hazard,
  ROADMAP §D). **HMAC v2** binds `key_id/direction/method/raw_path+query/timestamp/
  slot/sha256(body)` into the signed value, closing the redirect; the verifier is
  **sticky** (any v2 header ⇒ v2-only, no fallback to the path-unbound v1 scheme).
  **D3** re-scopes event idempotency from global to `(case_id, idempotency_key)`
  (migration 010) so the same key in another case is an independent event, never a
  replay — recorded in **ADR-003**. Rollout is dual-accept behind two independent
  sunset dates (inbound gated by a durable, fail-closed cross-replica witness;
  outbound by a staging callback E2E), shipped as a **non-hot stop/migrate/start
  cutover** (migration 010 is not hot-compatible; its downgrade is forward-only
  after cross-case reuse). **Two deliberate deviations from the normative package,
  recorded here:** (1) `POST /v1/review-tasks/{id}/complete` is **retired** — it
  duplicated a transition that already exists as the keyed `website.review_completed`
  event; completion now flows through that event with a validation floor (task
  exists / type=website / same-case / open), and full trust/concurrency binding is
  PR 5b. (2) `request_nonces` (anticipated by ROADMAP §G) is **omitted from
  migration 010** — retiring the endpoint removed the only keyless state-changing
  op, so a nonce table would be an unused security mechanism (YAGNI); revisit only
  if a future keyless HMAC op appears. `KYC_Tool_Build_Package/` is unmodified; the
  M2 hard stop is untouched.
- **D9 — Redacted decision-callback remainder is a governed, pseudonymous ordering
  record, not a personal-data-scope determination (remediation PR 7b-core).** Past
  `KYC_RETENTION_DAYS`, `workers.retention` destroys a decision_callback's BODY —
  it carried `checks[].source`, which can be reviewer-derived (`reviewer:<id>`) —
  but keeps the ROW: `case_id`, `run_id`, `decision_sequence`, `status`,
  `delivered_at`/`resolved_at`, and the recorded wire digest
  (`callback_wire_sha256`, `wire_version`) survive because the row is the durable
  ordering authority 7b-activation reconciles the platform against; deleting it
  would break that reconciliation. Redaction is scoped to terminal/non-sendable
  decision callbacks: `delivered` and `superseded` age by `delivered_at`/`resolved_at`,
  and `dead` ages by `created_at`. A `pending` callback is never touched. A redacted
  `dead` callback is deliberately **not** requeueable: the requeue endpoint refuses
  redacted bodies, and a retry must be a new `recalculate.requested` run because the
  original callback body is intentionally gone. The surviving remainder is pseudonymous —
  a hash plus internal ordinals, still joinable back to a case and, through it, to
  the natural person it concerns — and retaining it past the window is a
  deliberate, governed choice, not a claim that it falls outside any regulation's
  scope: this repo makes no determination of what is or is not personal data under
  any law. Accountability for that choice rests with the **deployer's data
  controller** — a named accountable owner is a deployment-time input this repo
  has no way to supply — and this repo does not, and cannot, discharge that
  controller's obligations (erasure, lawful basis, DPIA, breach notification,
  etc.); it only bounds what it keeps past the window and documents the bound.
  **Backups are a separate durable copy:** any backup taken before redaction still
  contains the pre-redaction body, independent of `KYC_RETENTION_DAYS`, until it
  ages out on its own backup retention schedule — see `docs/RUNBOOK.md`
  ("Retention & compliance").

### 🔵 D-7bcore-boundary — Local delivery evidence vs platform authority (PR 7b-core)

- The witness state `delivery_witnessed` is a **local publisher 2xx attestation bound to exact
  staged bytes** — the strongest statement this database can make about a decision callback, and
  explicitly **not** proof of a receiver fact. A database cannot observe a socket; it observes
  that its own application code reported one.
- **The terminating authority for what the receiver holds is the platform's signed
  accepted-request ledger**, introduced by PR 7b-activation. That unit must reconcile every
  `delivery_witnessed` row against the ledger and require digest/encoding agreement before
  declaring platform authority; a mismatch is a fail-closed `integrity_mismatch` terminal, never
  "nothing to reconcile".
- In-database authority (migrations `013`-`022`) defends against **application defects and
  races** — exhaustively. It does not defend against an adversary holding the database's own
  privileges, and the schema does not pretend otherwise.
- Two adversarial-audit prescriptions were formally rebutted and both dispositions were ACCEPTED
  by the reviewing agent:
  - **R1 — an extra local terminal-provenance column** (plus demotion of all pre-existing
    terminals): declined. A second local bit cannot prove a network fact against an actor who can
    rewrite the same database, and the demotion would retroactively re-label rows delivered under
    the already-fenced path.
  - **R2 — proving the prior authority before recreating it**: declined for prior *execution*
    history, which PostgreSQL records nowhere; ACCEPTED for *present observable structure*, which
    is provable. Migration `018` therefore validates the complete observable surface of both
    authority tables (ordered columns/types/nullability/defaults, exact constraint definitions,
    exact index definitions, the complete enabled trigger set by exact `pg_get_triggerdef`, and
    each owned function's normalized-body digest plus pinned `search_path`) and makes no claim
    about earlier execution.
- Recorded in full as **ADR-008**; the operative wording lives in `src/kyc_tool/outbox/witness.py`,
  which every reconciliation query goes through.
