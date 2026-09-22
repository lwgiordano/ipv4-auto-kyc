# KYC Tool — Platform Team Briefing

This is the handoff for TechCraft. The tool can be tested in a
closed staging environment, but it is not ready for production. Start with §4
for the platform work, §5 for the two provider decisions, §6 for staging, and
§8 for the remaining delivery gaps and numbered asks. Engineers should build
against `PLATFORM_INTEGRATION.md`, which owns the signatures and payloads,
including status codes.

## 1. What it is

The tool evaluates evidence about one registrant and the company they claim to
represent. It applies the published scoring and gate policy and returns a
decision for the platform to handle. It does not certify unrestricted legal
authority, drive platform screens or change platform state.

The work happens in the background. Your platform POSTs an event (a
registration, a verified email, an uploaded document, an RIR org handle) and
gets an acknowledgment straight away. The tool then looks the claimed company
up in Companies House, GLEIF and the five regional internet registries (ARIN,
RIPE, APNIC, LACNIC, AFRINIC). It screens the broker blocklist. It scores what
it found and POSTs the verdict to a webhook your team hosts.

The design has one message that may go directly to a person: the POC
verification email, sent only to the address the registry lists for that
contact. Nothing sends that message in the production path yet. Whether the
tool sends it or your platform does is one of the two open questions (§5 and
§8 item 10).

## 2. What it does

Each piece of evidence becomes a **check** worth points. Points add up toward
a **100-point threshold**. Five **gates** must also pass for an automatic
approval. A reviewer can approve manually through the separate review action.

| Gate | Meaning |
|---|---|
| `score_met` | total ≥ 100 |
| `legal_proof` | at least one legal-identity check passed (registry or document) |
| `control_proof` | a live `poc_verified` check passed |
| `broker_ok` | not on the blocked-broker list |
| `no_hard_conflict` | no live check carries `hard_conflict`, `document_registry_conflict` or `registry_exact_company_inactive` |

The default checks and weights come from
`KYC_Tool_Build_Package/machine_readable/scoring_rubric.json`, the policy file
that the tool and its tests both read. An approved console configuration can
override those defaults; each run pins the configuration revision it used, so
the run's recorded score is the authority for that decision.

| Check | Points | Category |
|---|---|---|
| `official_registry_match` | 25 | legal proof |
| `business_document_verified` | 25 | legal proof |
| `verified_company_email` | 25 | supporting |
| `org_id_match` | 25 | supporting |
| `poc_verified` | 25 | control proof |
| `linkedin_company_match` | 20 | supporting |
| `verified_email` | 10 | account access |
| `website_verified` | 10 | supporting |

Five of those eight can run without a human in closed staging when their
inputs and live credentials are present: the registry match, the ORG-ID match,
the company email, any verified email and the LinkedIn match. Passing those
five scores 105, but it does not pass the control gate. Automated control
requires a verified `poc_verified` check. Email and ORG-ID checks remain
supporting evidence. Website review is a human task by design. The POC and
document paths still need the decisions and provider work in §5.

Four verdicts come back: `approve`, `approve_buy_locked` (the account is fine,
buying stays locked until an ORG-ID verifies), `manual_review_insufficient`
and `reject`. That second one is how "ORG-ID optional at registration" works
in practice. Every check that does not pass carries a stable reason code
naming what is missing or wrong, and that code is what your review team acts
on.

Only a broker-blocklist match rejects a case on its own. An inactive official
registry record creates a hard conflict only when its legal name, address and
registration number exactly match the submitted company. That check carries
`registry_exact_company_inactive` and routes to manual review, never automatic
rejection. A generic historical `registry_company_inactive` reason alone does
not create the conflict because it may describe an unrelated search result.
Everything else that falls short also goes to manual review. The tool does not
implement sanctions screening. That is a platform responsibility and must be
completed before the platform calls the tool.

Historical category labels and decisions are not rewritten. A historical
category labelled control proof is not current authorization: the current gate
requires a live passed POC check. Governed revalidation of historical evidence
is required before production relies on it, including old POC and registry
results. A recorded manual approval remains authoritative.

**Evidence coverage and freshness:** the run is *partial* when a source invoked
for that run fails. Evidence already gathered stands and nothing is guessed,
but older live checks remain in the score. `partial: false` means the invoked
sources did not fail; it does not prove that every live check was fetched in
that run. The callback carries neither the partial flag nor evidence ages.

Use `GET /v1/runs/{run_id}` for `partial` and the invoked adapters'
`adapters[].fetched_at` values. Use `GET /v1/cases/{case_id}/checks` for the
current live checks and their `checks[].created_at` values. These reads are
review aids. A check's `created_at` is check-record time, not proof of
provider-evidence freshness. The checks endpoint shows current live-check
state, not a callback-time snapshot. Combining the reads cannot prove source
age or bind evidence to the callback decision. A full source-age and coverage
contract bound to the decision must still be specified, built and accepted
before automatic production decisions. Unavailable or unprovable freshness or
coverage means hold. A relevant new event can fetch evidence again;
`recalculate.requested` cannot.

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
   `website`. The company-email check needs that domain. LinkedIn normally
   compares its identity domain; when LinkedIn supplies no domain, the
   legal-name or alias fallback can still match. The platform gets back
   `202 {"run_id": "…"}`.
2. When that run finishes, the first verdict reaches your webhook: the registry matched
   (25) and LinkedIn put the contact at that company (20), so the score is 45.
   The decision is `manual_review_insufficient`, with reason codes for what is
   still missing.
3. The user verifies their work email. The platform POSTs `email.verified`,
   which earns `verified_email` (10) and `verified_company_email` (25). Score
   80.
4. The user supplies their RIR org handle. The platform POSTs
   `org_id.submitted`, the RIR lookup passes (25), and the score reaches 105.
   ORG-ID eligibility is now present, but the control gate is still false.
5. The user completes the POC token exchange after the live directory and
   delivery path are wired. A passed `poc_verified` adds 25 and passes the
   control gate. It proves access to the RIR-listed contact channel associated
   with the submitted organization or resource. It does not prove unrestricted
   legal authority to represent the company; business-policy checks and a
   recorded human review can still be required.
6. The computed decision is `approve`. While enforcement is off it arrives as
   `manual_review_insufficient` with that computed decision attached, and your
   registration team confirms it in the platform admin.

A document can add another 25 in closed staging through the JSON path. It can
do so in production only after question 2 in §5 is implemented and wired. A
verified POC adds 25 more once that flow is wired.

A newly accepted event queues a run, except
`reviewer.manual_approve`, which is recorded inline and returns no callback.
An idempotent replay returns the stored response; a rejected event creates no
run. A completed decision queues a callback. A permanently failed run needs
operator recovery, so acceptance does not guarantee a callback. Callback
delivery stops after the configured attempt ceiling, which is
eight by default, and then dead-letters for an operator. Dedupe on
`(case_id, run_id)`.

Evidence is optional at every step, and the tool scores whatever exists. When
a user later adds or changes something that matters, the platform re-sends the
matching event and verification runs again by itself. The full trigger table
is `PLATFORM_INTEGRATION.md` §3.

## 4. What your team builds

Five work areas:

1. **The event sender and decision receiver.** Send signed events when a
   registrant supplies or changes information (§3). Receive decisions at one
   HTTPS endpoint. Verify the signature and validate the body, then record
   the callback and its `(case_id, run_id)` dedupe key in the
   same transaction. Commit that transaction before returning 2xx. Until
   migration 025 is built and activated, callbacks have no wire ordering
   authority: keep a manual approval authoritative, record later valid
   automatic callbacks, and hold unordered results for review. See
   `PLATFORM_INTEGRATION.md` §4.
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
5. **The Salesforce pull consumer.** The tool exposes
   `GET /v1/cases/{case_id}/salesforce-projection`; it does not push to
   Salesforce. Your service must call that endpoint, apply the returned field
   mapping to the sandbox and production orgs, and own retries and
   reconciliation, including backfills. See `PLATFORM_INTEGRATION.md` §7.

## 5. The open questions

Two questions are open, and both are about work your platform may already do.
Either answer works for us. Neither blocks closed staging. Both block
production because each answer still needs a real provider wired into the
production profile. They are not the only production blockers; §8 lists the
rest.

**Question 1. Can your platform send the POC verification email?**

The POC exchange proves access to the RIR-listed contact channel associated
with the submitted organization or resource. It does not prove unrestricted
legal authority to represent an entity. The token goes to the address the
regional registry lists for that contact, never to an address the user typed.
The live RDAP-backed directory that can find that address is implemented, but
the production pipeline does not select it yet.

- **If yes:** your transactional email sends the message. We still have to
  agree and build the message that gives your service the token, the
  registry-listed recipient and the case reference.
- **If no:** we still have to build and wire an email provider for the tool.
  It will use an Amazon SES identity and sending domain that IPv4.Global
  provisions.

Either way the token rules stay ours. §4 item 2 lists them and
`PLATFORM_INTEGRATION.md` §5 has them in full. The POC check cannot award its
25 points in production until the chosen path is built and wired.

**Question 2. Can your platform read the fields off an uploaded document?**

The fields are the legal name, the registered address, the registration number
and the issuing jurisdiction, each as printed on the document rather than as
the user typed it.

- **If yes:** the platform writes them into the shared bucket as a small JSON
  object and posts `document.uploaded` pointing at it. The tool reads that
  JSON and compares it against the registries; the tool runs no OCR on this
  path. The JSON reader works in development and staging today, but production
  refuses that development setup. We still need a production implementation
  that accepts and validates the agreed extracted fields.
- **If no:** the platform posts `document.uploaded` pointing at the original
  PDF or image, and the tool runs OCR itself. That path is not built. It needs
  an OCR provider chosen and contracted by IPv4.Global, provider wiring, and
  agreed limits on file type and size.

Both options use the same event name, but one references extracted JSON and
the other an original file. Build the JSON staging path now and agree the
production format before implementing uploads. `PLATFORM_INTEGRATION.md` §6
describes both options.
The document check cannot award its 25 points in production until the chosen
path is wired into the production profile.
Matching extracted fields to the submission does not authenticate the document
or its issuer. Agree the production document-trust rule and upload controls
before relying on those points for unattended approval.

**Agreed at kickoff: your team hosts and operates it.**

IPv4.Global maintains the code and publishes releases. Your team pulls a
release and redeploys. Do not edit code on the server.

- Python 3.11 and FastAPI. The automated suite currently tests
  **PostgreSQL 16**. Compatibility with older server versions is not
  established here. The work queue and webhook outbox both live in Postgres.
  No Redis or message broker is used.
- Also needed: an S3 bucket for evidence files, and outbound HTTPS to the
  public registries.
- Three long-running stateless services. Retention is a daily scheduled job,
  not a fourth service, and database migration is a separate one-shot job:

| Process | Command |
|---|---|
| API | `uvicorn kyc_tool.api.app:create_app --factory` |
| Pipeline worker | `python -m kyc_tool.workers.pipeline_worker` |
| Outbox publisher | `python -m kyc_tool.workers.outbox_worker` |
| Retention (daily cron) | `python -m kyc_tool.workers.retention` |

- A `Dockerfile` ships in the repo. Migrations are versioned, and several are
  forward-only once real data exists, starting with migration 010 (PR 5a).
  Follow the stop/start and cutover procedure for each release in
  `docs/DEPLOYMENT.md`; do not assume every update is a rolling restart.
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

Initial staging integration runs with `KYC_ENFORCE_POSITIVE_DECISIONS=false`.
Prove signed events, the durable receiver and all eight receiver acceptance
cases before changing that setting. An automation rehearsal may then use
`true` only with approval from the IPv4.Global integration owner and TechCraft
platform owner, in an isolated, closed sandbox with synthetic accounts. It
must not change live account or buying permissions; return the setting to
`false` when the rehearsal ends. This is not a production go-live. The M2 gate
still requires the full production-readiness backlog, an end-to-end staging run on real
adapters, and the platform cutover before automation can be turned on in
**production**. Production launches with it off. The tool investigates, the
review team confirms, and the flag flips per environment only after that gate
is complete.

> **Keep staging closed until inbound v1 is actually disabled.** PR 5a adds
> path-bound HMAC v2, but a v1-only request during the dual-accept window is
> still path-unbound, so a signed event captured inside the skew window could
> be replayed to a different case. The redirect closes for v2 at deploy. For
> everyone else it closes only once inbound v1 is disabled, meaning the
> zero-witness is satisfied and `hmac_v1_inbound_sunset_at` takes effect. Keep
> staging's perimeter closed until then, not merely until PR 5a ships.

Checklist:

1. RDS PostgreSQL 16, an S3 bucket, and ECS/Fargate services (or one EC2 box)
   for the API and two workers, plus scheduled retention and migration tasks.
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
   `KYC_ENFORCE_POSITIVE_DECISIONS=false`, and `CH_API_KEY` (§8 item 14),
   because the registry lookups are live.
5. Leave `KYC_ENVIRONMENT` at `development` for now. The Companies House,
   GLEIF, RIR RDAP and Floqer clients can make live calls in this profile, but
   the profile itself is still a development fixture and production refuses
   it. Documents arrive as extracted JSON, and the POC directory selected by
   the pipeline is empty, so a `poc.submitted` in staging cannot pass until the
   live directory is wired in (§8, not built yet). For that day set
   `KYC_EMAIL_PROVIDER=file`, which appends each verification email as a JSON
   line to a local sink file (`.substrate/state/poc-emails.log` by default,
   moved with `KYC_EMAIL_FILE_PATH`), so your tests can read the token and the
   reference and finish the round-trip. That sink writes raw tokens to disk,
   so it is for closed staging only and production refuses it at boot. The
   production email and document paths still need the agreements in §5.
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
- For every decision callback, read `GET /v1/runs/{run_id}`. A response with
  `partial: true` means at least one source failed during that run; follow the
  partial-run procedure instead of treating the callback as fresh verification
  of every live check.
- POC round-trip, once the live directory is switched in (§6 step 5): after
  `poc.submitted`, read the token and the verification reference out of the
  email sink file, then POST `poc.token_verified` with both.
- The ops console (`/ui`, switched on with `KYC_UI_ENABLED=true` in staging)
  shows every case with its score, its gates and its run state, and it can
  compose signed test events from the browser.

## 8. Where things stand, and what we need from you

The service is not production-ready. The go/no-go gate is §8 of
`docs/superpowers/specs/2026-09-12-production-readiness-design.md`. Status at
this release falls into five states.

**Working now**

- Signed ingestion of all nine event types, with the typed request and
  response contract published in `/openapi.json`, and idempotent replay of a
  repeated key.
- Decision webhook delivery, at-least-once, with retries and dead-lettering.
- The read API and the pull-style Salesforce projection
  (`PLATFORM_INTEGRATION.md` §7). TechCraft still has to build the consumer
  that reads it and writes Salesforce.
- Website review and manual approval as signed events, plus the operator
  console with its live configuration and Salesforce destination-name mapping.
- Live registry clients: Companies House (which needs the key in item 14),
  GLEIF and the five RIR RDAP strategies. The current pipeline selects them
  from a development fixture profile; it selects an empty POC directory, not
  the live RDAP-backed directory. Production profile wiring is still missing.
- Reviewer information requests. A reviewer can record what a stalled case
  needs, most often the RIR org handle, and `GET /v1/cases/{id}` serves it as
  `information_requested`.
- The conformance kit you can run against staging.
  `python -m kyc_tool.conformance` runs the repository's reference signer,
  client and callback receiver against the published contract. It checks the
  tool side; it does not test TechCraft's sender or receiver. Use its published
  vectors as inputs to separate tests of your implementation
  (`PLATFORM_INTEGRATION.md` §11).
- S3-compatible evidence storage, and the boot check that refuses stand-in
  providers and unsafe configuration in production.

**Built and deliberately switched off**

- Enforcement of positive decisions. It stays off at launch, so a computed
  approval arrives as `manual_review_insufficient` carrying `enforcement_held`
  (`PLATFORM_INTEGRATION.md` §4).
- Retirement of the v1 signature. Both versions are accepted today, and the
  sunset dates are set at cutover.

**Waiting on the two decisions in §5 and the provider work they select**

- The POC verification flow. Token creation and validation work. The
  production pipeline still needs the live RDAP-backed directory and either
  the platform handoff or the tool-side email provider. Nothing sends the
  message yet.
- The document check. The development JSON reader works, but production
  refuses that stub. Tool-side OCR is not built. Either answer needs a real
  provider and production-profile wiring.

**Not built yet, for reasons of our own**

- The production provider profile: the switch that hands the pipeline the
  built POC directory, and whichever providers your two answers call for. The
  live Floqer client already exists and carries over.
- Migration 025 and platform-authoritative callback ordering. The database
  assigns an internal decision number today and can suppress some older local
  deliveries, but the number is not on the wire and that suppression is not
  an ordering guarantee. Concurrent senders and delayed callbacks can still
  arrive out of order or after a manual approval. Use the interim receiver rule in
  `PLATFORM_INTEGRATION.md` §4.
- A downloadable versioned contract bundle.
- The load and soak harness.

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
   projection: its owner, when it pulls, how it authenticates, and how it
   handles failed writes, including retry and backfill reconciliation.
5. AWS deployment access for whoever on your side deploys.

**Your answers** (write each one down and we turn it into a tested contract)

6. Your callback receiver's behaviour: that one database transaction records
   the valid callback and dedupe key, commits before returning 2xx, and treats
   an exact duplicate as already processed.
7. The ordering bootstrap, which migration 025 cannot be built and activated
   without:
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
   OCR provider. Either choice also needs its production provider and profile
   wired. §5 asks the question in full, and `PLATFORM_INTEGRATION.md` §6 has
   both wire paths.
10. POC verification email. Can your platform send it from its own
    transactional email, given the token and the registry-listed recipient
    over a typed contract? If it cannot, the tool sends through an SES
    identity that IPv4.Global provisions. Either choice needs its provider
    built and wired. Confirm as well that you host the POC page and echo back
    both `token` and `token_id`. §5 asks the question,
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
