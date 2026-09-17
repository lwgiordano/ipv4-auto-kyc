# KYC Tool — Platform Team Briefing

The orientation doc for the team integrating with and operating the tool.
`PLATFORM_INTEGRATION.md` is the full contract (signatures, payloads, error
codes); this explains what the tool is, answers the open questions, and lays
out the staging plan. Read this first, build from that one.

## 1. What it is

An asynchronous verification service, integrated the way you recommended: the
platform POSTs an event — a registration, a verified email, an uploaded
document, an ORG-ID — gets an immediate acknowledgment, and the tool works in
the background. It checks the company the registrant claims against Companies
House, GLEIF, and the five regional internet registries (ARIN, RIPE, APNIC,
LACNIC, AFRINIC), screens the broker blocklist, scores the evidence, and POSTs
the verdict to a webhook you host.

The tool never drives the platform's UI and never changes platform state — it
scores and answers, the platform acts. The one message that reaches a person
directly is the POC verification email, always to the registry-listed address;
who sends it is an open decision (§8 item 10, and `PLATFORM_INTEGRATION.md` §5).

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
`manual_review_insufficient`, `reject`. Every non-passing check carries a
stable reason code saying what's missing or wrong — that's what the review
team acts on.

Only a broker-blocklist match auto-rejects; everything else that falls short
goes to manual review, so the review queue carries the volume, not rejections.
Sanctions screening happens on the platform before the tool is ever called.

**Failure behavior:** if a registry is down, the run completes as *partial* —
existing evidence stands, nothing is guessed, and the next event re-checks.
An outage can delay a better verdict; it can never produce a wrong one.

## 3. A case, end to end

A case is one registrant: the person signing up on behalf of a company.
Approving the case approves that person, not the company, so the sign-up event
must carry who they are — `contact.name` and `contact.email` (the address they
signed up with, the one `email.verified` later confirms), and the
`platform_account_id` your side holds for them. A second registrant at the same
company is a second case. Send the RIR org handle when the registrant has one:
`org_id.submitted` right after the sign-up event, and again whenever they add
or change it later — it is optional at registration, and the check runs the
moment it arrives.

1. User registers → platform POSTs `kyb.run_requested` with the registrant's
   contact details and the company details → `202 {"run_id": "…"}`.
2. Seconds later your webhook receives the first verdict: registry matched
   (25) → score 25, `manual_review_insufficient`, reason codes showing what's
   still missing.
3. The uploaded document's fields are extracted — by the platform or by the
   tool, still open (§5) → `document.uploaded`
   → matches submission and registry (25) → score 50.
4. User verifies their work email → platform POSTs `email.verified` →
   `verified_email` (10) + `verified_company_email` (25) → score 85.
5. User submits ORG-ID → `org_id.submitted` → registry lookup passes (25) →
   score 110, all five gates green, decision `approve` (during MVP it arrives
   held-for-review with the computed decision attached; the review team
   confirms in the platform admin).

Every event gets its own run and its own webhook. Deliveries retry until you
acknowledge with a 2xx, so dedupe on `(case_id, run_id)`.

Evidence is optional at every step — the tool scores what exists. When a user
later adds or changes something that matters (ORG-ID, a document, company
details), the platform re-sends the matching event and verification re-runs
automatically. The full trigger table is `PLATFORM_INTEGRATION.md` §3.

## 4. What your team builds

1. **The decision webhook** — one HTTPS endpoint. Verify the signature,
   respond 200, dedupe. (`PLATFORM_INTEGRATION.md` §4.)
2. **The POC confirmation page** — user enters the code and reference from
   the verification email; platform POSTs both back to us. Codes are
   single-use, expire in 72 h, and die if the user edits identity details —
   recovery is always re-submitting the POC. (§5.)
3. **Admin views for held cases** — the review team works in the platform
   admin, so surface each case's decision, score, checks, and reason codes
   (from the webhook body, or `GET /v1/cases/{id}`). Keep score and reason
   codes admin-only; users see their status and the next useful step. (§7.)
4. **Status notifications and review assignment** — user emails per status,
   admin alerts, and assigning held cases to reviewers are platform features
   keyed off the webhook result. Suggested result-to-action mapping:
   `PLATFORM_INTEGRATION.md` §4.

## 5. Answers to the open questions

**File formats (#2).** The kickoff call leaned toward the platform extracting
the document fields — company name, address, registration number,
jurisdiction — and sending them as a small JSON; the tool cross-checks those
against the registries. That is now an open decision between platform
extraction (JSON to the tool) and the tool running OCR on the original file
(needs an OCR provider IPv4.Global chooses). The event contract is the same
either way.
See `PLATFORM_INTEGRATION.md` §6.

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
  and reversible — with one deliberate exception: migration 010 (PR 5a) is
  forward-only once two cases have reused an idempotency key
  (`docs/DEPLOYMENT.md` §6).
- `GET /readyz` for the load balancer (checks config, DB, migration version,
  storage), `GET /healthz` for liveness.
- All config is env vars prefixed `KYC_`; `docs/RUNBOOK.md` documents every
  one, plus dead-letter recovery and the ops console.
- With `KYC_ENVIRONMENT=production` the tool refuses to boot on unsafe config
  (missing secret, stub providers, non-HTTPS callback) and lists every
  violation at once. A bad deploy fails loudly instead of running quietly
  broken.

## 6. Staging plan

Staging runs with **automation on** (`KYC_ENFORCE_POSITIVE_DECISIONS=true`) to
rehearse the real end state. This is safe **only** because staging is closed —
reachable by our own tests, never by untrusted callers. It is a rehearsal, not
the production go-live: the M2 gate (real-adapter end-to-end + hardened signing
+ platform cutover) still governs turning automation on in **production**, which
launches with it off — the tool investigates, the review team confirms, and the
flag flips per environment once staging has proven out.

> **Keep staging closed until inbound v1 is actually disabled.** PR 5a adds
> path-bound HMAC v2, but during the dual-accept window a **v1-only** request is
> still path-unbound — a signed event captured within the skew window could be
> replayed to a different case. The redirect closes for v2 at deploy, but for
> everyone only once inbound v1 is disabled (the zero-witness is satisfied and
> `hmac_v1_inbound_sunset_at` takes effect). Keep staging's perimeter closed
> until then, not merely until PR 5a ships.

Checklist:

1. RDS Postgres 14+, S3 bucket, ECS/Fargate service (or one EC2 box) for the
   four processes.
2. Build the image from the repo `Dockerfile`.
3. Generate the HMAC secrets into AWS Secrets Manager and set them on both
   sides: the v1 legacy `KYC_PLATFORM_HMAC_SECRET`, plus the v2 pairs
   `KYC_HMAC_INBOUND_KEY_ID`/`KYC_HMAC_INBOUND_SECRET` and
   `KYC_HMAC_OUTBOUND_KEY_ID`/`KYC_HMAC_OUTBOUND_SECRET`
   (`PLATFORM_INTEGRATION.md` §2/§7). Staging boots in development mode without
   the v2 set, but v2 dual-accept — the thing staging exists to rehearse — needs
   it configured on both sides.
4. Core env vars: `KYC_DATABASE_URL`, `KYC_PLATFORM_CALLBACK_URL` (your
   staging receiver), `KYC_OBJECT_STORE=s3` + `KYC_S3_BUCKET`,
   `KYC_ENFORCE_POSITIVE_DECISIONS=true`.
5. Leave `KYC_ENVIRONMENT` at `development` for now: staging uses the built-in
   stand-ins (documents as extracted JSON, registry lookups from recorded
   data). For the POC flow, set `KYC_EMAIL_PROVIDER=file`: each verification
   email is appended as a JSON line to a local sink file
   (`.substrate/state/poc-emails.log` by default, `KYC_EMAIL_FILE_PATH` to
   move it), so your tests can read the token and reference and complete the
   round-trip. The sink writes raw tokens to disk — closed staging only;
   production refuses this provider at boot. Live providers land later
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
                "registration_number": "12345678", "jurisdiction": "GB",
                "contact": {"name": "Jane Doe", "email": "jane.doe@acme.example",
                            "title": "Director"},
                "platform_account_id": "acct-001"},
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
- POC round-trip in staging: after `poc.submitted`, read the token and
  "Verification reference" from the email sink file (§6 step 5), then POST
  `poc.token_verified` with both.
- The ops console (`/ui`, enable with `KYC_UI_ENABLED=true` in staging) shows
  cases, scores, gates, run states, and can compose signed test events from
  the browser.

## 8. Where things stand, and what we need from you

Status at this release, grouped into five states. Nothing here calls the
service production-ready; the go/no-go gate is §8 of
`docs/superpowers/specs/2026-09-12-production-readiness-design.md`.

**Implemented now**

- Signed event ingestion with a typed request and response contract in
  `/openapi.json` (all nine event types, exact status codes) and idempotent
  replay.
- Decision webhook delivery, at-least-once, with retries and dead-lettering.
- The read API and the pull-style Salesforce projection
  (`PLATFORM_INTEGRATION.md` §7).
- Website review and manual approval as signed events; the operator console,
  including live configuration and Salesforce destination-name mapping.
- Live registry adapters: Companies House (needs the API key, item 14), GLEIF,
  and the five RIR RDAP strategies — including the POC directory lookup over
  them (association verified on the org/resource record, registry-listed email
  read off it; who sends the token email stays open, item 10). S3-compatible
  evidence storage.
- The production boot check that refuses stub providers and unsafe config.

**Implemented but not activated**

- Ordered callback delivery (`decision_sequence`). The interim receiver rule in
  `PLATFORM_INTEGRATION.md` §4 applies until migration 025 is bootstrapped from
  your accepted-run ledger and activated.
- Positive enforcement of automatic approvals stays off at launch; held
  decisions arrive as `enforcement_held` (`PLATFORM_INTEGRATION.md` §4).
- v1 signature retirement: dual-accept now; sunset dates are set at cutover.

**Awaiting your configuration** (through the deployment secret manager or
written deployment config, never chat)

1. Callback base URLs, staging and production.
2. Key ids and the HMAC secrets for each wire direction, through the secret
   manager.
3. S3 bucket, key prefixes, and IAM ownership for evidence and extracted JSON.
4. The Salesforce sandbox and the platform service that will consume the
   projection.
5. AWS deployment access for whoever on your side deploys.

**Awaiting your contract decisions** (write each down; we turn it into a
tested contract)

6. Your callback receiver's behavior: that it commits the decision before
   returning 2xx, and how it dedupes on `(case_id, run_id)`.
7. Ordering bootstrap, needed before migration 025 can be planned: where the
   platform's accepted-run ledger lives; how we query the accepted decision for
   every case; how manual approvals and reverted decisions appear in it; who
   signs the bootstrap response and how that signer is identified; the maximum
   bootstrap size and the recovery procedure.
8. Review-task changes: the default is a webhook you host for task
   opened/completed/cancelled; if you would rather poll
   `GET /v1/review-tasks?status=open`, say so with the cursor, freshness, and
   missed-poll recovery rules you need.
9. Document extraction: decide between the platform extracting the four
   fields (JSON to the tool) and the tool running OCR on the original upload
   (needs an OCR provider). Open on both sides; `PLATFORM_INTEGRATION.md` §6
   has the two options.
10. POC verification email: decide who sends it, the tool through an SES
    sender IPv4.Global provisions, or the platform through its own
    transactional email with a typed hand-off contract
    (`PLATFORM_INTEGRATION.md` §5). Confirm too that you host the POC page
    and echo back both `token` and `token_id`.
11. Floqer: contract in place; the tool calls a published shortcut in
    IPv4.Global's own Floqer account (discovery-only, feeds the LinkedIn
    match). Nothing needed from the platform.
12. Operating targets: expected daily and peak case volume, concurrent runs,
    acceptable latency for light and full checks, soak duration, deployment
    region, maintenance-window constraints, availability and recovery
    objectives.
13. Reviewer information requests: when a case stalls for evidence the
    registrant never supplied — most often the RIR org handle — a reviewer
    records the ask in the console, and `GET /v1/cases/{id}` serves it as
    `information_requested` (`PLATFORM_INTEGRATION.md` §7). Say how you would
    rather learn of one: poll that field, or a new webhook message we send
    you. We build the webhook only once you answer; the request itself is
    recorded either way.

**From IPv4.Global**

14. Companies House API key (secret manager).
15. Email: if the tool sends, an SES identity and sending domain in the
    secret manager and deployment config.

**Implemented now**: the conformance kit you can run against staging —
`python -m kyc_tool.conformance` checks your signer offline, your sender against
a running tool, and your decision receiver against real signed callbacks
(`PLATFORM_INTEGRATION.md` §11).

**Coming on our side, not yet built**: a downloadable versioned contract
bundle, the production provider profile (Floqer client, the switch that hands
it the built POC directory, and — whichever way items 9 and 10 go — the email
sender or the platform hand-off, and the OCR engine), the load and soak
harness, and migration 025 once item 7 is answered.

**Out of scope by design**: the tool never writes Salesforce, never judges a
website automatically, never replaces your platform UI, and never changes a
historical decision when configuration or mappings change.

## 9. Doc map

| Question | Doc |
|---|---|
| Full API contract, signatures, payloads, webhook | `docs/PLATFORM_INTEGRATION.md` |
| Deploying, releasing, rollback, monitoring | `docs/DEPLOYMENT.md` |
| Operating it: env vars, health, dead letters, console | `docs/RUNBOOK.md` |
| How scoring and decisions work, in depth | `docs/OVERVIEW.md` |
| Salesforce field-by-field mapping and value rules | `docs/SALESFORCE_MAPPING.md` |
| Normative spec and policy files | `KYC_Tool_Build_Package/` |
