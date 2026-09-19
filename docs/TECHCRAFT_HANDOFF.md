# TechCraft handoff

This file is generated: it stacks the three source documents in reading order and
changes nothing inside them. Start with Part 1 for what the tool does and who does
what. Part 2 is the wire contract your engineers build against. Part 3 is for whoever
runs the service. Section numbers belong to the source files, so a pointer such as
"`docs/PLATFORM_INTEGRATION.md` §5" means the same section here. Edit a source and
rebuild with `.venv/bin/python scripts/build_techcraft_handoff.py`.

## Contents

- **Part 1** (`docs/PLATFORM_BRIEFING.md`): KYC Tool — Platform Team Briefing
  - 1. What it is
  - 2. What it does
  - 3. A case, end to end
  - 4. What your team builds
  - 5. The open questions
  - 6. Staging plan
  - 7. Testing tips
  - 8. Where things stand, and what we need from you
  - 9. Doc map
- **Part 2** (`docs/PLATFORM_INTEGRATION.md`): Platform Integration Guide — MVP
  - 1. The model
  - 2. Authentication (both directions)
  - 3. Sending events
  - 4. The decision webhook (you build this)
  - 5. POC verification page (you build this)
  - 6. Documents (open question: who reads the fields?)
  - 7. Read API and review tasks
  - 8. Hosting and deployment (you run this too)
  - 9. MVP scope and what comes later
  - 10. Answers we need
  - 11. Conformance kit
- **Part 3** (`docs/DEPLOYMENT.md`): Deployment & Releases
  - 1. One image, four processes
  - 2. Environments
  - 3. First-time setup (per environment)
  - 4. Deploying an update
  - 5. Post-deploy verification
  - 6. Rollback
  - 7. Monitoring and incidents
  - 8. Rules
  - 9. PR 5b cutover — brief full maintenance window
  - 10. PR 6 cutover — bundle-pinning activation
  - 11. PR 7b-core cutover — drained maintenance window (migration 013)
  - 12. Live configuration cutover (migration 024)

# Part 1

# KYC Tool — Platform Team Briefing

Read this one first. It says what the tool does, how one registrant moves
through it, what your team builds, and what we still need from you. The wire
contract your engineers build against is `PLATFORM_INTEGRATION.md`:
signatures, payloads, status codes. This document points at that one instead
of repeating it.

## 1. What it is

The tool answers one question about one person: does this registrant really
speak for the company they claim? It never drives your screens and never
changes platform state. It gathers evidence, scores it and answers. The
platform acts.

The work happens in the background. Your platform POSTs an event (a
registration, a verified email, an uploaded document, an RIR org handle) and
gets an acknowledgment straight away. The tool then looks the claimed company
up in Companies House, GLEIF and the five regional internet registries (ARIN,
RIPE, APNIC, LACNIC, AFRINIC). It screens the broker blocklist. It scores what
it found and POSTs the verdict to a webhook your team hosts.

One message from the tool reaches a person directly: the POC verification
email, always sent to the address the registry lists for that contact. Whether
the tool sends it or your platform does is one of the two open questions (§5
and §8 item 10).

## 2. What it does

Each piece of evidence becomes a **check** worth points. Points add up toward
a **100-point threshold**. Five **gates** have to hold as well, and a case
that misses one of them cannot be approved however high it scores.

| Gate | Meaning |
|---|---|
| `score_met` | total ≥ 100 |
| `legal_proof` | at least one legal-identity check passed (registry or document) |
| `control_proof` | at least one control check passed (ORG-ID, POC, or company email) |
| `broker_ok` | not on the blocked-broker list |
| `no_hard_conflict` | no two evidence sources contradict each other |

The checks and their weights come from
`KYC_Tool_Build_Package/machine_readable/scoring_rubric.json`, the policy file
that the tool and its tests both read:

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

Five of those eight run today with no human involved: the registry match, the
ORG-ID match, the company email, any verified email and the LinkedIn match. A
case that passes all five scores 105, which clears the threshold on its own.
Website review is a human task by design. The other two are waiting on the
answers in §5.

Four verdicts come back: `approve`, `approve_buy_locked` (the account is fine,
buying stays locked until an ORG-ID verifies), `manual_review_insufficient`
and `reject`. That second one is how "ORG-ID optional at registration" works
in practice. Every check that does not pass carries a stable reason code
naming what is missing or wrong, and that code is what your review team acts
on.

Only a broker-blocklist match rejects a case on its own. Everything else that
falls short goes to manual review, so expect the review queue rather than the
reject pile to carry the volume. Sanctions screening happens on the platform
before the tool is called at all.

**When a source is down:** the run finishes as *partial*. Evidence already
gathered stands, nothing is guessed, and the next event re-checks. An outage
can delay a better verdict. It cannot produce a wrong one.

## 3. A case, end to end

A case is one registrant: the person signing up on behalf of a company.
Approving the case approves that person, not the company, so the sign-up event
has to say who they are. It carries `contact.name` and `contact.email` (the
address they signed up with, and the one `email.verified` confirms later) plus
the `platform_account_id` you hold for them. A second registrant at the same
company is a second case with its own id.

Send the RIR org handle whenever the registrant has one. That means
`org_id.submitted` right after the sign-up event, and again every time they
add or change the handle later. It is optional at registration, and the check
runs the moment the handle arrives.

Here is the quickest path a clean case can take. The points come from the
table above.

1. The user registers. The platform POSTs `kyb.run_requested` with the contact
   details and everything it collected about the company, including its
   `website`. The company-email and LinkedIn checks compare against that
   domain and cannot pass without it. The platform gets back
   `202 {"run_id": "…"}`.
2. When that run finishes, the first verdict reaches your webhook: the registry matched
   (25) and LinkedIn put the contact at that company (20), so the score is 45.
   The decision is `manual_review_insufficient`, with reason codes for what is
   still missing.
3. The user verifies their work email. The platform POSTs `email.verified`,
   which earns `verified_email` (10) and `verified_company_email` (25). Score
   80.
4. The user supplies their RIR org handle. The platform POSTs
   `org_id.submitted`, the RIR lookup passes (25), and the score reaches
   105 with all five gates green.
5. The computed decision is `approve`. While enforcement is off it arrives as
   `manual_review_insufficient` with that computed decision attached, and your
   registration team confirms it in the platform admin.

A document adds another 25 on top of that, once question 2 in §5 is answered.
A verified POC adds 25 more.

Every event except `reviewer.manual_approve` gets its own run and its own
callback. Deliveries retry until you answer 2xx, so dedupe on
`(case_id, run_id)`.

Evidence is optional at every step, and the tool scores whatever exists. When
a user later adds or changes something that matters, the platform re-sends the
matching event and verification runs again by itself. The full trigger table
is `PLATFORM_INTEGRATION.md` §3.

## 4. What your team builds

Four things:

1. **The decision webhook.** One HTTPS endpoint. Verify the signature, answer
   200, dedupe on `(case_id, run_id)`. See `PLATFORM_INTEGRATION.md` §4.
2. **The POC confirmation page.** The user types in the code and the reference
   from the verification email, and the platform posts both back to us. Codes
   are single-use, they expire after 72 hours, and they die if the user edits
   the identity details behind them. Recovery is always the same: submit the
   POC again. See `PLATFORM_INTEGRATION.md` §5.
3. **Admin views for held cases.** Your review team works in the platform
   admin, so each held case needs its decision, its score, its checks and its
   reason codes on screen. They come from the webhook body, or from
   `GET /v1/cases/{id}` if you would rather pull. Keep score and reason codes
   admin-only. Users see their status and the next useful step. See
   `PLATFORM_INTEGRATION.md` §7.
4. **Status notifications and reviewer assignment.** Emails to users, alerts
   to admins and routing a held case to a reviewer are platform features keyed
   off the webhook result. There is a suggested result-to-action mapping in
   `PLATFORM_INTEGRATION.md` §4.

## 5. The open questions

Two questions are open, and both are about work your platform may already do.
Either answer works for us. Neither blocks staging. Both block production: a
production process refuses to start while the document and email providers are
still stand-ins.

**Question 1. Can your platform send the POC verification email?**

Proving control of IP resources means sending a token to the address the
regional registry lists for that contact, never to an address the user typed.
The tool already finds that address itself over RDAP.

- **If yes:** the tool hands you the token, the registry-listed recipient and
  the case reference over a typed contract we write together, and it hosts
  nothing. Your own transactional email does the sending.
- **If no:** the tool sends the message through an Amazon SES identity that
  IPv4.Global provisions, which also needs a sending domain.

Either way the token rules stay ours. §4 item 2 lists them and
`PLATFORM_INTEGRATION.md` §5 has them in full. Until this is answered the POC
check cannot award its 25 points.

**Question 2. Can your platform read the fields off an uploaded document?**

The fields are the legal name, the registered address, the registration number
and the issuing jurisdiction, each as printed on the document rather than as
the user typed it.

- **If yes:** the platform writes them into the shared bucket as a small JSON
  object and posts `document.uploaded` pointing at it. The tool reads that
  JSON and compares it against the registries. No OCR runs anywhere.
- **If no:** the platform posts `document.uploaded` pointing at the original
  PDF or image, and the tool runs OCR itself. That needs an OCR provider
  chosen and contracted by IPv4.Global, plus agreed limits on file type and
  size.

The event on the wire is the same either way, so your sending code does not
depend on the answer. `PLATFORM_INTEGRATION.md` §6 has both paths in detail.
Until this is answered the document check cannot award its 25 points.

**Settled at kickoff: your team hosts it and runs it.**

IPv4.Global keeps maintaining the code and cutting releases. Your team pulls a
release and redeploys. Nobody edits code on the server.

- Python 3.11 and FastAPI. **PostgreSQL 14+ is the only hard dependency**,
  because the work queue and the webhook outbox both live in the database. No
  Redis, no message broker.
- Also needed: an S3 bucket for evidence files, and outbound HTTPS to the
  public registries.
- Four small stateless processes, any of which scales horizontally:

| Process | Command |
|---|---|
| API | `uvicorn kyc_tool.api.app:create_app --factory` |
| Pipeline worker | `python -m kyc_tool.workers.pipeline_worker` |
| Outbox publisher | `python -m kyc_tool.workers.outbox_worker` |
| Retention (daily cron) | `python -m kyc_tool.workers.retention` |

- A `Dockerfile` ships in the repo. An update is: pull the release, build the
  image, run `alembic upgrade head`, restart the processes. Migrations are
  versioned. Several are forward-only once real data exists, starting with
  migration 010 (PR 5a), and `docs/DEPLOYMENT.md` §6 lists every one.
- `GET /readyz` for the load balancer, which checks the database, the
  migration version and storage, plus the configuration in production mode.
  `GET /healthz` for liveness.
- Configuration is environment variables with a `KYC_` prefix, listed in
  `docs/RUNBOOK.md` along with dead-letter recovery and the ops console. The
  Companies House key is the one exception: `CH_API_KEY`.
- With `KYC_ENVIRONMENT=production` the tool refuses to boot on unsafe
  configuration, a missing secret or a stand-in provider, and it lists every
  violation at once. A bad deploy fails loudly instead of running quietly
  broken.

## 6. Staging plan

Staging runs with **automation on** (`KYC_ENFORCE_POSITIVE_DECISIONS=true`) so
that the real end state gets a rehearsal. That is safe **only** because
staging is closed: reachable by our own tests, never by an untrusted caller.
It is a rehearsal and not the production go-live. The M2 gate (real adapters
end to end, hardened signing, platform cutover) still governs turning
automation on in **production**, which launches with it off. The tool
investigates, the review team confirms, and the flag flips per environment
once staging has proven out.

> **Keep staging closed until inbound v1 is actually disabled.** PR 5a adds
> path-bound HMAC v2, but a v1-only request during the dual-accept window is
> still path-unbound, so a signed event captured inside the skew window could
> be replayed to a different case. The redirect closes for v2 at deploy. For
> everyone else it closes only once inbound v1 is disabled, meaning the
> zero-witness is satisfied and `hmac_v1_inbound_sunset_at` takes effect. Keep
> staging's perimeter closed until then, not merely until PR 5a ships.

Checklist:

1. RDS Postgres 14+, an S3 bucket, and an ECS/Fargate service (or one EC2 box)
   for the four processes.
2. Build the image from the repo `Dockerfile`.
3. Generate the HMAC secrets into AWS Secrets Manager and set them on both
   sides: the v1 legacy `KYC_PLATFORM_HMAC_SECRET`, plus the v2 pairs
   `KYC_HMAC_INBOUND_KEY_ID`/`KYC_HMAC_INBOUND_SECRET` and
   `KYC_HMAC_OUTBOUND_KEY_ID`/`KYC_HMAC_OUTBOUND_SECRET`
   (`PLATFORM_INTEGRATION.md` §2 and §4). Staging boots in development mode
   without the v2 set, but dual-accept is the thing staging exists to
   rehearse, so configure it on both sides.
4. Core env vars: `KYC_DATABASE_URL`, `KYC_PLATFORM_CALLBACK_URL` (your
   staging receiver), `KYC_OBJECT_STORE=s3` with `KYC_S3_BUCKET`,
   `KYC_ENFORCE_POSITIVE_DECISIONS=true`, and `CH_API_KEY` (§8 item 14),
   because the registry lookups are live.
5. Leave `KYC_ENVIRONMENT` at `development` for now. Registry lookups are
   live. The stand-ins are the two open questions: documents arrive as
   extracted JSON, and the POC directory the pipeline ships is empty, so a
   `poc.submitted` in staging cannot pass until the live directory is
   switched in (§8, not built yet). For that day set
   `KYC_EMAIL_PROVIDER=file`, which appends each verification email as a JSON
   line to a local sink file (`.substrate/state/poc-emails.log` by default,
   moved with `KYC_EMAIL_FILE_PATH`), so your tests can read the token and the
   reference and finish the round-trip. That sink writes raw tokens to disk,
   so it is for closed staging only and production refuses it at boot. Live
   providers land later without changing your integration.
6. `alembic upgrade head`, start the processes, check `/readyz`.
7. Smoke test: send a signed `kyb.run_requested` and watch the verdict arrive.
   Until your receiver exists, `scripts/dev_receiver.py` is a stub that prints
   incoming callbacks.

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
                "website": "https://acme.example",
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

- Re-send the same bytes with the same `Idempotency-Key` and you get `200`
  with the stored response, no duplicate run. That is the retry safety to
  build on.
- That example signs v1. `PLATFORM_INTEGRATION.md` §2 has the v2 recipe and a
  worked vector. Its §11 has the conformance kit, whose `vector` mode prints
  the vector from the signed contract PDF (a different one from the §2
  example) so you can hold your own signer against it offline.
- `GET /v1/cases/case-001` shows the live checks and reason codes after each
  event.
- POC round-trip, once the live directory is switched in (§6 step 5): after
  `poc.submitted`, read the token and the verification reference out of the
  email sink file, then POST `poc.token_verified` with both.
- The ops console (`/ui`, switched on with `KYC_UI_ENABLED=true` in staging)
  shows every case with its score, its gates and its run state, and it can
  compose signed test events from the browser.

## 8. Where things stand, and what we need from you

Nothing here says the service is production-ready. The go/no-go gate is §8 of
`docs/superpowers/specs/2026-09-12-production-readiness-design.md`. Status at
this release falls into five states.

**Working now**

- Signed ingestion of all nine event types, with the typed request and
  response contract published in `/openapi.json`, and idempotent replay of a
  repeated key.
- Decision webhook delivery, at-least-once, with retries and dead-lettering.
- The read API and the pull-style Salesforce projection
  (`PLATFORM_INTEGRATION.md` §7).
- Website review and manual approval as signed events, plus the operator
  console with its live configuration and Salesforce destination-name mapping.
- Live registry adapters: Companies House (which needs the key in item 14),
  GLEIF and the five RIR RDAP strategies. The POC directory lookup runs over
  that same RDAP path, verifying the association on the org or resource record
  and reading the registry-listed email off it. Only the token email is
  missing (§5, question 1).
- Reviewer information requests. A reviewer can record what a stalled case
  needs, most often the RIR org handle, and `GET /v1/cases/{id}` serves it as
  `information_requested`.
- The conformance kit you can run against staging.
  `python -m kyc_tool.conformance` checks your signer offline, your sender
  against a running tool, and your decision receiver against real signed
  callbacks (`PLATFORM_INTEGRATION.md` §11).
- S3-compatible evidence storage, and the boot check that refuses stand-in
  providers and unsafe configuration in production.

**Built and deliberately switched off**

- Ordered callback delivery. The tool numbers each case's decisions
  internally, but that number does not reach the wire until migration 025 is
  bootstrapped from your accepted-run ledger and activated. The interim
  receiver rule in `PLATFORM_INTEGRATION.md` §4 applies until then.
- Enforcement of positive decisions. It stays off at launch, so a computed
  approval arrives as `manual_review_insufficient` carrying `enforcement_held`
  (`PLATFORM_INTEGRATION.md` §4).
- Retirement of the v1 signature. Both versions are accepted today, and the
  sunset dates are set at cutover.

**Blocked on the two questions in §5**

- The POC verification email. The directory lookup works and the token rules
  work. Nothing sends the message yet.
- The document check. No OCR engine is connected, and the one engine in the
  tree reads extracted JSON, which a production process refuses to start on.

**Not built yet, for reasons of our own**

- The production provider profile: the switch that hands the pipeline the
  built POC directory, and whichever providers your two answers call for. The
  live Floqer client already exists and carries over.
- A downloadable versioned contract bundle.
- The load and soak harness.
- Migration 025, once item 7 below is answered.

**Out of scope by design**

- The tool never writes Salesforce, never judges a website automatically,
  never replaces your platform UI and never changes a historical decision when
  configuration or mappings change.

The rest of this section is numbered so that either side can cite an item by
number: five configuration values from you, eight answers we need, and two
things IPv4.Global owes.

**Your configuration** (through the deployment secret manager or written
deployment config, never chat)

1. Callback base URLs, staging and production.
2. Key ids and the HMAC secrets for each wire direction, through the secret
   manager.
3. S3 bucket, key prefixes and IAM ownership for evidence and extracted JSON.
4. The Salesforce sandbox and the platform service that will consume the
   projection.
5. AWS deployment access for whoever on your side deploys.

**Your answers** (write each one down and we turn it into a tested contract)

6. Your callback receiver's behaviour: that it commits the decision before
   returning 2xx, and how it dedupes on `(case_id, run_id)`.
7. The ordering bootstrap, which migration 025 cannot be planned without:
   - where the platform's accepted-run ledger lives
   - how we query the accepted decision for any case
   - how manual approvals and reverted decisions appear in it
   - who signs the bootstrap response, and how that signer is identified
   - the maximum bootstrap size, and the recovery procedure
8. Review-task changes. The default is a webhook you host for a task opened,
   completed or cancelled. If you would rather poll
   `GET /v1/review-tasks?status=open`, say so, and tell us the cursor rule,
   the freshness you need and what should happen after a missed poll.
9. Document extraction. Can your platform extract the four fields and send
   them as JSON? If it cannot, the tool runs OCR and IPv4.Global contracts an
   OCR provider. §5 asks the question in full, and `PLATFORM_INTEGRATION.md`
   §6 has both wire paths.
10. POC verification email. Can your platform send it from its own
    transactional email, given the token and the registry-listed recipient
    over a typed contract? If it cannot, the tool sends through an SES
    identity that IPv4.Global provisions. Confirm as well that you host the
    POC page and echo back both `token` and `token_id`. §5 asks the question,
    `PLATFORM_INTEGRATION.md` §5 has the wire detail.
11. Floqer. The contract is in place, and the tool calls a published shortcut
    in IPv4.Global's own Floqer account for discovery only, which feeds the
    LinkedIn match. Nothing is needed from the platform.
12. Operating targets: expected daily and peak case volume, concurrent runs,
    acceptable latency for light and full checks, soak duration, deployment
    region, maintenance-window constraints, and the availability and recovery
    objectives you hold us to.
13. Reviewer information requests. When a case stalls for evidence the
    registrant never supplied, most often the RIR org handle, a reviewer
    records the ask in the console and `GET /v1/cases/{id}` serves it as
    `information_requested` (`PLATFORM_INTEGRATION.md` §7). How would you
    rather hear about one: poll that field, or a new webhook message we send
    you? We build the webhook only once you answer. The ask is recorded either
    way.

**From IPv4.Global**

14. The Companies House API key, through the secret manager.
15. Email: if the tool ends up sending, an SES identity and a sending domain
    in the secret manager and deployment config.

## 9. Doc map

| Question | Doc |
|---|---|
| The three documents in one file, generated from them | `docs/TECHCRAFT_HANDOFF.md` |
| Full API contract, signatures, payloads, webhook | `docs/PLATFORM_INTEGRATION.md` |
| Deploying, releasing, rollback, monitoring | `docs/DEPLOYMENT.md` |
| Operating it: env vars, health, dead letters, console | `docs/RUNBOOK.md` |
| How scoring and decisions work, in depth | `docs/OVERVIEW.md` |
| Salesforce field-by-field mapping and value rules | `docs/SALESFORCE_MAPPING.md` |
| Normative spec and policy files | `KYC_Tool_Build_Package/` |

# Part 2

# Platform Integration Guide — MVP

For the IPv4.Global platform team. Everything needed to integrate the KYC tool:
the API you call, the two things you build (a webhook receiver and a POC
confirmation page), the MVP scope, and the two questions we need answered.

## 1. The model

A case is one registrant: the person signing up on your platform on behalf of a
company. You send us what they told you, we check it against public registries,
and we send back a verdict your registration team can act on.

The tool is an async verification service. You POST events (registration data,
a verified email, an uploaded document, an ORG-ID). Each event is acknowledged
immediately and processed in the background: the tool gathers evidence, scores
it, and POSTs a decision to your webhook.

Decisions: `approve`, `approve_buy_locked` (account OK, purchasing held until
ORG-ID verifies), `manual_review_insufficient`, `reject`.

`platform_account_id` on the sign-up event is your id for that person, and
every decision we send back is about that case, so about that contact. Company
evidence (registry records, ORG-ID, website) is about the company they claim.
Contact evidence ties the person to it: an inbox at the company's domain, a
LinkedIn profile showing them at that company, later the RIR contact token.
Approving a case approves the contact, not the company. A second registrant at
the same company is a second case, with its own `case_id`.

**MVP posture:** auto-enforcement is off. A computed `approve` /
`approve_buy_locked` is delivered as `manual_review_insufficient` with an
`enforcement_held` marker (§4), and the registration team confirms it. Flipping
enforcement on later changes no part of this contract.

## 2. Authentication (both directions)

Nothing is accepted unsigned, in either direction. Your requests to us and our
webhook to you carry the same two headers:

```
X-KYC-Timestamp: <unix seconds, e.g. "1752681600">
X-KYC-Signature: <hex HMAC-SHA256(secret, timestamp + "." + raw_body)>
```

- The signed message is the timestamp string, a literal `.`, then the **raw
  request body bytes**. Sign the exact bytes you send. Verify the exact bytes
  you receive, before any JSON parsing.
- Requests older or newer than 300 seconds are rejected, so keep clocks on NTP.
- Compare signatures constant-time.
- v1 uses one shared secret per environment (staging ≠ production), ≥ 32 chars.
  v2 (below) splits this into **separate inbound (platform→tool) and outbound
  (tool→platform) secrets**, each with a `key_id`, so the two directions rotate
  independently.

Verify in Python:

```python
import hashlib, hmac, time

def verify(secret: str, timestamp: str, body: bytes, signature: str) -> bool:
    if abs(time.time() - float(timestamp)) > 300:
        return False
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + body,
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
```

### v2 signing (path-bound) — dual-accept now

v1 does not bind the URL, so a captured signature can be replayed to a different
case. **v2** signs the method and path too. We accept **both** during a
dual-accept window and give you fixed sunset dates, so you can take up v2 at
your own pace without anything breaking. Headers change to:

```
X-KYC-Timestamp: <unix seconds>
X-KYC-Key-Id:    kyc-platform-1        # a constant from your config
X-KYC-Signature-V2: <hex HMAC-SHA256(secret, canonical)>
```

The `canonical` value is 8 newline-joined lines (no trailing newline):

```
v2
<key_id>
platform->tool
<HTTP method, e.g. POST>
<raw path + "?query" if any, e.g. /v1/cases/acme-1/events>
<timestamp>
<Idempotency-Key for event POSTs, else empty>
<hex sha256 of the raw body>
```

One recipe covers everything you send. The only per-request variables are the
method, path, timestamp, idempotency key (empty on GETs), and body hash:

```python
import hashlib, hmac

def sign_v2(secret, *, key_id, method, path_qs, timestamp, slot, body: bytes):
    canonical = "\n".join([
        "v2", key_id, "platform->tool", method, path_qs, timestamp, slot,
        hashlib.sha256(body).hexdigest(),
    ])
    return hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()

# worked vector — sign_v2(secret="s", key_id="kyc-platform-1", method="POST",
#   path_qs="/v1/cases/acme-1/events", timestamp="1000.0", slot="idem-1",
#   body=b'{}')
#   == "16a499257960ec379a4621c31f12a986c252343459d7edc0de14f26b742719e6"
#   Reproduce this exact hex before going live to confirm byte-for-byte parity.
```

Rules: **if you send any v2 header, the request must be complete, valid v2.** We
do not fall back to v1 for a v2-labelled request, and sending *any* v2 header
(even present-but-empty) locks the request to v2. `key_id` is a constant from
your config and changes only on a secret rotation. Our webhook callbacks
dual-emit both signatures until the outbound sunset, so your receiver can
migrate whenever it is ready.

## 3. Sending events

Everything you tell us arrives as an event on one endpoint. There is no
registration call and no re-verify call: you post what happened, and the tool
decides again.

```
POST /v1/cases/{case_id}/events
Idempotency-Key: <unique string per event attempt>
X-KYC-Timestamp / X-KYC-Signature: as above
```

`case_id` is your identifier for the registrant, stable across every event for
that person. Cases are created on the first event, one per registrant.

Envelope (exactly these four keys):

```json
{
  "event_type": "kyb.run_requested",
  "occurred_at": "2026-07-16T12:00:00Z",
  "actor": {"type": "user", "id": "platform-user-123"},
  "payload": { ... }
}
```

`actor.type` is `user`, `reviewer`, or `system`. A fifth key at the top level
of the envelope is rejected 422, while unknown fields inside `payload` are
accepted and preserved.

### Responses

| Code | Meaning | Handling |
|---|---|---|
| 202 | Accepted. Body `{"run_id": ..., "status": "queued"}` | done — result comes by webhook |
| 200 | Replay of an already-processed key, stored response returned | safe retry, done |
| 400 | Missing `Idempotency-Key` | fix request |
| 401 | Bad/missing signature or stale timestamp | fix signing |
| 409 | Same `Idempotency-Key`, different body, or the review task named by a `website.review_completed` is on another case or not open | bug on your side — never reuse keys |
| 422 | Payload failed validation, or an unknown top-level envelope key | fix payload |
| 404 | The review task named by the event does not exist | fix the task id |
| 503 | Configuration unavailable, or the v1 signature witness could not be recorded | safe retry |

Retry on network failure with the **same** key and **same** bytes. You get 200
back rather than a duplicate run.

### Event types and payloads

| event_type | Payload (required unless noted) | Notes |
|---|---|---|
| `kyb.run_requested` | `company_legal_name`, `contact`, `platform_account_id`; optional `address`, `registration_number`, `jurisdiction`, `website` | send at registration, runs the full check set. `contact` is the registrant: an object with required `name` and `email` (the address they signed up with) and optional `title`, `first_name`, `last_name`. The LinkedIn check compares the person's name and the company domain, using `email` and the split names to find the right profile. Company name and title are recorded for the reviewer, never compared. `website` is optional to ingest, but `verified_company_email` and `linkedin_company_match` compare against its domain and cannot pass without it |
| `email.verified` | `email`, `domain`, `verified_at` | you own email verification, and this asserts it happened. `email` must be the contact's sign-up address (`contact.email`). The tool records it as sent and does not cross-check the two |
| `org_id.submitted` | `rir`, `org_handle` | `rir` ∈ `arin, ripe, apnic, lacnic, afrinic`. Optional at registration. Send it right after the sign-up event when the registrant supplied a handle there, and again whenever they add or change one later. The check runs the moment a handle arrives. A reviewer may also record one in the operator console when the contact supplies it by other means, and the envelope's `actor` says which (`reviewer` rather than your own `user`/`system` actor) |
| `poc.submitted` | `rir`, `poc_handle`; optional `org_handle`, `resource` | starts the verification email (§5) |
| `poc.token_verified` | `token_id`, `token`, `verified_at` | posted by your confirmation page (§5) |
| `document.uploaded` | `object_ref`, `doc_type` | see §6 |
| `website.review_completed` | `task_id`, `result`, `reviewer_id`; optional `reason_codes` | completes a website review task (§7). Needs a matching reviewer actor (below) |
| `reviewer.manual_approve` | `reviewer_id`; optional `note` | answers 200 inline with the case state. No run, no callback. Buying stays locked without a verified ORG-ID. Needs a matching reviewer actor (below) |
| `recalculate.requested` | `{}` | re-scores from stored evidence, with no new fetches |

Send events in the order they happen. Each one except `reviewer.manual_approve`
triggers its own run and its own decision callback.

### Reviewer actor requirement (`website.review_completed`, `reviewer.manual_approve`)

A valid signature proves a request came from you. It does not say who acted,
and these two events need that: the signed envelope's `actor` must identify the
reviewer.

- `actor.type` MUST be `"reviewer"`.
- `actor.id` MUST equal the payload's `reviewer_id` — both **nonblank** after
  trimming whitespace, compared **exact, case-sensitive**. A matching pair of
  blank strings authorizes nothing.
- A mismatch, a blank id on either side, or the wrong `actor.type` is rejected
  **422** — the request is authenticated (it carried a valid signature) but
  internally inconsistent, so it is not a 401/403.
- The tool records the **actor-derived** reviewer identity (`actor.id`) as the
  reviewer of record on the review task, on the check it writes and in the
  audit trail. The payload's `reviewer_id` field is never the record. Send
  both, and make them match.

### When to send each event

Evidence is optional at every step, and the tool scores whatever exists. Your
whole job is this: when verification-relevant information is added **or
changed**, send the matching event. The tool re-runs and returns a fresh
verdict, so there is no separate "retry" or "re-verify" call.

| Moment on the platform | Send |
|---|---|
| Registration submitted | `kyb.run_requested` with everything collected |
| User verifies their email | `email.verified` |
| User adds or changes an ORG-ID | `org_id.submitted` |
| User adds or changes a POC claim | `poc.submitted` |
| User completes the emailed-code page | `poc.token_verified` |
| Document uploaded or replaced | `document.uploaded` |
| Company details edited (name, address, registration number) | `kyb.run_requested` again, with the updated data |
| Admin approves manually | `reviewer.manual_approve` |
| Fresh verdict wanted, nothing new | `recalculate.requested` |

Changing identity details (ORG-ID, POC) suspends previously earned proof until
re-verified, so a score can drop after an edit (§5). Expected, not a bug.

## 4. The decision webhook (you build this)

This is the endpoint we POST every verdict to, and the first thing to build.
One body carries the decision, the score behind it and the reason codes for
each check.

Expose HTTPS `POST {your_base_url}/kyc/decision`. We sign it per §2, dual-emitting
v1 (the legacy shared secret) and v2 (the dedicated **outbound** secret + key id)
until the outbound sunset, then v2 only. Two lines of the §2 canonical differ
on this direction: it reads `tool->platform`, and the idempotency-key line is
empty. The v2 signature binds the **literal** request path, so if
`{your_base_url}` has a path prefix (e.g. `…/hooks`), we sign
`/hooks/kyc/decision` rather than `/kyc/decision`. Verify against the full path
you received. Respond 2xx to acknowledge. Anything else and we retry.

Body:

```json
{
  "case_id": "your-case-id",
  "run_id": "…",
  "event_id": "…",
  "decision": "manual_review_insufficient",
  "score": 110,
  "gates": {
    "score_met": true,
    "legal_proof": true,
    "control_proof": true,
    "broker_ok": true,
    "no_hard_conflict": true
  },
  "buy_enablement": "locked_org_id_required",
  "checks": [
    {"type": "official_registry_match", "status": "pass", "points": 25,
     "source": "companies_house", "reason_codes": []}
  ],
  "decided_at": "2026-07-16T12:00:05Z",
  "enforcement_held": {
    "computed_decision": "approve_buy_locked",
    "reason": "positive_enforcement_disabled"
  }
}
```

- `buy_enablement` is `enabled` or `locked_org_id_required`.
- `checks[].reason_codes` are stable strings explaining any non-pass — show
  them to your registration team.
- `enforcement_held` appears only while MVP enforcement is off: it carries the
  decision the tool computed. Treat the case as pending human review.
- **Delivery is at-least-once.** Dedupe on `(case_id, run_id)`. Retries back
  off exponentially (base 10 s, 8 attempts) before dead-lettering on our side.
  Acknowledge every exact valid duplicate as processed. Acknowledging a
  callback is a different act from applying it to the case.
- **Until ordered delivery is activated in migration `025`, the wire provides no
  callback-order authority.** Keep a manual approval authoritative. Acknowledge
  and record subsequent valid automatic callbacks, but hold unordered callbacks
  for review instead of applying them. If different automatic callbacks conflict,
  use an ordering authority the platform owns or hold them for review.
- Never infer callback order from `decided_at` or `event_sequence`.
  `decided_at` is a display timestamp, and `event_sequence` is only the per-case
  ingest ordinal of the event that triggered the run. Migration `025` introduces
  the separate `decision_sequence` callback-order authority after its governed
  activation.

### What to do with each result

Notifications, reviewer assignment, and user-facing screens are platform
features. The tool supplies the statuses. Suggested mapping:

| Result | Suggested platform handling |
|---|---|
| `approve` | activate the account; notify the user |
| `approve_buy_locked` | activate; notify the user with the ORG-ID prompt |
| `manual_review_insufficient` **with** `enforcement_held` | "ready to confirm" queue — the tool computed a positive; an admin confirms (MVP only) |
| `manual_review_insufficient`, no marker | manual-review queue; assign a reviewer; notify admins |
| `reject` | admin notification; user handling per ops policy |

Two boundaries shape the volume. Only a broker-blocklist match ever
auto-rejects, so expect the review queue rather than rejections to fill up. And
sanctions screening happens on the platform **before** the tool is called, so a
sanctioned registrant never reaches it.

**Display guidance.** Per case you have: decision, buy state, score, the five
gate booleans, and per-check status with reason codes. Show users the status
and the next useful step (verify your email, add your ORG-ID). Keep the score,
the gate booleans and the reason codes in admin views, because publishing
exactly why a check fails makes it easier to game. Wording is yours, and the
reason codes are stable strings safe to key copy on.

## 5. POC verification page (you build this)

Proving that a registrant controls IP resources means sending a code to the
address their regional registry lists, and having them type it back. You host
the page they type it into. Who sends that email is the first open question.

1. You post `poc.submitted`.
2. The tool looks up the POC in the registry directory over RDAP: the
   submitted ORG-ID or resource record must itself list the POC handle, and the
   address comes off the POC's registry record. Whichever side sends the email,
   it goes to the **registry-listed** address, never a user-supplied one. The
   email contains:
   `Your verification token: <secret>` and `Verification reference: <id>`.
3. The user enters both on your confirmation page.
4. You post `poc.token_verified` with `token` (the secret) and `token_id` (the
   reference). Both are required, and a placeholder `token_id` fails.
5. Result arrives as a normal decision callback.

Rules your page must respect:

- **Single-use.** A verified token is spent (`poc_token_consumed` on reuse).
- **Expires in 72 hours** (`poc_token_expired`).
- **Bound to the submitted identity.** If the user changes their ORG-ID, POC
  handle, or resource after the email went out, the old token fails
  (`poc_token_binding_mismatch`).
- Recovery is always the same: re-submit the POC (`poc.submitted` again), which
  cancels old tokens and sends a fresh email. Don't build a "resend same code"
  button.
- Changing identity details also suspends previously earned proof: expect
  scores to drop after an ORG-ID/POC edit until re-verified
  (`org_id_revalidation_pending`, `poc_not_associated`). Not a bug.

**Open question: can your platform send that email?**

- **If yes** — the platform sends it through its existing transactional email.
  We hand you the token, the registry-listed recipient and the case reference
  over a typed delivery contract still to be written, and we host no mail.
- **If no** — the tool sends it through an Amazon SES sender that IPv4.Global
  provisions with an identity and a sending domain. That sender is not built,
  and a production process refuses to boot while the email provider is the dev
  stub.

Either way the token rules above are enforced by the tool, and the POC check
cannot pass until the question is answered.

## 6. Documents (open question: who reads the fields?)

A registrant can upload a formation document. Before the tool can compare it
against what the user typed, someone has to read four fields off it: legal
name, address, registration number, jurisdiction. Which side reads them is the
second open question. The kickoff call leaned toward the platform.

**Open question: can your platform extract those four fields?**

- **If yes** — the platform stores the upload (your existing virus scanning
  and quarantine unchanged), writes the four fields as a JSON object to the
  shared object store, and posts `document.uploaded` with `object_ref`
  pointing at that JSON. The tool runs no OCR and reads that JSON as posted.
  The numbered steps below are this path's contract.
- **If no** — the platform stores the upload and posts `document.uploaded`
  with `object_ref` pointing at the **original file** (PDF or image) in the
  shared object store. The tool runs an OCR engine and extracts the same four
  fields itself. That path needs an OCR provider chosen and contracted by
  IPv4.Global, plus agreed file-type and size limits. None of it is built: the
  current engine reads extracted JSON only, and production refuses that stub.

`document.uploaded` is the same event on the wire either way, so your sending
code does not wait on the answer. Documents are optional at registration. A
case scores without them, and a later upload re-runs verification (§3).

1. Put a JSON object in the shared object store:
   `{"fields": {"name": "...", "address": "...", "number": "...", "jurisdiction": "..."}}`

   | Key | Meaning |
   |---|---|
   | `fields.name` | legal name exactly as printed on the document |
   | `fields.address` | registered address as printed |
   | `fields.number` | registration / company number as printed |
   | `fields.jurisdiction` | issuing jurisdiction, e.g. `GB` |

   Each key is individually optional, and anything missing routes toward review
   rather than toward a pass. Extract what the document says, not what the user
   typed. Comparing the two is exactly the tool's job.
2. Post `document.uploaded` with `object_ref` (storage key) and `doc_type`
   (`registration_certificate` for formation/registration documents, and more
   types can be added as needed).
3. Keep the original upload on your side for audit.

Staging runs on hand-extracted JSON until you answer. Production cannot start
on either path before then.

## 7. Read API and review tasks

Everything the tool knows about a case is readable over signed GETs. Use them
to chase what is missing and to mirror a case into Salesforce.

- `GET /v1/cases/{id}` — status, score, latest decision, live checks with
  reason codes ("what's missing" for follow-up), and `information_requested`
  (below).
- `GET /v1/cases/{id}/checks?all=1` — full check history.
- `GET /v1/runs/{id}` — one run's state and adapter results.
- `GET /v1/review-tasks?status=open` — open human-review tasks (website
  checks, hidden POC email).
- `GET /v1/cases/{id}/salesforce-projection` — the Salesforce-shaped view of a
  case: every `salesforce_sync_fields.json` destination with its value already
  mapped, keyed by the destination name the console currently has saved
  (below).

**Completing a website review** is a normal signed event rather than a separate
endpoint: post `website.review_completed` to `POST /v1/cases/{case_id}/events`
with payload `{"task_id": "...", "result": "pass"|"fail", "reviewer_id": "..."}`.
The envelope's `actor` must identify the same reviewer (`actor.type:
"reviewer"`, `actor.id` equal to `reviewer_id`, §3) or the event is rejected
422, and the actor-derived reviewer is the one recorded. The tool checks that
the task exists, that it is a website task on that case, and that it is open
(else 404/409/422). The state change, the check and the audit row are identical
to any other event. (The old `POST /v1/review-tasks/{id}/complete` endpoint is
retired — it duplicated this event.)

**What a reviewer has asked the contact for.** `GET /v1/cases/{id}` carries
`information_requested`: one entry per outstanding ask, shaped `{"field":
"org_id" | "registration_number" | "address" | "poc", "requested_at": …,
"requested_by": …, "note": … or null}`. A reviewer raises one from the operator
console when a case is stuck for want of evidence the registrant never
supplied. The tool records the ask, and the platform owns the message that
reaches the contact. An entry drops off by itself once the case receives that
evidence, so there is nothing to close and nothing to acknowledge. `org_id`
clears when `org_id.submitted` arrives, and the other three clear the same way.

You do not need this field to chase a missing ORG-ID today. The decision
webhook's `checks[].reason_codes` already carry `org_id_submission_incomplete`,
which is the same fact at decision time. How you would rather learn of a
reviewer's request is `PLATFORM_BRIEFING.md` §8 item 13: poll this field, or
have us send you a message. Nothing outbound is built until you answer.

### Salesforce projection (pull)

`GET /v1/cases/{case_id}/salesforce-projection` is how the platform reads the
Salesforce-shaped view of a case. It is a pull: call it after a callback, or on
your own schedule, for the case you are about to mirror. The tool never writes
Salesforce. This endpoint is the only sanctioned source for the mirror, and
nothing in a production integration reads the console's `/ui/api/…` routes.

One response is one consistent snapshot, taken in a single read-only
transaction. A mapping saved in the console while the request is in flight
cannot produce a body that is half old and half new.

| Field | Meaning |
|---|---|
| `case_id` | the case |
| `fields` | object keyed by the **destination** name currently saved in the console. Each entry carries `source_field` (the canonical `salesforce_sync_fields.json` name, e.g. `KYC_Status__c`), `source_identity` (the tool value it came from, e.g. `case.status`), `value_type` (`enum`, `integer`, `text`, `boolean`, `datetime`, `check_records`), `nullable`, and `value` |
| `mapping_revision` | the active configuration revision the destination names were read from. `null` when no live configuration has been activated (destinations then equal the canonical names) |
| `configuration_revision` | the configuration revision the pointed automatic decision ran under, and present only for a resolved automatic decision, otherwise `null` |
| `decision_authority` | which decision the values come from: `provenance`, `decision_row_id`, `run_id`, `decision_kind` (`automatic` or `manual`), `decision`, `run_provenance` |
| `manual_approval_authority` | the latest manual approval, if any: `provenance`, `decision_row_id`, `reviewer_id`, `decided_at` |
| `projection_timestamp` | RFC 3339 UTC timestamp of the snapshot |

Statuses: `200`, `401` (signature invalid, missing, or retired), `404` (unknown
case), `503` (configuration or the v1 signature witness unavailable, so retry).
Error bodies are `{"detail": "..."}`. The exact schema is
`SalesforceProjectionResponse` in `/openapi.json`.

A console mapping change affects the next read. It never rewrites earlier
decisions or callback bytes, and it does not by itself prove the platform
adopted the new destination names. `docs/SALESFORCE_MAPPING.md` has the
per-field value rules.

In production these reads also require the §2 signature headers. The
registration team also has an operator console at `/ui` (dashboards, case
detail, review queue), independent of this API.

## 8. Hosting and deployment (you run this too)

You host and operate the tool in IPv4.Global's AWS account, and IPv4.Global
maintains the code and cuts releases. An update is: pull the release, build the
image, run the migration, restart. No code is edited on the server. A
`Dockerfile` ships in the repo, and `docs/RUNBOOK.md` is the operator guide
(every env var, health checks, dead-letter recovery).

- **Stack:** Python 3.11, FastAPI. **PostgreSQL 14+ is the only hard
  infrastructure dependency** — queue and webhook outbox live in Postgres. No
  Redis/broker.
- **Also needed in production:** an S3-compatible bucket (evidence), outbound
  HTTPS (RDAP registries, Companies House, GLEIF), and an email provider if the
  tool ends up sending the §5 email.
- **Processes** (stateless, scale horizontally): API (`uvicorn
  kyc_tool.api.app:create_app --factory`), pipeline worker, outbox worker, and
  a daily retention cron.
- **Deploy:** `alembic upgrade head`, start processes. Wire `GET /readyz` to
  the load balancer (checks DB, migration version, storage, and the config in
  production mode), and use `GET /healthz` for liveness.
- Config is environment variables prefixed `KYC_` (full table:
  `docs/RUNBOOK.md`). With `KYC_ENVIRONMENT=production` a misconfigured process
  refuses to boot and lists every violation — intentional fail-closed.

## 9. MVP scope and what comes later

What is built today, and what each missing piece waits on.

Works now: the full event flow, the registry, ORG-ID, broker, LinkedIn,
document (extracted-fields) and email checks, scoring, webhooks, the review
queue, the audit trail and idempotent replays.

| Added later | Unblocked by |
|---|---|
| Tool-side OCR of raw files | extraction decision + engine choice |
| Live POC verification emails | the §5 answer, then an email provider + sending domain, or the platform hand-off contract |
| Companies House lookups in your deployment | `CH_API_KEY` in the deployment config (`PLATFORM_BRIEFING.md` §8 item 14). The adapter is live |
| LinkedIn matching in your deployment | the Floqer key and shortcut id in the deployment config. The adapter is live, and nothing is needed from the platform |
| `event_sequence` in callbacks | your confirmation |
| v1 signature retirement (v2 path-bound signing is live now, §2) | agreed inbound/outbound sunset dates |
| Auto-enforcement (the flag flip) | staging end-to-end on real providers + platform cutover sign-off |

None of these change the API in §§2–7.

## 10. Answers we need

Every answer we still need from the platform team and from IPv4.Global is in
`docs/PLATFORM_BRIEFING.md` §8. Its §4 lists what your team builds, and its §5
holds the two questions still open.

Secrets never travel in chat, email, tickets, or documents: use the deployment
secret manager.

## 11. Conformance kit

Both sides can check themselves against this contract before anything goes
live. The kit ships in the repository and runs from a checkout as
`python -m kyc_tool.conformance <mode>`. It derives every expected status,
header, event, and key from the same authorities the published contract is
verified against, so it cannot drift from what you are reading here without a
test failing.

| Command | What it proves |
|---|---|
| `python -m kyc_tool.conformance vector` | your signer: recomputes the worked v2 vector published in the signed integration contract (§4 there) byte for byte, offline, so you can hold your own implementation against it. Run this first. |
| `python -m kyc_tool.conformance send --tool <base-url> --case <case-id>` | your sender: signs and posts one of every accepted event type (§3, plus the §7 website-review completion), v2 path-bound, plus one v1 request while dual-accept lasts (`--no-v1` once your inbound v1 sunset has passed). Then the negatives: bad signature 401, timestamp outside the 300 s window 401, a sign-up with no `contact` 422, an unknown event type 422, the same `Idempotency-Key` twice 200 with the stored body verbatim. Then a signed `GET /v1/cases/{id}` carrying `status`, `score`, the latest decision, the live checks, and `information_requested`. |
| `python -m kyc_tool.conformance receive --port <n>` | your receiver: the kit stands in as a §4 receiver. It verifies v1 and v2 (v2 against the literal request path), validates the callback body, dedupes on `(case_id, run_id)`, answers 2xx, and prints one PASS/FAIL line per rule per callback. Point a staging tool's callback URL at it. It listens on 127.0.0.1 only, so reach it from staging through an SSH tunnel (`ssh -R`) or a local forward. Do not expose it, because it authenticates nothing. Unlike a production receiver it acknowledges an invalid callback (2xx) instead of holding it, so a broken rule is reported once rather than retried eight times. |

Every mode prints a `check / expected / got / PASS|FAIL` table. `vector` and
`send` exit non-zero if any row FAILs, so both drop straight into CI. `send`
writes real events, so give it a throwaway case id on staging.

Secrets are read from the **environment only** — never a command-line argument,
never printed, never logged:

| Variable | Used by |
|---|---|
| `KYC_CONFORMANCE_V1_SECRET` | `send` (the v1 request), `receive` (the dual-emitted v1 callback signature) |
| `KYC_CONFORMANCE_INBOUND_SECRET`, `KYC_CONFORMANCE_INBOUND_KEY_ID` | `send` (v2) |
| `KYC_CONFORMANCE_OUTBOUND_SECRET`, `KYC_CONFORMANCE_OUTBOUND_KEY_ID` | `receive` (v2) |

# Part 3

# Deployment & Releases

For the platform team operating the KYC tool in IPv4.Global's AWS account.
Ownership: IPv4.Global maintains the code and cuts releases. You pull a
release and redeploy. No code is edited on the server. Anything that needs
changing changes in the repo and ships as the next release.

## 1. One image, four processes

The repo `Dockerfile` builds a single image. Each process is the same image
with a different command:

| Process | Command | HTTP |
|---|---|---|
| API | (image default) `uvicorn kyc_tool.api.app:create_app --factory --host 0.0.0.0 --port 8000` | 8000 |
| Migrations (one-shot) | `alembic upgrade head` | — |
| Pipeline worker | `python -m kyc_tool.workers.pipeline_worker` | — |
| Outbox publisher | `python -m kyc_tool.workers.outbox_worker` | — |
| Retention (daily cron) | `python -m kyc_tool.workers.retention` | — |

All are stateless, so scale the API and pipeline workers horizontally as
needed. The job queue keeps each case's jobs in order (oldest first, one at a
time), which is what makes extra workers safe. Callback delivery order is a
separate contract: `docs/PLATFORM_INTEGRATION.md` §4.
Disable the image's HTTP healthcheck on worker containers (they serve no HTTP).

## 2. Environments

| | Staging | Production |
|---|---|---|
| `KYC_ENVIRONMENT` | `development` (until real providers land) | `production` |
| `KYC_ENFORCE_POSITIVE_DECISIONS` | `true` — rehearse full automation | `false` at launch, flipped after staging proves out |
| Providers | live registry lookups (`CH_API_KEY` set), with stand-ins for the POC directory, document extraction and email (file sink) | real registry providers, required. Real OCR and email providers are needed only if the tool extracts documents or sends the POC email, which are the two open questions in `docs/PLATFORM_INTEGRATION.md` §5/§6. Either way `KYC_OCR_ENGINE` and `KYC_EMAIL_PROVIDER` must leave their dev stubs (`docs/RUNBOOK.md`). |
| Secret | staging secret | separate production secret |

Production mode validates config at boot and refuses to start on anything
unsafe (missing secret, stub providers, non-HTTPS callback URL), listing every
violation at once. A bad deploy fails loudly instead of running quietly broken.

Staging's automation-on is safe **only** while staging is closed to untrusted
callers. PR 5a adds path-bound HMAC v2, but during the dual-accept window a
**v1-only** request is still path-unbound — a captured signed event could be
replayed to another case within the skew window. The redirect closes for v2
traffic at deploy, but for everyone only once **inbound v1 is actually disabled**
(the zero-witness satisfied AND `hmac_v1_inbound_sunset_at` in effect). Keep
staging's perimeter closed until that day arrives. Deploying PR 5a is not the
moment it can open. Production automation stays off regardless until the M2
gate is met.

**PR 5a is a non-hot cutover.** Migration 010 drops the global unique that the
old image's ingest still uses, so an old replica serving after the migration
would fail event inserts. Deploy **stop → migrate → start** (not a rolling
upgrade): drain all old API replicas, run `alembic upgrade head`, start the new
replicas, readiness-verify them, then run the one-shot
`python -m kyc_tool.ops.activate_hmac_v1_observation` to start the v1
observation clock. Do NOT skip the activation step — until it runs, the sunset
zero-witness never turns green (by design), so v1 can never be sunset.

## 3. First-time setup (per environment)

1. Provision: RDS PostgreSQL 14+, an S3 bucket, an ECS/Fargate service (or
   EC2) for the processes above.
2. Generate the shared HMAC secret into AWS Secrets Manager. Set the same
   value in the platform's config for that environment.
3. Set env vars. All carry the `KYC_` prefix except `CH_API_KEY`, the
   Companies House key. `docs/RUNBOOK.md` holds the full table and
   `.env.example` a sample. **A rolling restart does not carry
   every setting.** Changing `KYC_OUTBOX_MAX_ATTEMPTS` in either direction is a
   DRAINED cutover (§8). Where a release note or a setting calls for one, run
   that procedure rather than the default rolling deploy.
   Minimum: `KYC_DATABASE_URL`, `KYC_PLATFORM_CALLBACK_URL`,
   `KYC_OBJECT_STORE=s3`, `KYC_S3_BUCKET`, `CH_API_KEY`, the two per-environment
   values from §2, and the full **HMAC credential set**. Production boot refuses without
   all of it (PR 5a):
   - v1 legacy secret: `KYC_PLATFORM_HMAC_SECRET`
   - v2 **inbound** (platform→tool): `KYC_HMAC_INBOUND_KEY_ID` +
     `KYC_HMAC_INBOUND_SECRET`
   - v2 **outbound** (tool→platform callbacks): `KYC_HMAC_OUTBOUND_KEY_ID` +
     `KYC_HMAC_OUTBOUND_SECRET`
   - both sunsets `KYC_HMAC_V1_INBOUND_SUNSET_AT` /
     `KYC_HMAC_V1_OUTBOUND_SUNSET_AT` (tz-aware ISO-8601 — a naive or malformed
     value is refused at boot, not at request time)
   - `KYC_HMAC_V1_OBSERVATION_WINDOW_DAYS` (≥ 1)

   Set both sunset dates in the future at launch (dual-accept). The inbound one
   only *takes effect* once the zero-witness is green (activate per §2), so a
   date alone never cuts off live v1.
4. Run the migration task: `alembic upgrade head`.
5. Start the processes. Wire `GET /readyz` to the load balancer — it checks
   DB connectivity, migration version, and storage access, and returns 503
   until all pass. Config safety is validated only in production mode. In
   staging's development mode `/readyz` does NOT vet the env vars, so verify
   the §3 values by hand.
6. Smoke test: send one signed `kyb.run_requested` (script in
   `docs/PLATFORM_BRIEFING.md` §7) and confirm the decision arrives at the
   callback URL.

## 4. Deploying an update

Each release from IPv4.Global is a tagged version with release notes stating
three things: does it include a **migration**, any **new env vars**, and any
**contract change** (almost always: none — the API contract is stable).

1. Pull the release tag and build the image.
2. If the notes list new env vars, set them first.
3. Run the migration task (`alembic upgrade head`). Safe to run when there is
   no migration — it does nothing. **Do not assume a migration is compatible
   with the still-running previous image**: the release notes state whether it
   is. When they don't say so (or say it isn't), use a brief cutover: stop
   the processes, migrate, start the new image. Not every migration is
   hot-compatible. 008 was not.
4. Rolling restart: API, then workers.
5. Verify (§5).

## 5. Post-deploy verification

- `GET /readyz` → 200 on every instance.
- `GET /healthz` → returns the policy bundle hash, which must match the
  release notes. A hash change **without** a deploy is an incident (policy
  files are immutable per release).
- Staging: run the smoke event end to end, then the conformance kit against it
  (`python -m kyc_tool.conformance send --tool <base-url> --case <throwaway>`,
  documented in `docs/PLATFORM_INTEGRATION.md` §11). The kit's one v1-signed
  request records a v1 acceptance in the durable witness, which restarts the
  zero-v1 observation window. Pass `--no-v1` (or skip the kit) while an
  inbound v1 sunset is being observed (`docs/PLATFORM_INTEGRATION.md` §2).
- Watch `GET /v1/metrics` for 15 minutes. `jobs_by_status.dead` and
  `outbox_by_status.dead` must stay 0. The budget for
  `event_to_decision_seconds.p95` is < 10 s for light runs, < 120 s for full
  runs.

## 6. Rollback

- Code: redeploy the previous image tag. That is the whole rollback when the
  release had no migration (most releases).
- With a migration: revisions downgrade cleanly
  (`alembic downgrade <previous revision>` — the release notes name it), but
  once real traffic has written data under the new schema, prefer rolling
  forward with a fix. Downgrade without hesitation in staging; in production,
  check with IPv4.Global first. **Exception — migration 010 (PR 5a) is
  forward-only after cross-case idempotency-key reuse:** its downgrade
  deliberately refuses (it will not delete immutable audit events to recreate
  the old global unique — see `docs/RUNBOOK.md` and ADR-003). If two cases have
  shared an idempotency key, roll forward with a fix; do not downgrade 010.
  **Exception — migrations 013-023 (PR 7b-core) are forward-only after any wire
  witness exists, positive OR negative** (an `attempt_v1` decision callback with no attempt
  is durable proof nothing was staged, and counts). Their downgrades refuse — with stable
  sentinels, in execution order — and `018` through `022` refuse UNCONDITIONALLY
  (`MIGRATION_018_DOWNGRADE_REFUSED_FORWARD_ONLY`,
  `MIGRATION_019_DOWNGRADE_REFUSED_FORWARD_ONLY`,
  `MIGRATION_020_DOWNGRADE_REFUSED_FORWARD_ONLY`,
  `MIGRATION_021_DOWNGRADE_REFUSED_FORWARD_ONLY`,
  `MIGRATION_022_DOWNGRADE_REFUSED_FORWARD_ONLY` — spelled out because a refused
  command is grepped, not read), because walking below them
  would restore search-path-vulnerable or under-validated authority functions
  (`MIGRATION_017_DOWNGRADE_REFUSED_WITNESS_IN_USE`,
  `MIGRATION_016_DOWNGRADE_REFUSED_WITNESS_IN_USE`,
  `MIGRATION_015_DOWNGRADE_REFUSED_WITNESS_IN_USE`,
  `MIGRATION_014_DOWNGRADE_REFUSED_WITNESS_IN_USE`,
  `MIGRATION_013_DOWNGRADE_REFUSED_WITNESS_IN_USE`,
  `MIGRATION_013_DOWNGRADE_REFUSED_AMENDED_HISTORY`) — when an
  `outbox_delivery_attempts` row, a terminal `callback_wire_sha256`, or a
  `superseded` outbox row exists: those are immutable delivery evidence (for a
  pending/dead callback, the attempt row is the ONLY record that bytes were
  staged), and a local terminal status is never a reason to destroy the record
  of what the platform accepted. On refusal, KEEP or redeploy the reviewed
  **024-compatible** image — an older publisher lacks the receipt/terminal
  contract and must not run against preserved evidence. Rollback after first
  witness use is a flag/image rollback on that compatible schema, never a
  schema downgrade; a pre-7b image is permitted only after the entire walk
  reaches 012 — which is only possible on a schema that never reached `018`. Once
  `018` through `022` ARE installed, the supported rollback is redeploying the prior
  reviewed `024`-compatible image against the schema it is already on; the schema
  does not move. Do not apply `018` or anything above it in production until that
  bridge image has been reviewed and
  staged; on this preproduction branch, the safe recovery path is roll-forward.
  The UPGRADE side is gated too: `017` and `018` both refuse with
  `MIGRATION_017_PREFLIGHT_LIVE_CLAIMS` / `MIGRATION_018_PREFLIGHT_LIVE_CLAIMS`
  while any live (unexpired) outbox claim
  exists — stop the publishers AND retention, attest zero old processes at the
  orchestrator, let leases expire or run `reset_interrupted_outbox_claims`, then retry
  — and `018`/`019` additionally refuse with
  `MIGRATION_018_AUTHORITY_MANIFEST_MISMATCH` / `MIGRATION_019_AUTHORITY_MANIFEST_MISMATCH`
  when the observable authority surface
  is not the one the prior revision installed. Every other deliberate refusal is
  indexed in `docs/RUNBOOK.md` § "Migration refusal sentinels". Witness writers (the publisher and
  retention) share one advisory fence with the migrations — writers shared, maintenance
  exclusive — so maintenance queues behind those writers instead of deadlocking with
  them. **The fence does not cover the pipeline's decide transaction**, which locks the
  case row before inserting the outbox row and takes no fence; a migration run against a
  live pipeline can still deadlock, and the live-claim preflight cannot see a run
  mid-decide because it holds no outbox claim. `022` and `023` sharpen that from "can" to
  "does": they are the first revisions to take `ACCESS EXCLUSIVE` on `decisions` and `cases`,
  in the opposite order from BOTH decision writers — the pipeline's decide transaction and
  the API's inline `reviewer.manual_approve` (jobless: no run, no claim, invisible to any
  job/run drain check) — so a concurrent decide OR inline approval deadlocks them (`40P01`)
  — see `docs/RUNBOOK.md`. That is what the DRAINED cutover is for —
  stop the pipeline workers too, not just the publishers.

## 7. Monitoring and incidents

Alert on, from `GET /v1/metrics`:

- `jobs_by_status.dead` > 0 — a run gave up after retries
- `outbox_by_status.dead` > 0 — a callback or email became undeliverable
- `runs_by_state.FAILED` growth
- `adapter_latency[].error_rate` per upstream registry
- `event_to_decision_seconds.p95` over budget

`docs/RUNBOOK.md` has the failure playbooks. Recovery runs through the
requeue endpoints, which reset both the job and its failed run. The ops
console offers them as buttons (`/ui/api/requeue/...`) wherever
`KYC_UI_ENABLED` is on, and `POST /v1/ops/requeue/job/{job_id}` and
`/v1/ops/requeue/outbox/{outbox_id}` are always mounted behind the operator
token. Three limits to know:

- A dead `poc_email` row cannot be requeued: its token was scrubbed when it
  died (the endpoint refuses it). Recovery is a fresh `poc.submitted`.
- `recalculate.requested` re-decides from existing evidence but does **not**
  re-run the broker screen — after a blocklist update, re-send the original
  evidence event (or `kyb.run_requested`) instead.
- Registry-outage behavior needs no action: runs complete as partial and
  nothing wrong is ever emitted.

## 8. Rules

- No code edits on the server; no schema or data edits outside the runbook
  playbooks. The audit trail assumes the repo is the truth.
- Packaged policy changes require a release and version-bump guard. After the
  explicit configuration cutover below, scoring points, broker snapshots, and
  Salesforce destination names instead use audited, server-saved revisions.
  Threshold, hard gates, evidence rules, and M2 are not console-editable.
- Secrets only via environment / Secrets Manager; nothing secret is logged.
- Never set `KYC_AUTH_DISABLED` outside local dev. Production boot refuses it.
- **ANY change to `KYC_OUTBOX_MAX_ATTEMPTS` — raising OR lowering — is a DRAINED
  publisher cutover, not a rolling restart.** Each publisher enforces the ceiling
  it was started with, so during a rolling restart an OLD and a NEW publisher run
  different ceilings against the same rows:
  - Lowering: the OLD (higher) publisher can make one more send past the new value.
  - Raising: the OLD (lower) publisher can dead-letter a row at its lower ceiling
    before the NEW (higher) publisher ever supplies the extra attempts — and for a
    POC email the terminal transition redacts the token, so those lost retries are
    irreversible.

  So for EITHER direction, in this order: (1) disable autoscaling and rolling
  restart; (2) stop ALL outbox publishers of EVERY role — both the standalone
  `outbox_worker` and the embedded `dev_worker`; (3) attest zero publishers are
  running (the same attested-stop the reset CLI requires); (4) attest every new
  task definition carries the exact new value; (5) start. This procedure is the
  canonical record `kyc_tool.ops.cutover.OUTBOX_MAX_ATTEMPTS_CUTOVER`, rendered
  below and validated by `tests/unit/test_outbox_ceiling_contract.py`; RUNBOOK and
  `.env.example` embed the SAME rendered block. (A fleet-wide DB-persisted ceiling
  epoch enforced before claim is the fail-closed alternative if runtime config
  drift must be impossible — deferred; the drained cutover is the contract today.)

<!-- cutover:KYC_OUTBOX_MAX_ATTEMPTS:start -->
KYC_OUTBOX_MAX_ATTEMPTS: both-direction DRAINED publisher cutover (NOT a rolling restart)
1. disable autoscaling and rolling restart
2. stop ALL publishers of roles: outbox_worker, dev_worker
3. attest zero publishers running of roles: outbox_worker, dev_worker
4. attest every new task definition carries KYC_OUTBOX_MAX_ATTEMPTS
5. start publishers of roles: outbox_worker, dev_worker
<!-- cutover:KYC_OUTBOX_MAX_ATTEMPTS:end -->

## 9. PR 5b cutover — brief full maintenance window

PR 5b adds the reviewer-actor trust floor that closes the "review completed /
approved by anyone holding the shared secret" forgery: it requires the signed
envelope's `actor` to identify the reviewer (`docs/PLATFORM_INTEGRATION.md`
§3), not just the payload. **This is not a rolling deploy.** During any
old/new overlap, an old replica still honors the exact forgery this release
closes — an old API applies `reviewer.manual_approve` inline with no actor
floor, and an old pipeline worker (which claims a job purely by kind, with no
event-type filter) can still close a queued `system`-actor website completion
under the old actorless semantics. There is no way to keep an old replica
serving *any* traffic while guaranteeing it never touches a sensitive event,
so this release ships as a **brief full maintenance window** — the same
non-hot **stop → deploy → start** pattern PR 5a used (§2), extended to workers
as well as the API, which removes old/new overlap entirely. No migration
ships with this change.

The window is a real interruption, not a seamless roll: `POST
/v1/cases/{case_id}/events` is unavailable for its duration, for every event
type, and the pipeline is stopped. The "no loss" guarantee for that
interruption is a **platform prerequisite**, not something the current API
contract provides on its own — today the contract only directs retry on a
*network failure* (`docs/PLATFORM_INTEGRATION.md` §3), but a load balancer
with every API target down instead returns 502/503/504, and a delayed retry
that reuses the original signature can blow the 300-second HMAC skew. Before
scheduling the window, confirm with the platform team that they will
pause/buffer **all** event submission for its duration and drain it
afterward — re-signing each retried body with a **fresh timestamp/signature**
against the **same idempotency key** — or, if they prefer to keep sending,
that they treat 502/503/504 the same as a transport failure: retryable with
the same body/key and a fresh signature.

Steps:

0. **Before the window:** build and publish the reviewed image; record its
   **digest**. Every step below that runs code — the recovery one-shot, the
   new API, the new workers — is pinned to that one digest. The recovery
   module (`kyc_tool.ops.requeue_interrupted_jobs`) exists only in the new
   image, so running it on the still-current old task definition fails with
   `No module named …`.
1. **Pause all platform event submission** (every event type, not only the
   two sensitive ones) and block the ops composer: the platform buffers
   outbound events, and the composer route (a `POST` to `/ui/api/send-event`) is
   edge-blocked — or old replicas are flipped to `KYC_UI_ENABLED=false` — so
   an operator on a still-live old replica can't post an inline forged
   approval. The whole window is a maintenance pause; a partial pause cannot
   guarantee no-loss.
2. **Stop all old processes together** — the API pool and the pipeline-worker
   pool, as one coordinated action, **no graceful drain**, without awaiting
   either pool before signaling the other. Confirm both pools are at **zero**
   before continuing. A sequenced stop leaves the not-yet-stopped pool live
   and able to commit a forgery in the gap; the edge block cannot revoke a
   request already inside an old API's threadpool, so old APIs must be
   *stopped*, not drained. Hard termination is safe here: every transition
   (and every ingest) commits in one transaction, so interrupted work simply
   rolls back.
3. **Recover interrupted jobs.** With both pools confirmed at zero, run
   ``python -m kyc_tool.ops.requeue_interrupted_jobs`` as a one-shot task
   **pinned to the §0 digest**, before any new worker starts. With every
   worker stopped and none yet restarted, every `status='running'` job row is
   by definition interrupted — regardless of lease expiry, and with no need
   for a worker-liveness registry. The command requeues that whole set
   transactionally **without** consuming the forced-stop attempt (this is an
   operator-initiated interruption, not a handler failure — consuming the
   attempt could let the passive reaper dead-letter an expired final-attempt
   job before it ever reaches the new actor guard), and it asserts zero
   `running` rows remain on completion. It must not run while any worker is
   live — confirming both pools at zero (step 2) is its precondition.
4. **Deploy the new API and worker services, both pinned to the §0 digest**
   (attest it). Bring the **API** up first and hold **workers at zero** until
   step 5 passes — workers serve no HTTP, so the API probes below cannot
   vouch for a stale or mismatched worker image, and starting workers early
   would let one honor a recovered pre-upgrade completion.
5. **Direct-probe each new API replica** on a trusted path that bypasses the
   edge rule (internal target-group address or port-forward) — probing
   through the edge would let the load balancer's own 403 falsely certify a
   broken app. Use only side-effect-free probes (each is rejected at the
   ingest floor and rolls back, writing no rows) and assert the response
   **body**, not just the status code:
   - a signed `system`-actor `website.review_completed` against a valid open
     task → the app's **422** (a 404/409 would mask a broken actor floor);
   - a mismatched-actor `reviewer.manual_approve` → the app's **422**;
   - the composer → the app's **403** for both sensitive event types.

   Do not probe the decide-txn guard's live behavior in production this way —
   a real pipeline run there writes a decision and enqueues a callback
   unconditionally, and the outbox publisher (a separate process, not stopped
   in step 2) would deliver it to the platform. That behavior is proven
   **before the window, in staging**, against the exact §0 digest; attest the
   same digest here.
6. **Start the new workers** (they now claim the recovered queue under the
   new decide-txn actor guard), then **resume** — unpause platform event
   submission and unblock the composer route.

### Rollback

Rollback mirrors the same window and pins the recovery one-shot to the **last
image that still contains it**: pause all submission, stop all new processes
together (same coordinated hard stop; same recovery command, run against a
digest that has the module), redeploy the prior image for API **and**
workers, verify, start workers, resume — accepting that the prior image
restores pre-PR-5b behavior.

**Rollback verification is non-mutating only:** `GET` probes of `/readyz`
and `/healthz`, and prior-image digest attestation. Do **not** run the step-5
sensitive-mutation probes against the prior image — that image is the current
vulnerable code with no actor floor, so a mismatched-actor `manual_approve`
probe would actually `approve` the case inline, and a `system`-actor
completion probe would queue a run the restored old worker can honor: the
probe would *perform* the forgery it is meant to detect, not find it.
Exercise that behavior only in staging or an isolated DB. Keep all submission
and the composer blocked until the safe, non-mutating checks pass.

## 10. PR 6 cutover — bundle-pinning activation

PR 6 pins the policy bundle (and records the engine build) a run is actually
scored and decided under, instead of trusting whatever the worker process
happened to have loaded. It ships in two parts: a **rolling** part (safe to
deploy like any other release) and a **drained** part (the flag flip, not
safe to roll).

**Rolling — migration + provenance, flag stays off.** Migration 011
(`policy_bundles`, `bundle_pinning_epoch`, and the new nullable provenance
columns) is additive and hot-compatible — deploy it through the normal §4
flow. It is hot-compatible precisely because its three provenance-column
CHECKs land `NOT VALID` (a brief, metadata-only lock, no table scan) and are
validated by the follow-on migration 012 via `VALIDATE CONSTRAINT` under a
non-blocking lock, so the rolling `alembic` upgrade-to-head (the §4 one-shot) does not stall
ingest/decide writers on large `checks`/`decisions` audit tables. On the PR6
image, the API and pipeline worker seed and read back the
on-disk policy bundle at startup (failing closed on a corrupt persisted row)
and every automatic/manual decision starts recording bundle **and** engine
provenance immediately — `Settings.enforce_bundle_pinning`
(`KYC_ENFORCE_BUNDLE_PINNING`) stays `false`, so scoring itself is
byte-identical to pre-PR6 (a strict no-op; see ADR-005). A `GET` on `/readyz`
unconditionally confirms the process's loaded bundle is durably resolvable
from the store — watch it like any other readiness check during this
rollout.

**Seed the bundle.** Before relying on the store for anything beyond a
process's own startup seeding (in particular, before the preflight below),
confirm the release's bundle is in `policy_bundles`:

```operator
python -m kyc_tool.ops.seed_policy_bundle --expect-hash <sha256>
```

`--expect-hash` is the hash an operator names from a reviewed source (the
release notes / deploy manifest) — the command computes the hash of the
bundle at `KYC_POLICY_DIR`, compares it to `--expect-hash`, and **only on a
match** stores it; a mismatch raises and writes nothing, so a wrong policy
directory can never land a row silently.

**Drained cutover — flip the flag.** This is not a rolling deploy: every
worker that claims a `run_transition` job while `enforce_bundle_pinning` is
inconsistent across the pool risks resolving a bundle differently from its
peers. Flip it with the pool fully drained, the same shape PR 5a/5b used:

1. **Preflight.** ``python -m kyc_tool.ops.verify_pinnable_backlog`` — checks
   that every queued/running/dead `run_transition` job's run has a
   creation-pin bundle that actually loads from the store. **A nonzero exit
   blocks the cutover** — seed the missing bundle(s) (§ above, or historical
   recovery via the same command with the older policy directory) and re-run
   until it exits 0.
2. **Disable autoscaling/restarts; confirm zero old workers.** At the
   orchestrator (not by row count — a job-table count doesn't prove process
   quiescence), confirm every pipeline-worker replica currently running
   predates this cutover is gone.
3. **Recover interrupted jobs.** With workers confirmed at zero, run
   ``python -m kyc_tool.ops.requeue_interrupted_jobs`` once — every
   `status='running'` job at this point is by definition interrupted; it
   requeues the whole set without consuming the forced-stop attempt and
   asserts zero `running` rows remain. Its precondition is "all workers
   confirmed stopped" (step 2) — do not run it while any worker is live.
4. **Start flag-on workers.** Set `KYC_ENFORCE_BUNDLE_PINNING=true` and start
   the pipeline-worker pool. Confirm the startup **attestation** log line on
   every replica — a structured `bundle_pinning_ready` event carrying
   `flag=true`, `bundle_hash`, and `engine_build_id` — emitted only after the
   worker's own seed-and-verify passes, so a replica that never logs it never
   started claiming jobs.
5. **Resume.** Unpause whatever was paused for the drain (the API itself
   never stopped — only the worker pool is drained here).

**Activate the epoch.** Once the cutover is verified stable, write the
durable activation record:

```operator
python -m kyc_tool.ops.activate_bundle_pinning_epoch \
    --expect-bundle-hash <sha256> --expect-engine eng-1
```

This compares the **locally loaded** policy bundle and this process's
`ENGINE_BUILD_ID` against the `--expect-*` arguments before touching the
database at all (a valid-but-wrong bundle is refused here, not merely by
store-absence later), writes the singleton `bundle_pinning_epoch` row with
database time, and read-back-fails if a concurrent activation already wrote
different values — so a skewed operator clock or a mismatched second
activation can never silently move the boundary. From `activated_at`
onward, `docs/RUNBOOK.md`'s post-epoch alert treats any check/decision
missing its provenance stamp as an anomaly, not an expected state.

**Rollback — flag-only, no data migration.** Normal rollback for PR 6 never
means resuming on a pre-PR6 image (that would silently stop writing
provenance and, after the epoch, mint permanent NULLs — a defect, not a
safe fallback). It means disabling the flag **on the PR6 image**, via the
same drained shape: stop the worker pool → confirm zero running →
`ops.requeue_interrupted_jobs` → start workers with
`KYC_ENFORCE_BUNDLE_PINNING=false` → resume. The creation pin and
provenance writing are preserved throughout; scoring simply returns to the
process-loaded bundle. Migration 011 is **retained** — never downgrade it
once any bundle row, provenance column, or the epoch row is populated (its
downgrade deliberately refuses, the same forward-only-after-use contract
migration 010 established in ADR-003).

## 11. PR 7b-core cutover — drained maintenance window (migration 013)

**Step 0 — pre-window diagnostic (BEFORE any outage):**
0.1 Suspend the retention schedule.
0.2 Terminate and wait for every active retention task.
0.3 Capture target-orchestrator zero-running evidence. `TODO(integration)`: the exact
    zero-running listing — the `aws` CLI's `ecs list-tasks` scoped to the cluster and the
    retention family (or the EC2 equivalent) — and its expected zero-task output MUST be
    recorded here as a typed operator command once the production substrate is chosen. A
    pytest does NOT prove this — it is a deployment acceptance. Do not invent a substrate.
0.4 With the schedule still suspended, run the digest-pinned
    ``python -m kyc_tool.ops.verify_pr7b_core_backfill``. The result is valid ONLY while retention stays
    suspended AND the 0.3 attestation holds.
0.5 On failure, ABORT here — before stopping service (no outage begun). Recovery is restore-or-block:
    restore from authoritative backup the EXACT callback row, OR remain on 012 in
    `BLOCKED_NO_AUTHORITATIVE_MAPPING`. Backup availability is an operator prerequisite. Activation (`025`) is
    downstream and cannot repair this. Never fabricate a callback, delete a decision, or fall back to
    `decided_at`. On EVERY abort path, explicitly re-enable OR deliberately keep-frozen retention.
    THE RESTORE PATH IS A SHIPPED CLI, reachable from HERE — a pre-window maintenance stop, not the
    cutover (which 0.4 still gates): FIRST run the prerequisites check (read-only, takes NO
    lock) and confirm it is GREEN — exact schema phase, correct role, `outbox_id_seq`
    ownership, and timeout budgets:

```operator
python -m kyc_tool.ops.verify_pr7b_ops_prerequisites --expect-revision 012
```

A wrong maintenance credential OR wrong phase is caught HERE, not at `ALTER SEQUENCE`
inside the stop; then pause submissions,
hard-stop and attest EVERY writer (API,
pipeline, outbox, `dev_worker`, retention), then run
the restore CLI (dry-run first; add `--apply` to perform):

```operator
python -m kyc_tool.ops.restore_pr7b_core_callback --evidence <file.json> \
    --expect-original-id <id> --expect-manifest-digest <sha256>
```

`--expect-manifest-digest` is MANDATORY and is an INTEGRITY check: the tool recomputes
the sha256 of the evidence file (``sha256sum <file.json>``) and refuses unless it matches, so a
tampered or wrong file is rejected before any DB work — the file cannot self-certify by carrying
its own digest. The tool does NOT verify a cryptographic signature; the digest's authenticity is
yours to establish out of band, from a trusted/signed backup manifest (machine-verified signing
is a future option).
It validates the whole
contract below, inserts the exact original row, floors the sequence past the restored id
(`GREATEST(max(id), original_id) + 1`) in the SAME transaction, and fail-closed read-backs both
the acceptance predicate and the sequence before committing — any mismatch rolls back row and
sequence together. Then rerun 0.4 (the gate that reopens cutover) and either RESUME service or
proceed to the window. Pasting the SQL below by hand is NOT a sanctioned path — the earlier
revision of this section prescribed exactly that and was circular: the diagnostic stayed red
until the restore, while the sequence repair was documented as reachable only after cutover
step 2 and knew nothing of the id being restored (re-audit `f495de8` F1).
0.6 RESTORE ACCEPTANCE CONTRACT (the restore in 0.5 is an executable identity requirement, not
    advice — the backfill ranks by `outbox.id`, so a wrong id silently reverses the legacy order):
    (a) BEFORE restoring, record from the backup the authoritative evidence tuple per missing
        callback: `decision_id` plus **every schema-012 `outbox` column** —
        `(id, kind, case_id, run_id, payload_json, status, attempts, next_attempt_at,
        delivered_at, last_error, created_at)` — with `body_digest` computed ON THE BACKUP ROW as
        `encode(sha256(convert_to(payload_json::text,'UTF8')),'hex')`. `md5(...)` is prohibited.
        This procedure runs BEFORE 013, so it must name NO 013-only column: `resolved_at`,
        `ordering_stream`, `decision_sequence` and the claim tuple do not exist yet. Omitting the
        retry/audit columns is what makes "exact" false — a previously retried callback restored
        with a reset `attempts`/`next_attempt_at`/`last_error` is NOT the row that was pruned.
    (b) The restore MUST re-insert the ORIGINAL primary key AND every other recorded column:
        `INSERT INTO outbox (id, kind, case_id, run_id, payload_json, status, attempts,
         next_attempt_at, delivered_at, last_error, created_at) VALUES (<original_outbox_id>, ...)`
         — every value from the evidence tuple, none defaulted. A
        default-id INSERT is prohibited (it allocates a fresh id and re-ranks the restored older
        callback as newer), and substituting `now()` for `delivered_at` is prohibited (it falsifies
        the audit record). If the original id is unavailable, do NOT restore: remain
        `BLOCKED_NO_AUTHORITATIVE_MAPPING` on 012. The evidence tuple is captured into the JSON
        file the restore CLI's `--evidence` input consumes; `--expect-original-id` must repeat
        the id (double entry). The id, body digest and decision linkage are machine-refused on
        mismatch; the lifecycle fields are ATTESTED inputs from the backup — but the MANDATORY
        `--expect-manifest-digest` (sha256 of the whole evidence file, from the signed manifest)
        binds every one of them, so a falsified backup value cannot pass without also breaking the
        signed digest. The signed manifest and the documented capture query are the sanctioned source.
    (c) ACCEPTANCE PREDICATE — POSITIVE and fail-closed. Run per restored callback; it MUST return
        EXACTLY ONE row before proceeding. ZERO rows = still blocked. Do NOT invert it into a
        "select the mismatches, expect zero rows" form: an absent row (or one restored under the
        wrong `run_id`) matches nothing and would read as accepted.
        `SELECT 1 AS accepted FROM outbox o JOIN decisions d ON d.id = :decision_id
         WHERE o.id = :original_outbox_id AND o.kind = :original_kind
           AND o.case_id = :case_id AND o.run_id = :run_id
           AND d.case_id = o.case_id AND d.run_id = o.run_id
           AND encode(sha256(convert_to(o.payload_json::text,'UTF8')),'hex') = :body_digest
           AND o.status = :original_status
           AND o.delivered_at IS NOT DISTINCT FROM :original_delivered_at
           AND o.attempts = :original_attempts
           AND o.next_attempt_at IS NOT DISTINCT FROM :original_next_attempt_at
           AND o.last_error IS NOT DISTINCT FROM :original_last_error
           AND o.created_at IS NOT DISTINCT FROM :original_created_at;`
        Every schema-012 column is compared, so dropping any one of them from the restore fails
        the predicate. `IS NOT DISTINCT FROM` is used for nullables so NULL matches NULL.
    (d) THE SEQUENCE IS THE RESTORE CLI'S JOB — there is NO separate precondition to satisfy
        first (re-audit `8377440` F3: the old text made the restore reachable only after a
        `next_id > original_outbox_id` check that the documented `max=5`/`missing-id=100` case fails,
        which is exactly the case the restore exists for). `restore_pr7b_core_callback` floors the
        sequence to `GREATEST(max(id), original_id) + 1` in the SAME transaction as the row
        insert, under `ACCESS EXCLUSIVE`, with a fail-closed read-back — whether the missing id is
        below OR above the current high-water. It never `setval`s (a read-modify-write on a
        non-transactional object that can rewind under concurrent `nextval`); `ALTER SEQUENCE …
        RESTART WITH` takes a literal and excludes `nextval` for the transaction. Run it (dry-run,
        then `--apply`) as step 0.5 above — the restore and the sequence floor are ONE action, not
        a check-then-repair sequence.
    (e) ``python -m kyc_tool.ops.repair_outbox_sequence`` is the SEPARATE DRAINED action for the
        ONLY case the restore does not cover: a divergent sequence high-water with NO row to
        restore (nothing missing, the counter itself is wrong). Same maintenance-stop
        preconditions and owner privilege; pass `--floor` with the id when an id above max must stay cleared.
        It is never a prerequisite the restore waits on.
    (f) Only then rerun 0.4 (it must be clean — it also proves existence/1:1 of every mapping).

**Cutover (only after 0.4 is green):**
1. Pause submission, edge-block the composer, disable autoscaling/restarts.
2. Hard-stop API, pipeline, outbox, `dev_worker` (queue AND outbox), retention, and every writer;
   attest zero at the orchestrator.
3. Run the shipped ``python -m kyc_tool.ops.requeue_interrupted_jobs``. NO outbox reset here — the
   pre-013 schema has no claim columns; an interrupted old claim simply waits until its already-
   recorded `next_attempt_at`. Preserve every pending row's `next_attempt_at`.
4. Run ``python -m alembic -c alembic.ini upgrade head`` (the chain `013`→`014`→…→`022`→`023`) — the deployment image runs its exact
   equivalent. This repeats the §0 parity preflights under the zero-writer boundary and is the
   authoritative fail-closed check (the pre-window diagnostic is an early detector, not a substitute).
   `017` additionally machine-checks the drain: it refuses with `MIGRATION_017_PREFLIGHT_LIVE_CLAIMS`
   while any live (unexpired) outbox claim exists — leases must expire or be reset first.
5. Start API only, probe `/readyz`, then start + attest the fenced workers. No mutating prod smoke.
6. RESUME (forward completion): re-enable retention, autoscaling/restarts, and submissions, and
   remove the composer edge block. The window is NOT closed until all five paused controls
   (retention, autoscaling, restarts, submissions, composer edge block) are restored or removed.

**Rollback — a two-branch maintenance state machine (as drained as the forward cutover). BOTH branches
end in a full resume — never leave the system stopped or retention frozen:**
R1. Pause submissions, edge-block the composer, disable autoscaling/restarts.
R2. Hard-stop and orchestrator-attest zero API, pipeline, outbox, `dev_worker`, retention, every writer.
R3. While 013 still exists, run ``python -m kyc_tool.ops.reset_interrupted_outbox_claims`` (post-013-only;
    clears complete claim tuples, preserves `next_attempt_at`, atomically read-back-asserts zero) and
    verify zero claim tuples.
R4. **With `018` or anything above it installed there is no schema-downgrade path**: `018` through
    `022` refuse unconditionally — a walk from the head prints
    `MIGRATION_022_DOWNGRADE_REFUSED_FORWARD_ONLY` (`023`'s downgrade is a validation-only
    no-op the walk passes through first; the whole command is ONE transaction, so on refusal
    even that step rolls back and the schema does not move) — because walking below them would restore
    search-path-vulnerable authority functions, so rollback goes straight to R5 (image-only on
    the schema already installed). The walk below is the HISTORICAL path, reachable only on a
    schema that never reached `018`: run ``python -m alembic -c alembic.ini downgrade 012`` (the revision is a
    REQUIRED positional argument — a bare `alembic` downgrade invocation without it exits with a usage error
    mid-outage). That walk is `017 → 016 → 015 → 014 → 013 → 012`, and EACH revision preflights
    under
    `LOCK TABLE ... ACCESS EXCLUSIVE` (child-first from `015` on; `017` first takes the shared
    maintenance/writer advisory fence EXCLUSIVE, so it queues behind live witness writers instead
    of reasoning about their lock order). Sentinels in execution order:
    - `017` refuses — `MIGRATION_017_DOWNGRADE_REFUSED_WITNESS_IN_USE` — when ANY attempt row,
      terminal wire digest, or `attempt_v1` decision callback exists. NEGATIVE evidence counts:
      an attempt-regime row with no attempt is the durable proof nothing was staged.
    - `016` refuses — `MIGRATION_016_DOWNGRADE_REFUSED_WITNESS_IN_USE` — same rule one revision
      down (defense in depth below `017`), including the `attempt_v1` negative-evidence case.
    - `015` refuses — `MIGRATION_015_DOWNGRADE_REFUSED_WITNESS_IN_USE` — on any attempt row or
      terminal digest (child-first lock order; cannot deadlock a live writer).
    - `014` refuses — `MIGRATION_014_DOWNGRADE_REFUSED_WITNESS_IN_USE` — same witness rule.
    - `013` refuses on a `superseded` row, a surviving terminal digest
      (`MIGRATION_013_DOWNGRADE_REFUSED_WITNESS_IN_USE`), or the attempt table under a bare `013`
      stamp (`MIGRATION_013_DOWNGRADE_REFUSED_AMENDED_HISTORY`).
R5. ROLLBACK OUTCOME A — downgrade REFUSED (any sentinel above): the DB stays on the
    witness-authority schema, so KEEP or redeploy the reviewed **`024`-COMPATIBLE image** digest —
    an older publisher lacks the receipt/terminal contract and MUST NOT run against preserved
    evidence; PROHIBIT the pre-7b image outright. Rollback after first witness use is a
    FLAG/IMAGE rollback on the compatible schema, never a schema downgrade. A pre-7b image is
    permitted ONLY after the entire walk reaches `012` (outcome B). Verify `/readyz`, start + attest its fenced workers, then
    re-enable retention, autoscaling/restarts, and submissions and remove the composer edge block —
    OR remain in a DELIBERATELY DECLARED maintenance incident while the forward fix is applied. Do
    not end stopped.
R6. ROLLBACK OUTCOME B — downgrade SUCCEEDED: deploy the recorded prior-image digest; start API, probe
    `/readyz`, then start + attest its workers; attest image digest + running processes; then re-enable
    retention, autoscaling/restarts, and submissions and remove the composer edge block. Redeploying
    the pre-7b image BEFORE 013 is applied is also safe.

## 12. Live configuration cutover (migration 024)

Installing schema `024` does **not** activate configuration. Core migrations
`013`–`023` and configuration migration `024` are frozen independently; do not
repair either owner's revisions in place. Platform activation `025` remains
unbuilt/fail-closed; this procedure does not enable M2 or alter callbacks.

1. Record a database backup and the exact digest of the configuration-capable
   release image being deployed. Record that same tested image as the recovery
   image; a pre-configuration image is not a supported rollback after activation.
   Install schema `024` using the normal migration process. The following CLI
   requires at least `024`; it is not a schema-`023` preflight.
2. Configure `KYC_ENFORCE_BUNDLE_PINNING=true` and a nonempty
   `KYC_UI_ADMIN_TOKEN` in the CLI and every compatible API/pipeline/dev worker
   environment. Supply credentials through the secret store/environment, never
   command arguments, source, screenshots, or logs. Keep UI access restricted.
3. Run the read-only preflight before the maintenance window where schema `024`
   is already installed:

   ```bash
   python -m kyc_tool.ops.activate_live_configuration
   ```

   Require exit 0 and `ready: true`. Inspect `blocking_jobs`, `blocking_runs`,
   and `problems`. Any job not `done` (including dead/retryable work) and any
   unfinished legacy run block activation. Drain them under the old behavior;
   do not relabel them complete, delete them, or invent historical snapshots.
   Review the entire legacy broker baseline: exact stable IDs, names, policies,
   all identifier classes, and notes. Invalid/oversized baselines refuse rather
   than truncate. The CLI also verifies the packaged/stored policy baseline.
4. Block new event admissions and stop all relevant writers: API/composer,
   pipeline and dev workers, retry/recovery tools, publishers, retention, and
   deployment auto-restarts. Attest their stopped state at the orchestrator.
   Rerun the read-only preflight, then apply:

   ```bash
   python -m kyc_tool.ops.activate_live_configuration \
     --apply --attest-writers-stopped --operator-label "maintenance operator"
   ```

   The label is attribution, not verified individual identity. The attestation
   is operator-supplied, not fleet discovery. Apply rechecks under database
   writer fences, stores/verifies the policy, snapshots the full broker list
   including notes, and creates default mappings and the active revision in
   one transaction. Lock/statement budgets are 5/30 seconds; refusal writes
   no baseline. An already-active invocation verifies it and never resets it.
5. Start only the recorded compatible processes with pinning enabled. Require
   `/readyz` and `GET /ui/api/configuration` to verify active authority. Enter
   the admin credential in console Options for this session; read access alone
   is not save authority. Confirm authenticated Save and reload on disposable
   test state before claiming the console is editable. A single process's
   readiness does not attest the whole fleet. Then resume admissions.

Configuration requests are bounded at 32 MiB before decoding; configure the
ingress limit consistently. The local `scripts/devproxy.py` preserves the
browser-facing Host for same-origin checks and rejects oversized/malformed
configuration framing before reading the body. It is a loopback development
proxy, not a production forwarded-header trust policy. Do not restart an existing
demo via `scripts/dev.sh`: its cleanup deletes its temporary database. Preserve
the database and replace only compatible processes in a controlled maintenance
window; never resume a stale destructive watcher.

After activation, recover a prior desired configuration by saving its reviewed
sections as **new revisions**, retaining history. There is no pointer-reset or
rollback CLI. A missing/corrupt active revision is a maintenance incident:
restore verified authority from backup or forward-fix with the recorded compatible
image; do not substitute current packaged values. Downgrade `024` refuses any
recorded configuration history. Completed legacy runs stay explicitly unversioned;
ordinary requeue must not feed unfinished unversioned work to snapshot-only workers.
