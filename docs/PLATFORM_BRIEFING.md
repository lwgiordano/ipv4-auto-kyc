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
