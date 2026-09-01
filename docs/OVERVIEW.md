# KYC/KYB Tool — Overview & Guide

A complete reference for the IPv4.Global KYC/KYB verification tool: what it is,
how it works, what's built, what's planned, and what it needs to go live. The
early sections are plain-language; the later sections are technical.

---

## 1. What this is

The platform (IPv4.Global) is the storefront where customers register and
transact. The KYC tool is an automated **back-office verification clerk** the
platform consults: the platform hands it a customer's information, the tool
investigates using external evidence, and it returns a **verdict**. The tool
never talks to customers and never takes action itself. It scores and answers;
the platform enforces.

The closest analogy is a **credit check**: the storefront sends an applicant's
details to a bureau, the bureau checks sources and returns a decision, and the
storefront acts on it.

**The four verdicts the tool can return:**

| Verdict | Meaning |
|---|---|
| `approve` | Fully verified — open the account, buying enabled. |
| `approve_buy_locked` | Verified enough to open the account, but purchasing is held until the customer's IP-registry ID (ORG-ID) is confirmed. |
| `manual_review_insufficient` | Not enough evidence yet — a human should follow up. |
| `reject` | Hard no (e.g., a blocked broker). |

It reaches a verdict by collecting evidence, converting each piece into a
pass/fail **check** worth points, summing the points toward a **100-point
threshold**, and applying five **hard gates**. More proof → more points → a
better verdict.

---

## 2. How it works (the flow)

Everything is **event-driven**: the platform POSTs events, the tool processes
them asynchronously, and pushes results back. Nothing happens unless the
platform sends an event.

```
platform ──POST event──► API ──(persist + queue)──► background worker
                          │ returns 202 + run_id immediately (no waiting)
                          ▼
   worker:  broker gate ─blocked→ reject
            → gather evidence (adapters, external sources)
            → validate evidence into checks (deterministic rules)
            → score + evaluate 5 gates → decide (1 of 4)
            → push decision to platform via signed webhook
```

Step by step, for one customer ("case"):

1. **Event arrives.** The platform POSTs a signed event (e.g. "submitted
   company details," "verified email," "uploaded a document," "submitted an
   ORG-ID"). The tool records it, queues the work, and instantly returns a run
   ID; it does not block.
2. **A worker picks it up.** A background worker claims the job. Events for the
   same case are processed in order.
3. **Broker check first.** The company's identifiers are matched against the
   blocked-broker list. An exact match short-circuits everything → `reject`.
4. **Gather evidence.** The tool calls the external sources it needs for
   whatever's missing (registries, IP-registry lookups, document extraction,
   enrichment). Raw responses are stored for audit. If a source is down, that
   check simply isn't made; the tool never invents a failure.
5. **Validate into checks.** Each piece of evidence becomes a pass/fail check
   by fixed, deterministic rules. Each check is worth points.
6. **Score + gates.** Points from all currently-valid checks are summed
   (threshold = 100) and the five hard gates are evaluated.
7. **Decide.** One of the four verdicts.
8. **Return the decision.** The tool POSTs the decision back to the platform
   (signed) with the score, gate results, checks, and buy-enablement flag. The
   platform enforces it.
9. **Re-runs as evidence arrives.** When the customer does more, the platform
   sends another event and the cycle repeats: new checks supersede old ones and
   an updated decision goes back. A case naturally walks from *insufficient →
   approve_buy_locked → approve* as proof accumulates.

**Two defining properties:** the tool is **deterministic** (the same evidence
always yields the same decision, with no black-box judgment) and **fully auditable**
(every decision traces back through checks → raw evidence → the triggering
event, and records which policy version produced it).

---

## 3. The scoring model

### Checks and points (the scoring rubric)

| Check | Points | Category | How it's satisfied |
|---|---|---|---|
| `verified_company_email` | +25 | control proof | Company-domain email verified (not free/disposable), domain matches the submitted company. |
| `official_registry_match` | +25 | legal proof | Active company, exact match on name + address + registration number against the official registry. |
| `org_id_match` | +25 | control proof | RIR ORG-ID handle exists and matches the company, no broker conflict. |
| `poc_verified` | +25 | control proof | Point-of-contact token sent to the RIR-listed email and verified. |
| `business_document_verified` | +25 | legal proof | Uploaded document's name/address/number/jurisdiction match submission and registry. |
| `linkedin_company_match` | +20 | supporting | Person name + company + title + domain all match deterministically. |
| `website_verified` | +10 | supporting | Human reviewer confirms the company website is genuine. |
| `verified_email` | +10 | account access | Any email inbox verified. |

### The five hard gates

All five must pass for automatic approval:

1. **Score met** — total ≥ 100.
2. **Legal proof** — legal-business proof established (registry match *or*
   document match).
3. **Control proof** — control established (company email *or* ORG-ID *or* POC).
4. **Broker OK** — not a blocked broker.
5. **No hard conflict** — no contradictory evidence.

### The decision rule (priority order)

1. **`reject`** — blocked broker (exact match) or hard block.
2. **`approve`** — score ≥ 100, all gates pass, **and** the ORG-ID is verified.
3. **`approve_buy_locked`** — score ≥ 100 and all gates pass, but ORG-ID **not**
   yet verified → account approved, purchasing locked until it is.
4. **`manual_review_insufficient`** — anything else.

A customer can clear the 100-point threshold multiple ways: the rubric is
intentionally redundant, so no single source is a hard dependency.

---

## 4. The integration contract (how the platform connects)

### Inbound: one endpoint, all events

`POST /v1/cases/{case_id}/events`. Every event uses the same envelope and is
authenticated:

- **Auth (path-bound v2, dual-accept):** the primary scheme is **v2** — headers
  `X-KYC-Timestamp` + `X-KYC-Key-Id` + `X-KYC-Signature-V2`, an HMAC-SHA256 over a
  canonical value binding the method, full request path, direction, key id,
  timestamp, and body, so a captured signature can't be redirected to another
  case. The legacy **v1** header `X-KYC-Signature =
  HMAC-SHA256(secret, "{timestamp}.{raw_body}")` is still accepted during the
  dual-accept window, until the inbound sunset. Full spec + a worked vector:
  `docs/PLATFORM_INTEGRATION.md` §2. Requests older than 300 s are rejected
  (replay protection).
- **Idempotency:** an `Idempotency-Key` header per event, so retries are safe; a
  repeat returns the original response, never a duplicate run.
- **Envelope:** `event_type`, `occurred_at`, `actor {type, id}`, `payload`.

**Payload per event type** (bold = required):

| Event | Payload |
|---|---|
| `kyb.run_requested` | **`company_legal_name`**, `address`, `jurisdiction`, `website`, `contact{name,title}`, `platform_account_id` |
| `email.verified` | **`email`**, **`domain`**, **`verified_at`** |
| `org_id.submitted` | **`rir`**, **`org_handle`** |
| `poc.submitted` | **`rir`**, **`poc_handle`**, `org_handle`, `resource` |
| `poc.token_verified` | **`token_id`**, **`verified_at`**, `token` |
| `document.uploaded` | **`object_ref`**, **`doc_type`** |
| `website.review_completed` | **`task_id`**, **`result`** (pass/fail), **`reviewer_id`**, `reason_codes[]` |
| `reviewer.manual_approve` | **`reviewer_id`**, `note` |
| `recalculate.requested` | *(none)* |

**Ingest responses:** `202` accepted (with run_id) · `200` idempotent replay ·
`409` same key, different body · `422` bad schema · `401` bad signature.

### Outbound: the decision callback (webhook)

After every scoring run, the tool POSTs to **`{callback_url}/kyc/decision`**,
signed with **v2** (path-bound, using the dedicated outbound secret + key id) and
dual-emitting **v1** until the independent outbound sunset — the signature binds
the literal callback path, so a base with a path prefix signs that full path:

```
case_id, run_id, event_id,
decision,        // one of the four verdicts
score,           // total points; 100 = approval threshold (max 165)
gates{ score_met, legal_proof, control_proof, broker_ok, no_hard_conflict },
buy_enablement,  // enabled vs locked
checks[]{ type, status, points, source, reason_codes },
decided_at
```

Delivery is **at-least-once**, so the platform must dedupe on `(case_id, run_id)`.

### Outbox delivery ordering & local decision sequence (PR 7b-core)

Decision callbacks and POC emails are delivered from a transactional outbox. As of PR 7b-core the
publisher claims the oldest pending row **per `(case_id, ordering_stream)` FIFO stream** (`decision`
vs `email`), so a stuck POC email never blocks a case's decision callbacks. Every automatic decision
gets an internal per-case `decision_sequence`, allocated under the case's `FOR UPDATE` lock — it is
**not on the wire** (the callback body is byte-identical to pre-7b) and drives a **best-effort local
`superseded` guard**: an older requeued callback is suppressed only when a higher-sequence decision
already carries a locally-stamped `published_at`. Each claim is fenced by a `claim_token` so a stale
publisher cannot overwrite a reclaimer's terminal. **Boundary:** THREE residual reverts are NOT
closed here and remain expected until 7b-activation (`024`) adds the platform high-water mark:
send-before-stamp; cross-replica; and a queued automatic callback delivered AFTER a later manual
approval (manual approvals carry no run, no callback, and no sequence, so the local guard has no
higher locally-published automatic sequence to compare). 7b-core does not claim exactly-once (see
`AUDIT_FINDINGS.md` A6).

### Read endpoints (pull, on demand)

`GET /v1/cases/{id}` · `/v1/cases/{id}/checks` · `/v1/runs/{id}` ·
`/v1/review-tasks` · `/v1/metrics` · `/healthz`. The webhook is the primary
path; these are for querying state directly.

### The handshake — what must be exchanged to connect

1. **Platform → tool:** the tool's base URL + the HMAC credentials — the legacy
   v1 shared secret plus the v2 **inbound** secret + key id (path-bound signing,
   dual-accept; `docs/PLATFORM_INTEGRATION.md` §2).
2. **Tool → platform:** a callback endpoint the tool POSTs decisions to, signed
   with the v2 **outbound** secret + key id (and v1 until the outbound sunset);
   the platform dedupes on `(case_id, run_id)`.
3. **Upstream credentials** for the evidence sources (see §6).

---

## 5. Current state — what's built and working

The full engine is built and tested against a real database, and runs
end-to-end. A green draft PR carries the entire build.

**Built and working today:**

- The complete pipeline: event ingestion → queue → broker gate → evidence
  gathering → deterministic validation → scoring → 5 gates → decision → signed
  webhook callback → audit.
- All nine evidence adapters wired behind interfaces (live/stub status below).
- The scoring, gate, and decision engine, driven entirely by the policy files.
- A hand-built, crash-safe job queue and a transactional outbox for reliable
  callback delivery (at-least-once, retried with backoff).
- Append-only checks with supersession (exactly one live check per type; a case
  re-scores cleanly as new evidence arrives).
- A full audit trail: every decision reconstructs from event → evidence →
  checks → score → gates → decision → callback.
- **Per-run policy bundle pinning (PR 6).** The exact policy bundle behind
  any decision is now durably stored and reconstructable (content-hashed,
  not merely a hash of whatever files happened to be on disk), and every
  automatic/manual decision records both that bundle **and** the engine
  build (the scoring/gate/decision code itself) that produced it. Behind
  the `enforce_bundle_pinning` flag, a run resolves and scores under **the
  bundle it was created under**, not whichever bundle the worker process
  currently has loaded — see `docs/architecture-decisions.md` ADR-005.
  Still ahead: revalidating pre-existing evidence under a since-changed
  rubric (PR 6b), immutable source-evidence containment (PR 8), and
  broker-state reproducibility (PR 10).
- An **ops console** at `/ui`: cases, scores, gates, run state, integrations
  status, and a composer for sending test events.
- A one-command local dev stack (`scripts/dev.sh`) and a **Dockerfile** for
  containerized hosting.
- A broad automated test suite (policy-generated + golden cases + integration
  tests) that runs in CI on every change.

### Adapter status (the nine evidence sources)

| Adapter | Status | Notes |
|---|---|---|
| `broker_policy` | **Live** | Internal blocklist gate (database-backed). |
| `email_verification` | **Live** | Reads platform-delivered verification data. |
| `companies_house` | **Needs key** | UK registry; works unauthenticated, `CH_API_KEY` raises limits. |
| `gleif` | **Live** | Global entity registry; public API, no key. |
| `rir_rdap` | **Live** | ORG-ID / IP ownership; five RIR strategies (ARIN, RIPE, APNIC, LACNIC, AFRINIC). |
| `rir_poc` | **Stub** | POC token machinery; email delivery handled by the platform (see §6). |
| `document_ocr` | **Dev engine** | Reads structured test data today; production extraction is an open decision (§6). |
| `floqer_company_enrichment` | **Stub** | Enrichment; account exists, workflow + API wiring pending (§6). |
| `website_manual_review` | **Manual** | Human review queue — by design, not an automated source. |

---

## 6. What's needed to go live

These are the open integration items. None block the *core* engine; they
determine how rich the automated verification is at launch.

| Item | What's needed | Owner | Required for v1? |
|---|---|---|---|
| **Platform callback URL + HMAC credentials** | The endpoint the tool POSTs decisions to, plus the v1 legacy secret and the split v2 inbound/outbound secrets + key ids (`docs/PLATFORM_INTEGRATION.md` §2). | Platform team | **Yes** — the integration handshake. |
| **Companies House key** | `CH_API_KEY` (optional; works without, key raises rate limits). | Hilco | Recommended |
| **Document extraction** | Decide who reads uploaded documents (see §9). If the tool does it: pick an OCR engine (AWS Textract recommended). If the platform does it: it sends the four extracted fields as data. | Platform / Hilco | Decision needed |
| **POC token email** | Resolved: the **platform's existing transactional email** delivers the POC token; nothing to procure. | Platform | Wire-up only |
| **Floqer** | A Floqer *workflow* that returns the contact's LinkedIn match, plus the API key + trigger endpoint. Account already exists. | Hilco | **No** — fast-follow |
| **Hosting** | AWS environment: containers, Postgres, S3, secrets (see §8). | Platform / Hilco | **Yes** |

### Document extraction (OCR) — detail

The `document.uploaded` event carries a reference to the stored file, not the
bytes. Two ways to get the four fields the tool needs:

- **Platform extracts.** The platform reads the document and sends the four
  fields (company legal name, registered address, registration number,
  jurisdiction) as structured data on the event. The tool does no OCR.
- **Tool extracts.** The tool fetches the file and runs OCR behind a one-method
  interface: `extract(bytes, doc_type) → {name, address, number, jurisdiction}`.
  AWS Textract (plain-text tier, roughly cents per document) or self-hosted
  Tesseract both fit; supported formats are PDF, JPEG, PNG, TIFF. Many documents
  are native PDFs whose text extracts with no OCR cost.

### Floqer enrichment — detail

Floqer feeds one supporting check (`linkedin_company_match`, +20) and never
drives a decision on its own. Hilco has an account. Floqer is workflow-based
rather than a plain REST API, so integration is two steps: (1) build a Floqer
workflow that takes a company name + domain and returns the contact's LinkedIn
match (name, title, company, domain); (2) trigger it over Floqer's HTTP API,
which the tool's client (already stubbed behind a fixed interface) calls with
the API key and endpoint. It is credit-metered per lookup. Fast-follow, not
required for v1.

---

## 7. What's NOT needed

Deliberately out of scope:

- **A new email provider.** The platform's existing transactional email sends
  the POC token; no separate service to buy.
- **The tool doing OCR.** If the platform extracts the document fields and sends
  them as data, the tool needs no OCR engine at all.
- **Floqer at launch.** It contributes only a supporting +20 signal and never
  decides an outcome; the approval math clears 100 without it. A fast-follow,
  not a launch dependency.

---

## 8. Hosting & operations

**Stack:** Python 3.11 / FastAPI / PostgreSQL, with S3 for document and evidence
storage. Containerized (Dockerfile in the repo).

**Runtime shape:** one container image, run as three long-running process types
plus a migration step:

| Process | Command | Scaling |
|---|---|---|
| API | `uvicorn kyc_tool.api.app:create_app --factory` | Stateless — scale horizontally. |
| Pipeline worker | `python -m kyc_tool.workers.pipeline_worker` | Run N; coordinate via Postgres. |
| Outbox (webhook) worker | `python -m kyc_tool.workers.outbox_worker` | Delivers decision callbacks. |
| Migrations | `alembic upgrade head` | Once per deploy, before the rest. |
| Retention (cron) | `python -m kyc_tool.workers.retention` | Nightly; prunes per retention policy. |

**AWS mapping:** ECS/Fargate (containers) · RDS PostgreSQL · S3 · Secrets Manager
(HMAC secrets + upstream keys) · an internal ALB in front of the API (only the
platform needs to reach it).

**Outbound egress needed to:** Companies House, GLEIF, the RIR RDAP endpoints,
the platform's callback URL (and Floqer once wired).

**Configuration** is entirely environment variables:

| Variable | Purpose |
|---|---|
| `KYC_DATABASE_URL` | PostgreSQL connection string. |
| `KYC_PLATFORM_CALLBACK_URL` | Where the tool POSTs decisions. |
| `KYC_PLATFORM_HMAC_SECRET` | v1 legacy shared secret — needed until **both** v1 sunsets have passed (it verifies inbound v1 and signs the outbound v1 dual-emit); currently production-required unconditionally. |
| `KYC_HMAC_INBOUND_SECRET` / `KYC_HMAC_OUTBOUND_SECRET` (+ `_KEY_ID`) | v2 path-bound signing, split per direction (PR 5a). |
| `KYC_HMAC_V1_INBOUND_SUNSET_AT` / `KYC_HMAC_V1_OUTBOUND_SUNSET_AT` / `KYC_HMAC_V1_OBSERVATION_WINDOW_DAYS` | v1 dual-accept sunset dates + zero-witness window (prod-required). |
| `KYC_OBJECT_STORE` | `fs` (dev) or `s3` (production). |
| `KYC_S3_BUCKET` | Evidence bucket when `KYC_OBJECT_STORE=s3`. |
| `CH_API_KEY` | Companies House key (optional; raises rate limits). |
| `ARIN_API_KEY` | Optional; raises RIR RDAP rate limits. |
| `KYC_UI_ENABLED` | Set `false` in production. |

**Security:** the ops console (`/ui`) is debug tooling; set `KYC_UI_ENABLED=false`
in production or keep the port on the internal network. Its composer also
refuses `website.review_completed` and `reviewer.manual_approve` in
production with a server-side 403 (`docs/RUNBOOK.md`) — that check, not
hiding the controls, is the actual boundary for reviewer events.

### How updates work

- **Code changes** ship as standard rolling container deploys — tested in CI,
  rolled out with zero downtime, and reversible by redeploying the prior image;
  RDS/S3 data is untouched. **Exceptions — some releases are NOT rolling:**
  (1) a release carrying a **non-hot migration** uses a brief stop/migrate/start
  instead of a rolling deploy — notably PR 5a's migration 010, which drops a
  unique the old image still needs and becomes forward-only once two cases have
  reused an idempotency key (`docs/DEPLOYMENT.md` §2/§6); (2) a
  **security-sensitive non-hot code cutover** — a release with **no migration**
  whose fix only holds if no old replica serves traffic during rollout — uses a
  brief full maintenance window (all API + workers stopped together, no old/new
  overlap), notably PR 5b's reviewer-actor binding, where an old API would still
  apply `reviewer.manual_approve` with no actor floor and an old worker would
  still close a queued `system`-actor completion, reopening the exact forgery the
  release closes (`docs/DEPLOYMENT.md` §9; ADR-004). A release that ships its own
  cutover instructions overrides this generic rolling default.
- **Settings** (URLs, secrets, toggles) are environment variables, changed in
  the hosting config with no code change.
- **Scoring rules** (points, threshold, blocklist) are data-driven and
  **versioned**: a change is made deliberately, tested, and released, and every
  decision records which policy version produced it, so the audit trail always
  holds. There is intentionally no hot-reload; the blocklist can be updated in
  the database for urgent additions. Since **PR 6**, that policy bundle is
  also durably stored (content-hashed, so an exact historical bundle can
  always be reloaded, not just referenced by a sha of files that may have
  moved on) and, once `enforce_bundle_pinning` is cut over
  (`docs/DEPLOYMENT.md` §10), a run resolves and scores under **the bundle
  it was created under** rather than whichever bundle a worker process
  currently has loaded; the engine build that produced the decision is
  recorded alongside it (ADR-005).

---

## 9. Open decisions

1. **Who extracts document fields — platform or tool?** If the platform
   extracts, it sends the four fields (company name, address, registration
   number, jurisdiction) as structured data and the tool needs no OCR. If the
   tool does it, it OCRs the file (PDF/JPEG/PNG/TIFF). This decides the
   `document.uploaded` event payload.
2. **Callback URL + secret exchange** — the platform provides the endpoint and
   both sides agree on the HMAC credentials: the v1 legacy secret plus the split
   v2 inbound/outbound secrets + key ids (and a rotation plan).
3. **Registration-flow alignment** — the platform's registration is a
   lightweight "get through the door" step (ORG-ID not required up front, with a
   tooltip that providing it improves approval odds); heavier verification is
   deferred to transaction time. The tool already supports this via the
   `approve_buy_locked` path; confirm the platform's tiers map to the four
   verdicts as intended.

---

## 10. Roadmap — where it's going

- **v1 (launch):** the core verification path (email verification, registry
  match via Companies House / GLEIF, ORG-ID via RDAP, and documents), with
  signed webhook callbacks to the platform. Everything needed for this is built;
  it waits on the callback handshake, the document-extraction decision, and
  hosting.
- **Fast-follows (shortly after launch):**
  - Wire Floqer (build the workflow, supply the API key).
  - Finalize document extraction (platform-side ingestion, or the tool's OCR
    engine).
- **Future (as needs emerge):**
  - A **self-service admin screen** so authorized staff can adjust thresholds,
    point values, and the blocklist without a developer, with changes still
    versioned and audit-logged. Build this once you know which knobs you turn
    often.
  - Additional registry sources beyond Companies House / GLEIF for jurisdictions
    they don't cover.

---

## 11. Why it's built this way (key design decisions)

- **Policy-as-data, versioned per release.** Scoring rules live in data files,
  not code; each decision records the exact policy version, so any past
  decision is fully explainable. No silent rule edits. **Per-run bundle
  pinning (PR 6)** makes that policy version durably reconstructable (a
  content-hashed store, not just on-disk files) and, behind a flag, pins a
  run to resolve and score under its own creation-time bundle rather than
  whatever the worker process has loaded, with the producing engine build
  recorded alongside it — see `docs/architecture-decisions.md` ADR-005.
- **Deterministic validation.** Given the same evidence, the tool always
  produces the same decision, essential for a compliance tool and for testing.
- **Append-only checks + supersession.** Evidence is never overwritten; a case
  re-scores cleanly and the full history is preserved.
- **Postgres-backed queue + transactional outbox.** Reliable, crash-safe
  processing and at-least-once callback delivery without extra infrastructure.
- **HMAC-signed both directions.** Neither side can be spoofed; a forged
  "approved" message can't be injected.
- **The tool scores; the platform enforces.** Clean separation: the tool is a
  pure decision engine with no side effects on customers.
