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
  No component claims exactly-once.

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

### 🟡 C2 — POC token round-trip path unspecified
- The tool stores `token_hash`; the `poc.token_verified` payload carries "token id".
  Nothing says who hosts the confirmation link or how the raw token is validated.
- **v1 behavior**: token emails link to the **platform**; the platform posts
  `poc.token_verified` including the raw token; the tool validates hash + expiry.
  The email sender and link format sit behind interfaces.

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
