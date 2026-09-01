# Architecture (v2 — Platform-Driven)

## 1. The three layers

| Layer | Role | Owns |
|---|---|---|
| **IPv4.Global Platform** | Hub. Entry and enforcement. | User actions (register, verify, add ORG-ID, upload doc, run KYB), reviewer manual approve, enforcement of decisions (approve / lock buying / reject), one-way Salesforce sync |
| **KYC Tool** (this build) | Scoring engine. | Event ingestion, evidence adapters, deterministic validation, KYC checks, score + hard gates, decision computation, audit trail, manual review queues |
| **Salesforce** | System of record. | Mirrored case status, score, checks, actions — written by the platform, read by humans. Drives nothing. |

```
 platform event                 decision
PLATFORM ──────────► KYC TOOL ──────────► PLATFORM ──── one-way sync ────► SALESFORCE
   ▲                                          │
   └────────── user adds evidence ◄───────────┘   (loop until resolved)
```

Key inversion vs. the original (v1) concept: Salesforce fields are **not** a control plane. Nothing in Salesforce triggers the tool. Manual approval is **not** initiated from Salesforce — a reviewer approves directly on the platform.

## 2. End-to-end flow (one pass)

1. **Platform receives event** — registration, email verified, ORG-ID submitted, POC submitted, document uploaded, KYB run requested, or reviewer action.
2. **Platform invokes the KYC Tool** — `POST /v1/cases/{case_id}/events` with an idempotency key and the event payload.
3. **Tool resolves inputs** — loads/creates the case, loads live (non-superseded) checks, merges submitted data.
4. **Tool runs the affected adapters** — full set for a KYB run; only the affected adapter for a single-evidence event (see `machine_readable/platform_events.json`).
5. **Tool validates deterministically** — each adapter result is evaluated against its pass rule (see `03_ADAPTERS_AND_EVIDENCE.md`).
6. **Tool creates/supersedes checks** — one immutable check per evidence item; superseded checks stay for audit.
7. **Tool scores** — sum of live check points, capped per rules; evaluates the four hard gates.
8. **Tool decides** — `approve` | `approve_buy_locked` | `manual_review_insufficient` | `reject` (see `02_SCORING_AND_DECISIONS.md`).
9. **Tool returns the decision** to the platform (synchronous response for fast runs; callback for long runs).
10. **Platform enforces** — approves the account, locks/unlocks buying, or rejects/suspends.
11. **Platform syncs Salesforce** — status, score, action, and check summaries mirrored to the CRM record.
12. **Loop** — the user adds more evidence through the platform; go to 1. `manual_review_insufficient` auto-clears the moment a recalculation crosses the threshold with gates passed.

## 3. Manual approve (the exception path)

- Reviewer approves the account **directly on the platform**.
- The platform enforces Approve immediately — score and hard gates are bypassed.
- The platform notifies the tool (`reviewer.manual_approve` event) so the case is marked `approved_manual` with reviewer identity and timestamp in the audit trail.
- The platform syncs the result to Salesforce like any other outcome.
- Buy enablement policy still applies: without a passed ORG-ID check, manual approval yields buy-locked state.

## 4. Components to build (inside the KYC Tool)

1. **Event API** — receives platform events, validates payloads, enforces idempotency, enqueues work, returns fast.
2. **Orchestrator** — per-case run state machine (`machine_readable/state_machine.json`); fans out adapter jobs; joins results.
3. **Adapter workers** — one worker module per adapter (`03_ADAPTERS_AND_EVIDENCE.md`), each with retries, timeouts, raw-response capture to object storage, and a normalized output contract.
4. **Validation layer** — pure functions: `(normalized evidence, submitted data) → pass | fail | needs_review` with reason codes.
5. **Check store** — immutable checks with supersession chains.
6. **Scoring engine** — pure function over live checks → `{score, gates, breakdown}`.
7. **Decision engine** — pure function over `{score, gates, broker status, conflicts}` → decision (`machine_readable/decision_policy.json`).
8. **Review queue** — human tasks: website verification (always manual) and POC-email-unavailable cases. Minimal UI or API consumed by the platform's back-office.
9. **Platform callback publisher** — returns decisions; retries with backoff; exactly-once semantics via event IDs.
10. **Audit log** — append-only record of every event, adapter call, check, score, and decision.

Salesforce sync is **not** a tool component — the platform owns it (`05_SALESFORCE_SYNC.md` defines the field mapping the platform uses).

## 5. Sequence — initial KYB run

```
Platform                    KYC Tool                          External
   │  POST /events (kyb.run) │                                    │
   ├──────────────────────►  │ resolve case, enqueue run          │
   │   202 {run_id}          │                                    │
   │ ◄──────────────────────┤                                    │
   │                         │ email status ──────────────────►  platform email API
   │                         │ Floqer discovery ──────────────►  Floqer
   │                         │ registry match ────────────────►  Companies House / GLEIF
   │                         │ ORG-ID (if submitted) ─────────►  RIR RDAP
   │                         │ POC (if submitted) ────────────►  RIR RDAP + token email
   │                         │ document OCR (if uploaded)        │
   │                         │ broker exact-match (local list)   │
   │                         │ website → review queue (human)    │
   │                         │ validate → checks → score → gates │
   │  POST /kyc/decision     │                                    │
   │ ◄──────────────────────┤ {decision, score, breakdown}       │
   │  enforce + sync SF      │                                    │
```

## 6. Deployment shape

Production-ready reference (AWS flavor): API Gateway + queue (SQS) + workers (Lambda/Fargate) + Postgres + S3 for raw evidence + Secrets Manager + structured logs/metrics/alerts.

**Acceptable v1 simplification** (default unless the host repo says otherwise): one web service + background worker processes + Postgres + S3-compatible object store, provided it keeps: durable job queue, retries with dead-lettering, idempotency, raw evidence capture, and secret management. The architecture must make the split-out to separate services possible later (workers communicate through the queue and database, not in-process calls).

## 7. Explicitly out of scope for v1

- Automated website crawling/scoring (manual queue instead).
- Fuzzy broker matching / risk-flag review path (documented future extension).
- Any Salesforce-initiated flow, trigger, or callout into the tool.
- Paid enrichment sources beyond Floqer unless a phase explicitly adds one.
