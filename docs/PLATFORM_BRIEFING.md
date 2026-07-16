# KYC Tool — Platform Team Briefing

The orientation doc for the team integrating with and operating the tool.
`PLATFORM_INTEGRATION.md` is the full contract (signatures, payloads, error
codes); this explains what the tool is, answers the open questions, and lays
out the staging plan. Read this first, build from that one.

## 1. What it is

An asynchronous verification service, integrated the way you recommended: the
platform POSTs an event — a registration, a verified email, an uploaded
document, an ORG-ID — gets an immediate acknowledgment, and the tool works in
the background. It checks the company against Companies House, GLEIF, and the
five regional internet registries (ARIN, RIPE, APNIC, LACNIC, AFRINIC),
screens the broker blocklist, scores the evidence, and POSTs the verdict to a
webhook you host.

The tool never talks to end users and never mutates the platform. It answers;
the platform acts.

## 2. What it does

Each piece of evidence becomes a **check** worth points. Points sum toward a
**100-point threshold**; five **hard gates** must also hold:

| Gate | Meaning |
|---|---|
| `score_met` | total ≥ 100 |
| `legal_proof` | at least one legal-identity check passed (registry or document) |
| `control_proof` | at least one control check passed (ORG-ID, POC, or company email) |
| `broker_ok` | not on the blocked-broker list |
| `no_hard_conflict` | no two evidence sources contradict each other |

The checks (from `KYC_Tool_Build_Package/machine_readable/scoring_rubric.json`,
the policy file both the tool and its tests read):

| Check | Points | Category |
|---|---|---|
| `official_registry_match` | 25 | legal proof |
| `business_document_verified` | 25 | legal proof |
| `verified_company_email` | 25 | control proof |
| `org_id_match` | 25 | control proof |
| `poc_verified` | 25 | control proof |
| `linkedin_company_match` | 20 | supporting |
| `verified_email` | 10 | account access |
| `website_verified` | 10 | supporting |

Verdicts: `approve`, `approve_buy_locked` (account OK, purchasing held until
ORG-ID verifies — this is how "ORG-ID optional at registration" works),
`manual_review_insufficient`, `reject` (blocked broker). Every non-passing
check carries a stable reason code saying what's missing or wrong — that's
what the review team acts on.

**Failure behavior:** if a registry is down, the run completes as *partial* —
existing evidence stands, nothing is guessed, and the next event re-checks.
An outage can delay a better verdict; it can never produce a wrong one.

## 3. A case, end to end

1. User registers → platform POSTs `kyb.run_requested` with the company
   details → `202 {"run_id": "…"}`.
2. Seconds later your webhook receives the first verdict: registry matched
   (25) and LinkedIn matched (20) → score 45, `manual_review_insufficient`,
   reason codes showing what's still missing.
3. User verifies their work email → platform POSTs `email.verified` →
   `verified_email` (10) + `verified_company_email` (25) → score 80.
4. User submits ORG-ID → `org_id.submitted` → registry lookup passes (25) →
   score 105, all five gates green, decision `approve` (during MVP it arrives
   held-for-review with the computed decision attached; the review team
   confirms in the platform admin).

Every event gets its own run and its own webhook. Deliveries retry until you
acknowledge with a 2xx, so dedupe on `(case_id, run_id)`.

## 4. What your team builds

1. **The decision webhook** — one HTTPS endpoint. Verify the signature,
   respond 200, dedupe. (`PLATFORM_INTEGRATION.md` §4.)
2. **The POC confirmation page** — user enters the code and reference from
   the verification email; platform POSTs both back to us. Codes are
   single-use, expire in 72 h, and die if the user edits identity details —
   recovery is always re-submitting the POC. (§5.)
3. **Admin views for held cases** — the review team works in the platform
   admin, so surface each case's decision, score, checks, and reason codes
   (from the webhook body, or `GET /v1/cases/{id}`). (§7.)

## 5. Answers to the open questions

**File formats (#2).** In the MVP the platform extracts the document fields —
company name, address, registration number, jurisdiction — and uploads them as
a small JSON; the tool cross-checks those against the registries. So supported
input formats are whatever you choose to parse. Later the tool can OCR raw
PDFs/images itself once an engine is picked; your integration doesn't change.
(§6.)

**Stack and deployment (#3).** Your team hosts and operates the tool in
IPv4.Global's AWS account. The code itself stays IPv4.Global-maintained: we
cut releases in the repo, your team pulls a release and redeploys. No code is
edited on the server.

- Python 3.11 + FastAPI. **PostgreSQL 14+ is the only hard dependency** — the
  work queue and webhook delivery live in the database. No Redis, no broker.
- Also: an S3 bucket (evidence files) and outbound HTTPS (public registries).
- Four small stateless processes; scale any of them horizontally:

| Process | Command |
|---|---|
| API | `uvicorn kyc_tool.api.app:create_app --factory` |
| Pipeline worker | `python -m kyc_tool.workers.pipeline_worker` |
| Outbox publisher | `python -m kyc_tool.workers.outbox_worker` |
| Retention (daily cron) | `python -m kyc_tool.workers.retention` |

- A `Dockerfile` ships in the repo. An update is: pull the release, build the
  image, `alembic upgrade head`, restart processes. Migrations are versioned
  and reversible.
- `GET /readyz` for the load balancer (checks config, DB, migration version,
  storage), `GET /healthz` for liveness.
- All config is env vars prefixed `KYC_`; `docs/RUNBOOK.md` documents every
  one, plus dead-letter recovery and the ops console.
- With `KYC_ENVIRONMENT=production` the tool refuses to boot on unsafe config
  (missing secret, stub providers, non-HTTPS callback) and lists every
  violation at once. A bad deploy fails loudly instead of running quietly
  broken.

## 6. Staging plan

Staging runs with **automation on** (`KYC_ENFORCE_POSITIVE_DECISIONS=true`) —
access is limited to our own tests, so we rehearse the real end state.
Production launches with it off: the tool investigates, the review team
confirms, and the flag flips per environment once staging has proven out.

Checklist:

1. RDS Postgres 14+, S3 bucket, ECS/Fargate service (or one EC2 box) for the
   four processes.
2. Build the image from the repo `Dockerfile`.
3. Generate one shared secret into AWS Secrets Manager; set it on both sides
   (`KYC_PLATFORM_HMAC_SECRET` here, the same value in the platform's staging
   config).
4. Core env vars: `KYC_DATABASE_URL`, `KYC_PLATFORM_CALLBACK_URL` (your
   staging receiver), `KYC_OBJECT_STORE=s3` + `KYC_S3_BUCKET`,
   `KYC_ENFORCE_POSITIVE_DECISIONS=true`.
5. Leave `KYC_ENVIRONMENT` at `development` for now: staging uses the built-in
   stand-ins (documents as extracted JSON, verification emails written to
   logs, registry lookups from recorded data). Live providers land later
   without changing your integration; production mode is for then.
6. `alembic upgrade head`, start the processes, check `/readyz`.
7. Smoke test: send a signed `kyb.run_requested`, watch the verdict arrive.
   Until your receiver exists, `scripts/dev_receiver.py` is a stub that
   prints incoming callbacks.

## 7. Testing tips

Signing a request (Python):

```python
import hashlib, hmac, json, time, uuid
import httpx

SECRET = "…"  # staging shared secret
body = json.dumps({
    "event_type": "kyb.run_requested",
    "occurred_at": "2026-07-16T12:00:00Z",
    "actor": {"type": "system", "id": "smoke-test"},
    "payload": {"company_legal_name": "Acme Networks Ltd",
                "address": "1 Main Street, London, EC1A 1AA",
                "registration_number": "12345678", "jurisdiction": "GB"},
}).encode()
ts = str(time.time())
sig = hmac.new(SECRET.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
r = httpx.post("https://kyc-staging.example/v1/cases/case-001/events",
               content=body,
               headers={"Content-Type": "application/json",
                        "Idempotency-Key": uuid.uuid4().hex,
                        "X-KYC-Timestamp": ts,
                        "X-KYC-Signature": sig})
print(r.status_code, r.json())
```

- Re-send the same bytes with the same `Idempotency-Key`: you get `200` and
  the stored response, no duplicate run. That's the retry safety to build on.
- `GET /v1/cases/case-001` shows the live checks and reason codes after each
  event.
- The ops console (`/ui`, enable with `KYC_UI_ENABLED=true` in staging) shows
  cases, scores, gates, run states, and can compose signed test events from
  the browser.

## 8. What we need from you

1. Staging callback URL (production's later).
2. A secure channel to exchange the shared secret.
3. AWS access for whoever on your side deploys.
4. Confirmation of the document path in §5 (platform extracts) or a request
   for tool-side OCR instead.
5. Will you consume the optional `event_sequence` ordering field?

## 9. Doc map

| Question | Doc |
|---|---|
| Full API contract, signatures, payloads, webhook | `docs/PLATFORM_INTEGRATION.md` |
| Deploying, releasing, rollback, monitoring | `docs/DEPLOYMENT.md` |
| Operating it: env vars, health, dead letters, console | `docs/RUNBOOK.md` |
| How scoring and decisions work, in depth | `docs/OVERVIEW.md` |
| Normative spec and policy files | `KYC_Tool_Build_Package/` |
