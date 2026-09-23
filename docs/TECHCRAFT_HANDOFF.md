# IPv4.Global KYC/KYB — Integration and Operations Reference

Supported environment: closed staging.

Production use is not supported by this release.

Section numbers restart in each part.

## Contents

- Part 1: Product and integration briefing (`docs/PLATFORM_BRIEFING.md`)
- Part 2: Platform integration reference (`docs/PLATFORM_INTEGRATION.md`)
- Part 3: Deployment guide (`docs/DEPLOYMENT.md`)
- Part 4: Operations runbook (`docs/RUNBOOK.md`)
- Part 5: Alert reference (`docs/ALERTS.md`)
- Part 6: Salesforce mapping (`docs/SALESFORCE_MAPPING.md`)
- Part 7: Production readiness (`docs/PRODUCTION_READINESS.md`)

# Part 1: Product and integration briefing

The tool supports testing in a closed staging environment. It is not ready for
production. Section 4 defines platform responsibilities, §5 defines the two
required provider decisions, §6 defines staging, and §8 records the remaining
delivery gaps and numbered inputs. `PLATFORM_INTEGRATION.md` is the authority
for the complete wire request and response contract.

## 1. What it is

The tool evaluates evidence about one registrant and the company they claim to
represent. It applies the published scoring and gate policy and returns a
decision for the platform to handle. It does not certify unrestricted legal
authority, drive platform screens or change platform state.

The work happens in the background. The platform POSTs an event (a
registration, a verified email, an uploaded document, an RIR org handle) and
gets an acknowledgment straight away. The tool then looks the claimed company
up in Companies House, GLEIF and the five regional internet registries (ARIN,
RIPE, APNIC, LACNIC, AFRINIC). It screens the broker blocklist. It scores what
it found and POSTs the verdict to a platform-hosted webhook.

The design has one message that may go directly to a person: the POC
verification email, sent only to the address the registry lists for that
contact. Nothing sends that message in the production path yet. Whether the
tool sends it or the platform does is one of the two required decisions (§5 and
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
naming what is missing or wrong, and that code is what the review team acts
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
the `platform_account_id` held for them. A second registrant at the same
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
2. When that run finishes, the first verdict reaches the platform webhook: the registry matched
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
   `manual_review_insufficient` with that computed decision attached, and the
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

## 4. Platform responsibilities

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
   from the verification email, and the platform posts both back to the tool. Codes
   are single-use, they expire after 72 hours, and they die if the user edits
   the identity details behind them. Recovery is always the same: submit the
   POC again. See `PLATFORM_INTEGRATION.md` §5.
3. **Admin views for held cases.** The review team works in the platform
   admin, so each held case needs its decision, its score, its checks and its
   reason codes on screen. They come from the webhook body, or from
   `GET /v1/cases/{id}` through a pull integration. Keep score and reason codes
   admin-only. Users see their status and the next useful step. See
   `PLATFORM_INTEGRATION.md` §7.
4. **Status notifications and reviewer assignment.** Emails to users, alerts
   to admins and routing a held case to a reviewer are platform features keyed
   off the webhook result. There is a suggested result-to-action mapping in
   `PLATFORM_INTEGRATION.md` §4.
5. **The Salesforce pull consumer.** The tool exposes
   `GET /v1/cases/{case_id}/salesforce-projection`; it does not push to
   Salesforce. The platform service must call that endpoint, apply the returned field
   mapping to the sandbox and production orgs, and own retries and
   reconciliation, including backfills. See `PLATFORM_INTEGRATION.md` §7.

## 5. Required provider decisions

Two provider decisions remain open. Either option supports the product design,
and neither blocks closed staging. Both block production until the selected
provider is wired into the production profile. Section 8 lists the other
production blockers.

**Decision 1. POC verification email owner**

The POC exchange proves access to the RIR-listed contact channel associated
with the submitted organization or resource. It does not prove unrestricted
legal authority to represent an entity. The token goes to the address the
regional registry lists for that contact, never to an address the user typed.
The live RDAP-backed directory that can find that address is implemented, but
the production pipeline does not select it yet.

- **Platform-owned:** the platform transactional-email service sends the
  message. A typed delivery contract must provide that service with the token, the
  registry-listed recipient and the case reference.
- **Tool-owned:** an email provider must be built and wired for the tool.
  It will use an Amazon SES identity and sending domain that IPv4.Global
  provisions.

Token creation, expiry, identity binding and consumption remain tool
responsibilities. Section 4 item 2 lists the rules and
`PLATFORM_INTEGRATION.md` §5 has them in full. The POC check cannot award its
25 points in production until the chosen path is built and wired.

**Decision 2. Document-field extraction owner**

The fields are the legal name, the registered address, the registration number
and the issuing jurisdiction, each as printed on the document rather than as
the user typed it.

- **Platform-owned:** the platform writes them into the shared bucket as a small JSON
  object and posts `document.uploaded` pointing at it. The tool reads that
  JSON and compares it against the registries; the tool runs no OCR on this
  path. The JSON reader works in development and staging today, but production
  refuses that development setup. A production implementation must accept and
  validate the agreed extracted fields.
- **Tool-owned:** the platform posts `document.uploaded` pointing at the original
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

### Hosting and operations ownership

TechCraft hosts and operates the tool in IPv4.Global's AWS account. IPv4.Global
maintains the code and publishes releases. TechCraft pulls a
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
  forward-only once real data exists, starting with migration 010.
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
`false` when the rehearsal ends. This is not a production go-live. Production
approval requires the full `PRODUCTION_READINESS.md` backlog, an end-to-end staging run on real
adapters, and platform cutover approval before automation can be turned on in
**production**. Production launches with it off. The tool investigates, the
review team confirms, and the flag flips per environment only after that gate
is complete.

> **Keep staging closed until inbound v1 is actually disabled.** Path-bound
> HMAC v2 is available, but a v1-only request during the dual-accept window is
> still path-unbound, so a signed event captured inside the skew window could
> be replayed to a different case. The redirect closes for v2 at deploy. For
> everyone else it closes only once inbound v1 is disabled, meaning the
> zero-witness is satisfied and `hmac_v1_inbound_sunset_at` takes effect. Keep
> staging's perimeter closed until then, not merely until v2 is deployed.

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
4. Core env vars: `KYC_DATABASE_URL`, `KYC_PLATFORM_CALLBACK_URL` (the
   staging receiver), `KYC_OBJECT_STORE=s3` with `KYC_S3_BUCKET`,
   `KYC_ENFORCE_POSITIVE_DECISIONS=false`, and `CH_API_KEY` (§8 item 14),
   because the registry lookups are live. Also set `KYC_UI_ADMIN_TOKEN`, and
   `KYC_READ_AUTH_REQUIRED=true` on any host another machine can reach.
   Development mode requires neither, so without them the requeue endpoints,
   an enabled console and the `/v1` reads accept anyone who can reach them
   (`DEPLOYMENT.md` §2).
5. Leave `KYC_ENVIRONMENT` at `development` for now. The Companies House,
   GLEIF, RIR RDAP and Floqer clients can make live calls in this profile, but
   the profile itself is still a development fixture and production refuses
   it. Documents arrive as extracted JSON, and the POC directory selected by
   the pipeline is empty, so a `poc.submitted` in staging cannot pass until the
   live directory is wired in (§8, not built yet). For that day set
   `KYC_EMAIL_PROVIDER=file`, which appends each verification email as a JSON
   line to a local sink file (`var/poc-emails.log` by default,
   moved with `KYC_EMAIL_FILE_PATH`), so staging tests can read the token and the
   reference and finish the round-trip. That sink writes raw tokens to disk,
   so it is for closed staging only and production refuses it at boot. The
   production email and document paths still need the agreements in §5.
6. `alembic upgrade head`, start the processes, check `/readyz`.
7. Smoke test: send a signed `kyb.run_requested` and watch the verdict arrive.
   Until the platform receiver exists, `scripts/dev_receiver.py` is a stub that prints
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

## 8. Release status and required inputs

The service is not production-ready. The go/no-go gate is in
`PRODUCTION_READINESS.md`. Status at
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
- The conformance kit for staging checks.
  `python -m kyc_tool.conformance` runs the repository's reference signer,
  client and callback receiver against the published contract. It checks the
  tool side; it does not test TechCraft's sender or receiver. Use its published
  vectors as inputs to separate platform-implementation tests
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
  the platform delivery contract or the tool-side email provider. Nothing sends the
  message yet.
- The document check. The development JSON reader works, but production
  refuses that stub. Tool-side OCR is not built. Either option needs a real
  provider and production-profile wiring.

**Not built in this release**

- The production provider profile: the switch that hands the pipeline the
  built POC directory, and the providers selected by the two required decisions. The
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
  never replaces the platform UI and never changes a historical decision when
  configuration or mappings change.

The remaining requirements use stable item numbers: five platform
configuration values, eight required decisions, and two IPv4.Global inputs.

**Platform configuration** (through the deployment secret manager or written
deployment config, never chat)

1. Callback base URLs, staging and production.
2. Key ids and the HMAC secrets for each wire direction, through the secret
   manager.
3. S3 bucket, key prefixes and IAM ownership for evidence and extracted JSON.
4. The Salesforce sandbox and the platform service that will consume the
   projection: its owner, when it pulls, how it authenticates, and how it
   handles failed writes, including retry and backfill reconciliation.
5. AWS deployment access for the TechCraft deployment owner.

**Required decisions** (record each decision as an input to the tested contract)

6. Platform callback receiver behavior: one database transaction records
   the valid callback and dedupe key, commits before returning 2xx, and treats
   an exact duplicate as already processed.
7. The ordering bootstrap, which migration 025 cannot be built and activated
   without:
   - where the platform's accepted-run ledger lives
   - how IPv4.Global queries the accepted decision for any case
   - how manual approvals and reverted decisions appear in it
   - who signs the bootstrap response, and how that signer is identified
   - the maximum bootstrap size, and the recovery procedure
8. Review-task changes. The default is a platform-hosted webhook for a task
   opened, completed or cancelled. If polling
   `GET /v1/review-tasks?status=open` is selected instead, record the cursor
   rule, required freshness and missed-poll recovery behavior.
9. Document extraction. Record whether the platform will extract the four fields and send
   them as JSON. Otherwise, the tool runs OCR and IPv4.Global contracts an
   OCR provider. Either choice also needs its production provider and profile
   wired. §5 asks the question in full, and `PLATFORM_INTEGRATION.md` §6 has
   both wire paths.
10. POC verification email. Record whether the platform will send it from its own
    transactional email, given the token and the registry-listed recipient
    over a typed contract. Otherwise, the tool sends through an SES
    identity that IPv4.Global provisions. Either choice needs its provider
    built and wired. Confirm that TechCraft hosts the POC page and echoes back
    both `token` and `token_id`. §5 asks the question,
    `PLATFORM_INTEGRATION.md` §5 has the wire detail.
11. Floqer. The contract is in place, and the tool calls a published shortcut
    in IPv4.Global's own Floqer account for discovery only, which feeds the
    LinkedIn match. Nothing is needed from the platform.
12. Operating targets: expected daily and peak case volume, concurrent runs,
    acceptable latency for light and full checks, soak duration, deployment
    region, maintenance-window constraints, and the availability and recovery
    objectives that apply to IPv4.Global.
13. Reviewer information requests. When a case stalls for evidence the
    registrant never supplied, most often the RIR org handle, a reviewer
    records the ask in the console and `GET /v1/cases/{id}` serves it as
    `information_requested` (`PLATFORM_INTEGRATION.md` §7). Select either
    polling that field or a new outbound webhook message. The webhook is built
    only after that decision is recorded. The request is recorded either
    way.

**From IPv4.Global**

14. The Companies House API key, through the secret manager.
15. Email: if the tool ends up sending, an SES identity and a sending domain
    in the secret manager and deployment config.

## 9. Doc map

| Topic | Document |
|---|---|
| Full API contract, signatures, payloads, webhook | `docs/PLATFORM_INTEGRATION.md` |
| Deploying, releasing, rollback, monitoring | `docs/DEPLOYMENT.md` |
| Operating it: env vars, health, dead letters, console | `docs/RUNBOOK.md` |
| Production go/no-go requirements | `docs/PRODUCTION_READINESS.md` |
| Salesforce field-by-field mapping and value rules | `docs/SALESFORCE_MAPPING.md` |
| Alert expressions and scrape requirements | `docs/ALERTS.md` |
| Machine-readable policy files used by the service | `KYC_Tool_Build_Package/machine_readable/` |

# Part 2: Platform integration reference

This guide covers the TechCraft event sender,
decision receiver, reviewer actions, Salesforce reads, and the proposed POC
confirmation page. Product decisions and the numbered delivery checklist are
in `PLATFORM_BRIEFING.md` §5 and §8.

This release is for closed staging, not a production launch. Production provider
wiring and ordered callback delivery are unfinished. The email and document
options in §5–§6 require recorded decisions before their production paths can
be built.

## 1. The model

A case is one registrant: the person signing up on the platform on behalf of a
company. The platform sends the submitted data, the tool checks it against
public registries, and the tool returns a verdict for the registration team.

The platform POSTs events such as registration data, a verified email or an
ORG-ID. Accepted new events normally queue background work: the tool gathers
evidence, scores it, and POSTs a decision to the platform webhook. Manual approval is handled
inline. Replaying an accepted event does not create another run (§3).

Decisions: `approve`, `approve_buy_locked` (account OK, purchasing held until
ORG-ID verifies), `manual_review_insufficient`, `reject`.

`platform_account_id` on the sign-up event is the platform id for that person,
and every returned decision concerns that case and contact. Company
evidence (registry records, ORG-ID, website) is about the company they claim.
Company email, ORG-ID and LinkedIn results are supporting evidence. Automated
control proof requires a passed POC token sent to the RIR-listed contact channel
associated with the submitted organization or resource. That proves access to
the channel, not unrestricted legal authority to represent the company.
Approving a case approves the contact, not the company. A second registrant at
the same company is a second case, with its own `case_id`.

**Initial integration posture:** auto-enforcement is off. A computed `approve` /
`approve_buy_locked` is delivered as `manual_review_insufficient` with an
`enforcement_held` marker (§4), and the registration team confirms it.
Production enforcement requires completion of the full backlog in
`PRODUCTION_READINESS.md`, real-provider staging tests and platform cutover
approval. An optional automation rehearsal uses synthetic sandbox accounts
only, after the approvals in `PLATFORM_BRIEFING.md` §6. It does not permit live
permission changes or bypass the interim receiver holds in §4. This guide is
not permission to enable production enforcement.

## 2. Authentication (both directions)

Sign every request in both directions. The tool always verifies event
signatures (`KYC_AUTH_DISABLED` is for one developer's machine only). It
verifies read signatures in production, and in staging once
`KYC_READ_AUTH_REQUIRED=true` is set. `docs/DEPLOYMENT.md` §2 requires that on
any staging host another machine can reach. Platform requests and tool
webhooks carry the same two headers:

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

The following example shows the v1 signature calculation. It is not a complete
request validator: a receiver must also handle missing or malformed headers
without accepting the request. New integration code should use v2 below.

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
case. **v2** signs the method and path too. During the migration both formats
are accepted. Agree separate inbound and outbound retirement dates with
IPv4.Global. This document sets no dates. Inbound v1 cannot retire until its
recorded observation window contains no accepted v1 traffic. Headers change to:

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

Every platform update arrives as an event on one endpoint. There is no
registration call and no re-verify call: post what happened, and the tool
decides again.

```
POST /v1/cases/{case_id}/events
Idempotency-Key: <unique string per logical event; reuse on retry>
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
| 200 | Replay of an accepted key, or an inline manual approval | use the returned body; neither creates a new queued run |
| 400 | Missing `Idempotency-Key` | fix request |
| 401 | Bad/missing signature or stale timestamp | fix signing |
| 409 | Same `Idempotency-Key`, different body, or the named website task belongs to another case or is no longer open | check the cause: keep retry bytes unchanged, or refresh the task before acting |
| 422 | Payload failed validation, or an unknown top-level envelope key | fix payload |
| 404 | The review task named by the event does not exist | fix the task id |
| 503 | Configuration unavailable, or the v1 signature witness could not be recorded | safe retry |

Retry on network failure with the **same** key and **same** bytes. You get 200
back rather than a duplicate run.

### Event types and payloads

| event_type | Payload (required unless noted) | Notes |
|---|---|---|
| `kyb.run_requested` | `company_legal_name`, `contact`, `platform_account_id`; optional `address`, `registration_number`, `jurisdiction`, `website` | Send at registration. `contact` requires `name` and `email` (the sign-up address), with optional `title`, `first_name`, `last_name`. Supply `website` for the company-email check and LinkedIn domain comparison. LinkedIn requires the person's name and company identity to match: domain first, or exact company name/alias only when the LinkedIn record has no domain. Company-name and title comparisons are also recorded for review. Title does not decide the result. |
| `email.verified` | `email`, `domain`, `verified_at` | you own email verification, and this asserts it happened. `email` must be the contact's sign-up address (`contact.email`). The tool records it as sent and does not cross-check the two |
| `org_id.submitted` | `rir`, `org_handle` | `rir` ∈ `arin, ripe, apnic, lacnic, afrinic`. Optional at registration. Send it right after the sign-up event when the registrant supplied a handle there, and again whenever they add or change one later. The check runs the moment a handle arrives. A reviewer may also record one in the operator console when the contact supplies it by other means, and the envelope's `actor` says which (`reviewer` rather than your own `user`/`system` actor) |
| `poc.submitted` | `rir`, `poc_handle`; optional `org_handle`, `resource` | starts the verification email (§5) |
| `poc.token_verified` | `token_id`, `token`, `verified_at` | posted by your confirmation page (§5) |
| `document.uploaded` | `object_ref`, `doc_type` | see §6 |
| `website.review_completed` | `task_id`, `result`, `reviewer_id`; optional `reason_codes` | completes a website review task (§7). Needs a matching reviewer actor (below) |
| `reviewer.manual_approve` | `reviewer_id`; optional `note` | answers 200 inline with the case state. No run, no callback. Buying stays locked without a verified ORG-ID. Needs a matching reviewer actor (below) |
| `recalculate.requested` | `{}` | re-scores from stored evidence, with no new fetches |

Send events in the order they happen. Each accepted new event except
`reviewer.manual_approve` queues a run. Rejected events and idempotent replays
do not. A run that fails permanently needs operator recovery. Accepting an
event does not guarantee a callback will arrive.

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

Evidence is optional at every step, and the tool scores whatever exists. When
verification-relevant information is added **or changed**, send the matching
event. The tool re-runs and returns a fresh
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
re-verified, so a score can drop after an edit (§5). That decrease is expected.

## 4. Platform decision webhook

The tool POSTs every verdict to this endpoint. It is the first required
platform integration.
One body carries the decision, the score behind it and the reason codes for
each check.

Expose HTTPS `POST {your_base_url}/kyc/decision`. We sign it per §2, dual-emitting
v1 (the legacy shared secret) and v2 (the dedicated **outbound** secret + key id)
until the outbound sunset, then v2 only. Two lines of the §2 canonical differ
on this direction: it reads `tool->platform`, and the idempotency-key line is
empty. The v2 signature binds the **literal** request path, so if
`{your_base_url}` has a path prefix (e.g. `…/hooks`), the tool signs
`/hooks/kyc/decision` rather than `/kyc/decision`. Verify against the full path
you received.

Your receiver must perform these steps in order:

1. Verify the signature and validate the complete callback body.
2. In one database transaction, record the callback and its `(case_id, run_id)`
   deduplication record. Store any apply-or-hold result in that same transaction.
   The current release's ordering restrictions below still apply.
3. Commit the transaction, then return 2xx. An exact valid duplicate already
   committed can also receive 2xx without repeating its effects.

Do not acknowledge before commit: after a 2xx the tool stops retrying, even if
your process then crashes. Reject invalid callbacks without updating the
deduplication record. If the same identity arrives with different content,
hold it for investigation rather than treating it as an exact duplicate.

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
  "buy_enablement": "enabled",
  "checks": [
    {"type": "verified_company_email", "status": "pass", "points": 25,
     "source": "platform_email_verification", "reason_codes": []},
    {"type": "official_registry_match", "status": "pass", "points": 25,
     "source": "companies_house", "reason_codes": []},
    {"type": "org_id_match", "status": "pass", "points": 25,
     "source": "arin_rdap", "reason_codes": []},
    {"type": "poc_verified", "status": "pass", "points": 25,
     "source": "rir_poc_record_plus_token", "reason_codes": []},
    {"type": "verified_email", "status": "pass", "points": 10,
     "source": "platform_email_verification", "reason_codes": []}
  ],
  "decided_at": "2026-07-16T12:00:05Z",
  "enforcement_held": {
    "computed_decision": "approve",
    "reason": "positive_enforcement_disabled"
  }
}
```

- `buy_enablement` is `enabled` or `locked_org_id_required`. It reports whether
  the ORG-ID check makes buying eligible. It is not permission to enable buying.
- `checks[].reason_codes` are stable strings explaining any non-pass — show
  them to your registration team.
- `enforcement_held` appears while automatic positive decisions are held: it
  carries the decision the tool computed. Treat the case as pending human review.
- **Delivery is at-least-once.** Dedupe on `(case_id, run_id)`. Retries back
  off exponentially (defaults: base 10 s, 8 attempts) before dead-lettering on
  the tool side. Delivery failures then need operator recovery (§8). Retries are
  not unlimited.
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

A partial run can retain older passing checks when a source fails. The callback
does not include a freshness or partial-run flag. Use `GET /v1/runs/{run_id}`
for `partial` and the invoked adapters' `adapters[].fetched_at` values. Use
`GET /v1/cases/{case_id}/checks` for the current live checks and their
`checks[].created_at` values. `partial: false` means the sources invoked in that
run did not fail. It does not prove that every live check was fetched in that
run.

These reads are review aids. A check's `created_at` is check-record time, not
proof of provider-evidence freshness. The checks endpoint shows current
live-check state, not a callback-time snapshot. Combining the reads cannot
prove source age or bind evidence to the callback decision. A full source-age
and coverage contract bound to the decision must still be specified, built and
accepted before automatic production decisions. Unavailable or unprovable
freshness or coverage means hold. Send another relevant event to fetch evidence
again; `recalculate.requested` does not fetch.

### Permission precedence

Receiver acceptance and effectiveness are separate from permission changes.
Apply these rules in order; a later row cannot override an earlier hold.

| Priority | Condition | Required outcome |
|---|---|---|
| 1 | Signature or complete body is invalid | Reject without a callback or dedupe record. |
| 2 | Same `(case_id, run_id)`, different content | Create an integrity hold for investigation. Preserve the original durable body and outcome; do not classify the variant as an exact duplicate or emit effects. |
| 3 | Exact duplicate: same identity and content | Acknowledge from the original durable outcome without repeating an effect. |
| 4 | A recorded manual decision is current | Keep it authoritative. Record later valid automatic callbacks without replacing it. |
| 5 | First accepted valid callback, with no current manual or automatic decision | Record it as the current automatic receiver decision. |
| 6 | Later automatic callback would replace a current automatic decision without ordering authority, or automatic callbacks conflict | Record it and hold effectiveness for review. Do not use arrival order, `decided_at` or `event_sequence` as authority. |
| 7 | The run is partial, required source coverage is missing, or freshness is unavailable or unacceptable | Hold for review and revalidation. |
| 8 | `enforcement_held` is present, the decision is `manual_review_insufficient`, or a positive approval candidate has an unmet gate | Hold both account approval and buying. `buy_enablement=enabled` does not clear the hold. |
| 9 | Current release in staging | Store the receiver result, but do not mutate LIVE account or buying permissions, including suspension. Synthetic sandbox permission effects may be tested. |
| 10 | Future production, after every prior rule and production gate passes | Apply the account result and ORG-ID purchase eligibility as separate permissions. |

The interim receiver records the first accepted callback as its current
automatic decision when no higher-priority rule prevents effectiveness. A
current automatic receiver decision does not grant LIVE permissions. A later
automatic replacement without ordering authority is recorded but held from
effectiveness. Current staging never changes LIVE permissions either way.

For every valid new callback, fetch any required run and freshness evidence
beforehand. Then, under the same per-case transaction or serialization used to
persist the callback, re-read the current ledger and manual authority, apply the
precedence above, and record the callback, dedupe identity and apply-or-hold
result atomically. Commit before 2xx. External effects may follow only from that
committed outcome.

For the future production behavior, `approve` is an account-approval candidate
and `buy_enablement=enabled` is a buying candidate. `approve_buy_locked` is an
account-approval candidate while buying remains locked. A held decision enables
neither. A `reject` can drive the approved deny-or-suspend policy only after the
receiver and evidence requirements pass. The production gate must pass too. Exact matched
inactive-registry evidence produces a held manual-review decision, not `reject`.

### Receiver acceptance cases

TechCraft must execute these cases against its own durable receiver. Acceptance
evidence has not been collected. The conformance utility does not certify
permission enforcement.

#### A1 — Invalid callback

- **Input:** A callback with an invalid signature, unknown field or wrong field type.
- **Expected record:** No callback or dedupe row and no apply-or-hold result.
- **Expected permissions:** Account and buying state stay unchanged.
- **Observable result:** A non-2xx response and no row for the rejected identity.

#### A2 — First valid callback

- **Input:** A new, valid callback with a previously unseen `(case_id, run_id)`.
- **Expected record:** Callback, dedupe identity and apply-or-hold result in the same transaction.
- **Expected permissions:** The current release records the result but makes no LIVE permission change.
- **Observable result:** The transaction commit succeeds before 2xx; a crash before commit produces no 2xx and the tool retries.

#### A3 — Exact duplicate

- **Input:** The same valid callback bytes after A2 committed.
- **Expected record:** The existing durable callback and dedupe row remain single.
- **Expected permissions:** Return the stored outcome with no repeated effect.
- **Observable result:** A 2xx response, one durable identity and no duplicate notification or permission write.

#### A4 — Held positive with buy eligibility

- **Input:** `decision=manual_review_insufficient`, `enforcement_held.computed_decision=approve` and `buy_enablement=enabled`.
- **Expected record:** Store the decision, buy state and any enforcement-hold marker.
- **Expected permissions:** Account and buying receive no LIVE permission change; buying stays locked despite ORG-ID eligibility.
- **Observable result:** The review view shows account status and buying status separately.

#### A5 — Partial run or unavailable freshness

- **Input:** `partial: true`, missing required run coverage, or freshness evidence unavailable or outside the approved policy.
- **Expected record:** Store and acknowledge the valid callback with a review hold.
- **Expected permissions:** No LIVE permission changes; `buy_enablement=enabled` does not clear the hold.
- **Observable result:** The case enters revalidation or review and records the missing coverage or freshness evidence.

#### A6 — Manual approval precedence

- **Input:** A valid automatic callback arrives after a recorded manual approval.
- **Expected record:** Store and acknowledge the automatic callback without replacing the manual record.
- **Expected permissions:** The manual approval remains authoritative until the platform's governed release process changes it.
- **Observable result:** The effective source remains manual and the automatic callback remains auditable.

#### A7 — Unordered or conflicting automatic callbacks

- **Input:** Two valid automatic callbacks conflict and no active platform ordering authority resolves them.
- **Expected record:** Store and acknowledge both callbacks with a review hold.
- **Expected permissions:** No LIVE permission change; never choose by last arrival or timestamp.
- **Observable result:** The conflict is visible for review and neither callback silently replaces the other.

#### A8 — Same identity with different content

- **Input:** A callback reuses the same `(case_id, run_id)` as A2 with different content.
- **Expected record:** Preserve the original durable body, dedupe row and original outcome; add an integrity hold without replacing them.
- **Expected permissions:** No permission effect follows from the conflicting variant.
- **Observable result:** An investigation opens, the original remains unchanged and the variant is not treated as an exact duplicate.

Two boundaries shape the volume. Only a broker-blocklist match ever
auto-rejects, so expect the review queue rather than rejections to fill up. And
sanctions screening is a platform responsibility **before** calling the tool.
The tool does not perform that screening or prove that your platform ran it.

**Display guidance.** Per case you have: decision, buy state, score, the five
gate booleans, and per-check status with reason codes. Show users the status
and the next useful step (verify your email, add your ORG-ID). Keep the score,
the gate booleans and the reason codes in admin views, because publishing
exactly why a check fails makes it easier to game. Wording is yours, and the
reason codes are stable strings safe to key copy on.

## 5. Platform POC verification page

The POC exchange proves access to the RIR-listed contact channel associated
with the submitted organization or resource. It does not prove unrestricted
legal authority to represent an entity. A code goes to the address the
regional registry lists, and the registrant types it back. The platform hosts
the confirmation page. Email ownership is a required provider decision.

1. You post `poc.submitted`.
2. Once the live directory is wired into the worker, the tool looks up the POC over RDAP: the
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

**Required decision: POC verification email owner**

- **Platform-owned** — the platform sends it through its existing transactional
  email. A typed delivery contract, still to be written, must carry the token,
  registry-listed recipient and case reference. The tool hosts no mail.
- **Tool-owned** — the tool sends it through an Amazon SES sender that IPv4.Global
  provisions with an identity and a sending domain. That sender is not built,
  and a production process refuses to boot while the email provider is the dev
  stub.

The token checks are built. The shipped worker still uses an empty POC
directory, so staging cannot complete this flow until the built live directory
is wired in. Production also needs the agreed email path. A file email sink
can support closed-staging tests. It is not a production sender.

## 6. Documents and extraction ownership

A registrant can upload a formation document. Before the tool can compare it
against what the user typed, someone has to read four fields off it: legal
name, address, registration number, jurisdiction. Which side reads them is the
second required provider decision.

**Required decision: document-field extraction owner**

- **Platform-owned** — the platform stores the upload (with existing virus scanning
  and quarantine unchanged), writes the four fields as a JSON object to the
  shared object store, and posts `document.uploaded` with `object_ref`
  pointing at that JSON. The tool runs no OCR and reads that JSON as posted.
  The numbered steps below are this path's contract.
- **Tool-owned** — the platform stores the upload and posts `document.uploaded`
  with `object_ref` pointing at the **original file** (PDF or image) in the
  shared object store. The tool runs an OCR engine and extracts the same four
  fields itself. That path needs an OCR provider chosen and contracted by
  IPv4.Global, plus agreed file-type and size limits. None of it is built: the
  current engine reads extracted JSON only, and production refuses that stub.

Both options use `document.uploaded`, but the object it references is different.
Build and test the extracted-JSON path below for staging. Do not send raw PDFs
or images to the current engine. Agree the production extraction contract
before implementing that upload path. Documents are optional at registration.
A later upload re-runs verification (§3).

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

Staging can use hand-extracted JSON. Choosing platform extraction does not
remove the current production boot restriction: an approved production provider
and its wiring still need to replace the development JSON-scan configuration.

## 7. Read API and review tasks

Everything the tool knows about a case is readable over signed GETs. Use them
to identify missing evidence and to mirror a case into Salesforce.

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
console when a case is waiting for evidence the registrant has not
supplied. The tool records the ask, and the platform owns the message that
reaches the contact. An entry drops off by itself once the case receives that
evidence, so there is nothing to close and nothing to acknowledge. `org_id`
clears when `org_id.submitted` arrives, and the other three clear the same way.

This field is not required to identify a missing ORG-ID. The decision
webhook's `checks[].reason_codes` already carry `org_id_submission_incomplete`,
which is the same fact at decision time. The delivery method for a reviewer's
request is `PLATFORM_BRIEFING.md` §8 item 13: poll this field or select a new
outbound message. Nothing outbound is built until that decision is recorded.

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

## 8. Hosting and deployment

TechCraft hosts and operates the tool in IPv4.Global's AWS account. IPv4.Global
maintains the code and cuts releases. Follow each release's migration and
stop/start instructions rather than assuming a rolling update is safe. No code
is edited on the server. A `Dockerfile` ships in the repo, and
`docs/RUNBOOK.md` is the operator guide
(every env var, health checks, dead-letter recovery).

- **Stack:** Python 3.11, FastAPI and PostgreSQL. CI tests PostgreSQL 16.
  Compatibility with older server versions is not established here. The job
  queue and webhook outbox use PostgreSQL, with no separate Redis or broker.
- **Also needed in production:** an S3-compatible bucket (evidence), outbound
  HTTPS (RDAP registries, Companies House, GLEIF), and an email provider if the
  tool ends up sending the §5 email.
- **Services:** API (`uvicorn kyc_tool.api.app:create_app --factory`), pipeline
  worker and outbox worker. Retention is a daily scheduled job. Migrations are
  a separate one-shot job. Commands and scaling limits are in `DEPLOYMENT.md` §1.
- **Deploy:** follow `DEPLOYMENT.md` §3–§4 and the applicable cutover procedure.
  Wire `GET /readyz` to
  the load balancer (checks DB, migration version, storage, and the config in
  production mode), and use `GET /healthz` for liveness.
- Config is environment variables prefixed `KYC_` (full table:
  `docs/RUNBOOK.md`). With `KYC_ENVIRONMENT=production` a misconfigured process
  refuses to boot and lists every violation — intentional fail-closed.

## 9. Release scope and production dependencies

This release includes the full event flow, the registry, ORG-ID, broker, LinkedIn,
document (extracted-fields) and email checks, scoring, webhooks, the review
queue, the audit trail and idempotent replays.

| Added later | Unblocked by |
|---|---|
| Tool-side OCR of raw files | extraction decision + engine choice |
| Live POC verification emails | the §5 answer, then an email provider + sending domain, or the platform hand-off contract |
| Companies House lookups in your deployment | `CH_API_KEY` in the deployment config (`PLATFORM_BRIEFING.md` §8 item 14). The adapter is live |
| LinkedIn matching in your deployment | the Floqer key and shortcut id in the deployment config. The adapter is live, and nothing is needed from the platform |
| `event_sequence` in callbacks | your confirmation |
| v1 signature retirement (v2 path-bound signing is live now, §2) | agreed dates and the recorded inbound zero-v1 observation window |
| Ordered callback delivery (`decision_sequence`) | migration `025` and the platform bootstrap/receiver agreement; not built in this release |
| Auto-enforcement | the full `PRODUCTION_READINESS.md` backlog, real-provider staging end-to-end tests and platform cutover sign-off |

The two provider decisions and ordering activation require additional contracts.
Do not treat proposed fields or delivery paths as available API features.

## 10. Required inputs and decisions

All remaining platform and IPv4.Global inputs are listed in
`docs/PLATFORM_BRIEFING.md` §8. Section 4 defines platform responsibilities,
and §5 defines the two required provider decisions.

Secrets never travel in chat, email, tickets, or documents: use the deployment
secret manager.

## 11. Conformance kit

Use the kit for staging smoke tests and signature comparisons. From an installed
checkout, run `python -m kyc_tool.conformance <mode>`. It exercises selected
contract checks. Passing it does not establish production readiness or verify
TechCraft's own sender and receiver implementations.

| Command | What it checks |
|---|---|
| `python -m kyc_tool.conformance vector` | Prints a fixed v2 test vector. Compare your own signer's output with it; running this mode alone does not exercise your signer. |
| `python -m kyc_tool.conformance send --tool <base-url> --case <case-id>` | Uses the kit's client to post the event types to the tool, exercise selected invalid requests and an idempotent replay, then read the case. It does not run your sender. It includes a v1 request unless you pass `--no-v1`. The website event uses a test task id and expects refusal when that task does not exist, so this does not prove a successful website-review round trip. |
| `python -m kyc_tool.conformance receive --port <n>` | A local diagnostic receiver for callbacks sent by the tool, not a test of your receiver. It reports signature/body checks and keeps valid callback identities in memory. It returns 2xx even when a check fails, and loses that memory on restart. Bind it only to local or closed-staging test traffic, using a tunnel if needed. Never copy its acknowledgement or storage behavior into a production receiver. |

`vector` and `send` print `check / expected / got / PASS|FAIL` tables and exit
non-zero if a row fails. `receive` prints a result line per callback with
details for failed checks. `send`
writes real events, including reviewer actions, so give it a throwaway case id
on staging. Its v1 request records v1 traffic: use `--no-v1` throughout an
inbound zero-v1 observation window. Waiting until the retirement date is too late. The
`receive` process reports failures in its output. It is not a CI exit-code gate.

`receive` validates every callback body through the `DecisionCallback` model
in `src/kyc_tool/api/schemas.py`, the same model the tool encodes with, so a
wrong field type or an unknown field fails its `body.schema` row and stays out
of its dedupe memory. That is still a check of the tool's output, not of your
receiver. Validate your own receiver against §4 and that model, and test
malformed bodies independently.

TechCraft must also test its actual receiver: crash before commit, retry an
exact callback, submit invalid content, and deliver callbacks out of order.
Confirm that no acknowledged callback is lost, no duplicate repeats an effect,
and no unordered callback overwrites a manual approval.

Secrets are read from the **environment only** — never a command-line argument,
never printed, never logged:

| Variable | Used by |
|---|---|
| `KYC_CONFORMANCE_V1_SECRET` | `send` (the v1 request), `receive` (the dual-emitted v1 callback signature) |
| `KYC_CONFORMANCE_INBOUND_SECRET`, `KYC_CONFORMANCE_INBOUND_KEY_ID` | `send` (v2) |
| `KYC_CONFORMANCE_OUTBOUND_SECRET`, `KYC_CONFORMANCE_OUTBOUND_KEY_ID` | `receive` (v2) |

# Part 3: Deployment guide

The platform team operates the KYC tool in IPv4.Global's AWS account.
IPv4.Global maintains the code and publishes releases. Deploy only a
published release. Do not edit code on the server; make changes in the repo and
include them in a later release.

This release is limited to closed staging. Production startup remains blocked
by unfinished provider wiring. Selecting the document-extraction and POC-email
owners does not remove that block. Production launch requires the work and
approvals in `PLATFORM_BRIEFING.md` §8.

## 1. One image: services and scheduled jobs

The repo `Dockerfile` builds a single image. Each process is the same image
with a different command:

| Process | Command | HTTP |
|---|---|---|
| API | (image default) `uvicorn kyc_tool.api.app:create_app --factory --host 0.0.0.0 --port 8000` | 8000 |
| Migrations (one-shot) | `alembic upgrade head` | — |
| Pipeline worker | `python -m kyc_tool.workers.pipeline_worker` | — |
| Outbox publisher | `python -m kyc_tool.workers.outbox_worker` | — |
| Retention (daily cron) | `python -m kyc_tool.workers.retention` | — |

Run the API, pipeline worker and outbox publisher as services. Run migrations
once per deployment when required, and retention as a daily scheduled job.
Scale the API and pipeline workers horizontally as needed. The job queue keeps
each case's jobs in order (oldest first, one at a time), so extra workers do
not run the same case's jobs concurrently. Callback delivery order is a
separate contract: `docs/PLATFORM_INTEGRATION.md` §4.
Disable the image's HTTP healthcheck on worker containers (they serve no HTTP).

## 2. Environments

| | Staging | Production |
|---|---|---|
| `KYC_ENVIRONMENT` | `development` (until real providers are available) | `production` |
| `KYC_ENFORCE_POSITIVE_DECISIONS` | `false` for initial integration; `true` only for an authorized synthetic-account rehearsal | `false`; enable only after all `PRODUCTION_READINESS.md` requirements pass |
| Providers | live registry lookups (`CH_API_KEY` set), with stand-ins for the POC directory, document extraction and email (file sink) | real registry providers, required. Real OCR and email providers are needed only if the tool extracts documents or sends the POC email, which are the two open questions in `docs/PLATFORM_INTEGRATION.md` §5/§6. Either way `KYC_OCR_ENGINE` and `KYC_EMAIL_PROVIDER` must leave their dev stubs (`docs/RUNBOOK.md`). |
| Secret | staging secret | separate production secret |
| `KYC_READ_AUTH_REQUIRED` | `true` on any host another machine can reach. Staging runs as `development`, where every `/v1` read (cases, checks, runs, review tasks, metrics) is otherwise unsigned. The platform signs reads exactly as it will in production. | `true` (boot refuses anything else) |
| `KYC_UI_ADMIN_TOKEN` | Set on every staging host, whether or not the console is enabled. Without it, the always-mounted `/v1/ops` requeue endpoints and an enabled console accept anyone who can reach them. Once it is set, those endpoints and the console require it. Configuration reads accept it or a platform-signed request. | required, not blank (boot refuses an empty token) |
| `KYC_AUTH_DISABLED` | Never set on a shared host. The tool cannot tell staging from a developer machine, so nothing refuses it here. | refused at boot |

Production mode validates config at boot and refuses to start on anything
invalid under its checks (for example, a missing secret, stub provider or
non-HTTPS callback URL), listing the violations. Passing these checks does not
prove that external services are available or the platform integration works.

Initial staging integration keeps positive enforcement off. The optional
automation rehearsal requires the approvals and synthetic-account isolation in
`docs/PLATFORM_BRIEFING.md` §6; it never changes live permissions. Keep staging
closed to untrusted callers in both stages. Path-bound HMAC v2 is available,
but during the dual-accept window a **v1-only** request is still path-unbound —
a captured signed event could be replayed to another case within the skew
window. The redirect closes for v2
traffic at deploy, but for everyone only once **inbound v1 is actually disabled**
(the zero-witness satisfied AND `hmac_v1_inbound_sunset_at` in effect). Keep
staging's perimeter closed until that day arrives. Deploying v2 is not the
moment it can open. Production automation stays off until every requirement
in `PRODUCTION_READINESS.md` passes.

**Migration 010 is a non-hot cutover.** It drops the global unique that the
old image's ingest still uses, so an old replica serving after the migration
would fail event inserts. Deploy **stop → migrate → start** (not a rolling
upgrade): drain all old API replicas, run `alembic upgrade head`, start the new
replicas, readiness-verify them, then run the one-shot
`python -m kyc_tool.ops.activate_hmac_v1_observation` to start the v1
observation clock. Do NOT skip the activation step — until it runs, the sunset
zero-witness never turns green (by design), so v1 can never be sunset.

## 3. First-time setup (per environment)

1. Provision PostgreSQL 16 (the server version tested in CI), an S3 bucket,
   and ECS/Fargate or EC2 capacity for the services and jobs above.
   Confirm RDS settings and backup/restore procedures in your staging deployment.
2. Generate the shared HMAC secret into AWS Secrets Manager. Set the same
   value in the platform's config for that environment.
3. Set env vars. All carry the `KYC_` prefix except `CH_API_KEY`, the
   Companies House key. `docs/RUNBOOK.md` holds the full table and
   `.env.example` a sample. **A rolling restart does not carry
   every setting.** Changing `KYC_OUTBOX_MAX_ATTEMPTS` in either direction is a
   DRAINED cutover (§8). Where a release note or a setting calls for one, run
   that procedure rather than the default rolling deploy.
   Minimum: `KYC_DATABASE_URL`, `KYC_PLATFORM_CALLBACK_URL`,
   `KYC_OBJECT_STORE=s3`, `KYC_S3_BUCKET`, `CH_API_KEY`, every per-environment
   value from §2 (including the access settings), and the full **HMAC credential
   set**. Production boot refuses without
   all of it:
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

Each IPv4.Global release is a tagged version with release notes stating
whether it includes a migration, new environment variables or a contract change.
Read those notes before scheduling the update.

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
- With a migration: check the release notes for a supported downgrade target
  and the conditions below. Use `alembic downgrade <previous revision>` only
  when every revision in that path permits it for the database's current state.
  These restrictions also apply in staging. Once real traffic has written data
  under the new schema, prefer rolling forward with a fix. Consult IPv4.Global
  before a production rollback. **Migration 010 is
  forward-only after cross-case idempotency-key reuse:** its downgrade
  deliberately refuses because it will not delete immutable audit events to
  recreate the old global unique. See `docs/RUNBOOK.md`. If two cases have
  shared an idempotency key, roll forward with a fix, do not downgrade 010.
  **Exception — migrations 013-023 are forward-only after any wire
  witness exists, positive OR negative** (an `attempt_v1` decision callback with no attempt
  is durable proof nothing was staged, and counts). Their downgrades refuse with stable
  sentinels in execution order. Revisions `018` through `022` refuse UNCONDITIONALLY
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
  schema downgrade. A pre-7b image is permitted only after the entire walk
  reaches 012 — which is only possible on a schema that never reached `018`. Once
  `018` through `022` ARE installed, the supported rollback is redeploying the prior
  reviewed `024`-compatible image against the schema it is already on. The schema
  does not move. Do not apply `018` or anything above it in production until that
  bridge image has been reviewed and
  staged. Use roll-forward recovery before production.
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
  retention) share one advisory fence with the migrations. Writers hold it shared,
  and maintenance holds it exclusively, so maintenance queues behind those writers instead of deadlocking with
  them. **The fence does not cover the pipeline's decide transaction**, which locks the
  case row before inserting the outbox row and takes no fence, a migration run against a
  live pipeline can still deadlock, and the live-claim preflight cannot see a run
  mid-decide because it holds no outbox claim. `022` and `023` sharpen that from "can" to
  "does": they are the first revisions to take `ACCESS EXCLUSIVE` on `decisions` and `cases`,
  in the opposite order from BOTH decision writers: the pipeline's decide transaction and
  the API's inline `reviewer.manual_approve` (jobless: no run, no claim, invisible to any
  job/run drain check). A concurrent decide OR inline approval therefore deadlocks them (`40P01`).
  See `docs/RUNBOOK.md`. That is what the DRAINED cutover is for:
  stop the pipeline workers as well as the publishers.

## 7. Monitoring and incidents

Use `GET /v1/metrics` to alert on:

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
token. Recovery limits:

- A dead `poc_email` row cannot be requeued: its token was scrubbed when it
  died (the endpoint refuses it). Recovery is a fresh `poc.submitted`.
- `recalculate.requested` re-decides from existing evidence but does **not**
  re-run the broker screen — after a blocklist update, re-send the original
  evidence event (or `kyb.run_requested`) instead.
- For a registry outage, inspect the failed source and case reason codes.
  Some runs complete with partial evidence, a failed job may need recovery.
  After the source recovers, send the relevant evidence event to fetch again.
  `recalculate.requested` alone does not refresh the source data.

## 8. Rules

- No code edits on the server, no schema or data edits outside the runbook
  playbooks. The audit trail assumes the repo is the truth.
- Packaged policy changes require a release and version-bump guard. After the
  explicit configuration cutover below, scoring points, broker snapshots, and
  Salesforce destination names instead use audited, server-saved revisions.
  Threshold, hard gates, evidence rules, and positive-decision enforcement are
  not console-editable.
- Secrets only via environment / Secrets Manager, nothing secret is logged.
- Never set `KYC_AUTH_DISABLED` outside a single developer's machine. Production boot refuses
  it. Staging runs as `development` and cannot refuse it, so keep this rule there yourself (§2).
- **ANY change to `KYC_OUTBOX_MAX_ATTEMPTS` (raising OR lowering) is a DRAINED
  publisher cutover, not a rolling restart.** Each publisher enforces the ceiling
  it was started with, so during a rolling restart an OLD and a NEW publisher run
  different ceilings against the same rows:
  - Lowering: the OLD (higher) publisher can make one more send past the new value.
  - Raising: the OLD (lower) publisher can dead-letter a row at its lower ceiling
    before the NEW (higher) publisher supplies the extra attempts. For a
    POC email the terminal transition redacts the token, so those lost retries are
    irreversible.

  So for EITHER direction, in this order: (1) disable autoscaling and rolling
  restart, (2) stop ALL outbox publishers of EVERY role — both the standalone
  `outbox_worker` and the embedded `dev_worker`, (3) attest zero publishers are
  running (the same attested-stop the reset CLI requires), (4) attest every new
  task definition carries the exact new value, (5) start.

KYC_OUTBOX_MAX_ATTEMPTS: both-direction DRAINED publisher cutover (NOT a rolling restart)
1. disable autoscaling and rolling restart
2. stop ALL publishers of roles: outbox_worker, dev_worker
3. attest zero publishers running of roles: outbox_worker, dev_worker
4. attest every new task definition carries KYC_OUTBOX_MAX_ATTEMPTS
5. start publishers of roles: outbox_worker, dev_worker

## 9. Reviewer-actor cutover — brief full maintenance window

This cutover requires the signed
envelope's `actor` to identify the reviewer (`docs/PLATFORM_INTEGRATION.md`
§3), rather than relying on the payload alone. **This is not a rolling deploy.** During any
old/new overlap, an old API applies `reviewer.manual_approve` inline with no actor
floor, and an old pipeline worker (which claims a job purely by kind, with no
event-type filter) can still close a queued `system`-actor website completion
under the old actorless semantics. There is no way to keep an old replica
serving *any* traffic while guaranteeing it never touches a sensitive event,
so use a **brief full maintenance window** with a non-hot
**stop → deploy → start** pattern across the API and workers. This change has
no migration.

The window is a real interruption, not a smooth roll: `POST
/v1/cases/{case_id}/events` is unavailable for its duration, for every event
type, and the pipeline is stopped. The "no loss" guarantee for that
interruption is a **platform prerequisite**, not something the API contract
provides on its own. The contract directs retry on a
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

0. **Before the window:** build and publish the reviewed image, record its
   **digest**. Every code-running step below is pinned to that digest: the
   recovery one-shot, new API and new workers. The recovery
   module (`kyc_tool.ops.requeue_interrupted_jobs`) exists only in the new
   image, so running it on the still-current old task definition fails with
   `No module named …`.
1. **Pause all platform event submission** (every event type, not only the
   two sensitive ones) and block the ops composer: the platform buffers
   outbound events, and the composer route (a `POST` to `/ui/api/send-event`) is
   edge-blocked — or old replicas are flipped to `KYC_UI_ENABLED=false` — so
   an operator on a still-live old replica can't post an inline forged
   approval. The whole window is a maintenance pause, a partial pause cannot
   guarantee no-loss.
2. **Stop all old processes together** — the API pool and the pipeline-worker
   pool, as one coordinated action, **no graceful drain**, without awaiting
   either pool before signaling the other. Confirm both pools are at **zero**
   before continuing. A sequenced stop leaves the not-yet-stopped pool live
   and able to commit a forgery in the gap, the edge block cannot revoke a
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
   **body** as well as the status code:
   - a signed `system`-actor `website.review_completed` against a valid open
     task → the app's **422** (a 404/409 would mask a broken actor floor),
   - a mismatched-actor `reviewer.manual_approve` → the app's **422**,
   - the composer → the app's **403** for both sensitive event types.

   Do not probe the decide-txn guard's live behavior in production this way —
   a real pipeline run there writes a decision and enqueues a callback
   unconditionally, and the outbox publisher (a separate process, not stopped
   in step 2) would deliver it to the platform. That behavior is proven
   **before the window, in staging**, against the exact §0 digest, attest the
   same digest here.
6. **Start the new workers** (they now claim the recovered queue under the
   new decide-txn actor guard), then **resume** — unpause platform event
   submission and unblock the composer route.

### Rollback

Rollback mirrors the same window and pins the recovery one-shot to the **last
image that still contains it**: pause all submission, stop all new processes
together (same coordinated hard stop, same recovery command, run against a
digest that has the module), redeploy the prior image for API **and**
workers, verify, start workers, resume — accepting that the prior image
restores the previous actor-validation behavior.

**Rollback verification is non-mutating only:** `GET` probes of `/readyz`
and `/healthz`, and prior-image digest attestation. Do **not** run the step-5
sensitive-mutation probes against the prior image. It has no actor floor, so a
mismatched-actor `manual_approve`
probe would actually `approve` the case inline, and a `system`-actor
completion probe would queue a run the restored old worker can honor: the
probe would perform the unauthorized mutation it is meant to detect.
Exercise that behavior only in staging or an isolated DB. Keep all submission
and the composer blocked until the safe, non-mutating checks pass.

## 10. Bundle-pinning activation

This cutover pins the policy bundle and records the engine build used to score
and decide a run. It has a **rolling** part and a **drained** part. The flag
flip is not safe for a rolling deployment.

**Rolling migration and provenance, flag stays off.** Migration 011 adds
`policy_bundles`, `bundle_pinning_epoch`, and the nullable provenance columns.
It supports live deployment through the normal §4 flow. Its three provenance
checks land `NOT VALID`, which takes a brief metadata lock and does not scan the
tables. Migration 012 then uses `VALIDATE CONSTRAINT` under a lock that does not
block writers, so the rolling `alembic` upgrade-to-head (the §4 one-shot) does not stall
ingest/decide writers on large `checks`/`decisions` audit tables. On the new
image, the API and pipeline worker seed and read back the
on-disk policy bundle at startup (failing closed on a corrupt persisted row)
and every automatic/manual decision starts recording bundle **and** engine
provenance immediately — `Settings.enforce_bundle_pinning`
(`KYC_ENFORCE_BUNDLE_PINNING`) stays `false`, so scoring itself is
byte-identical to the unpinned behavior. A `GET` on `/readyz`
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
match** stores it, a mismatch raises and writes nothing, so a wrong policy
directory can never land a row silently.

**Drained cutover — flip the flag.** This is not a rolling deploy: every
worker that claims a `run_transition` job while `enforce_bundle_pinning` is
inconsistent across the pool risks resolving a bundle differently from its
peers. Flip it with the pool fully drained, using the same stop/start shape described above:

1. **Preflight.** ``python -m kyc_tool.ops.verify_pinnable_backlog`` — checks
   that every queued/running/dead `run_transition` job's run has a
   creation-pin bundle that actually loads from the store. **A nonzero exit
   blocks the cutover** — seed the missing bundle(s) (§ above, or historical
   recovery via the same command with the older policy directory) and re-run
   until it exits 0.
2. **Disable autoscaling/restarts, confirm zero old workers.** At the
   orchestrator (not by row count — a job-table count doesn't prove process
   quiescence), confirm every pipeline-worker replica currently running
   predates this cutover is gone.
3. **Recover interrupted jobs.** With workers confirmed at zero, run
   ``python -m kyc_tool.ops.requeue_interrupted_jobs`` once — every
   `status='running'` job at this point is by definition interrupted, it
   requeues the whole set without consuming the forced-stop attempt and
   asserts zero `running` rows remain. Its precondition is "all workers
   confirmed stopped" (step 2) — do not run it while any worker is live.
4. **Start flag-on workers.** Set `KYC_ENFORCE_BUNDLE_PINNING=true` and start
   the pipeline-worker pool. Confirm the startup **attestation** log line on
   every replica. The structured `bundle_pinning_ready` event carries
   `flag=true`, `bundle_hash`, and `engine_build_id`, and is emitted only after the
   worker's own seed-and-verify passes, so a replica that never logs it never
   started claiming jobs.
5. **Resume.** Unpause whatever was paused for the drain (the API itself
   never stopped — only the worker pool is drained here).

**Activate the epoch.** Once the cutover is verified stable, write the
durable activation record. Run this only for the first activation, when
no activation row exists. An existing activation epoch is a historical
boundary: do not reset or recreate it to deploy a later engine build.
For a new first activation under this release:

```operator
python -m kyc_tool.ops.activate_bundle_pinning_epoch \
    --expect-bundle-hash <sha256> --expect-engine eng-2
```

This compares the **locally loaded** policy bundle and this process's
`ENGINE_BUILD_ID` against the `--expect-*` arguments before touching the
database at all (a valid-but-wrong bundle is refused here, rather than only by
store-absence later), writes the singleton `bundle_pinning_epoch` row with
database time, and read-back-fails if a concurrent activation already wrote
different values — so a skewed operator clock or a mismatched second
activation can never silently move the boundary. From `activated_at`
onward, `docs/RUNBOOK.md`'s post-epoch alert treats any check/decision
missing its provenance stamp as an anomaly, not an expected state.

**Rollback — flag-only, no data migration.** Normal rollback never
means resuming on an image that predates bundle provenance (that would silently stop writing
provenance and, after the epoch, mint permanent NULLs — a defect, not a
safe fallback). It means disabling the flag **on the provenance-capable image**, via the
same drained shape: stop the worker pool → confirm zero running →
`ops.requeue_interrupted_jobs` → start workers with
`KYC_ENFORCE_BUNDLE_PINNING=false` → resume. The creation pin and
provenance writing are preserved throughout, scoring simply returns to the
process-loaded bundle. Migration 011 is **retained** — never downgrade it
once any bundle row, provenance column, or the epoch row is populated (its
downgrade deliberately refuses, the same forward-only-after-use contract
migration 010 established).

## 11. Callback cutover — drained maintenance window (migration 013)

**Step 0 — pre-window diagnostic (BEFORE any outage):**
0.1 Suspend the retention schedule.
0.2 Terminate and wait for every active retention task.
0.3 Capture target-orchestrator zero-running evidence. `TODO(integration)`: the exact
    zero-running listing must use the `aws` CLI's `ecs list-tasks` scoped to the cluster and the
    retention family, or the EC2 equivalent. Its expected zero-task output MUST be
    recorded here as a typed operator command once the production deployment target is chosen.
    A pytest does not prove this. It is a deployment acceptance check and needs evidence from
    the selected orchestrator.
0.4 With the schedule still suspended, run the digest-pinned
    ``python -m kyc_tool.ops.verify_pr7b_core_backfill``. The result is valid ONLY while retention stays
    suspended AND the 0.3 attestation holds.
0.5 On failure, ABORT here — before stopping service (no outage begun). Recovery is restore-or-block:
    restore from authoritative backup the EXACT callback row, OR remain on 012 in
    `BLOCKED_NO_AUTHORITATIVE_MAPPING`. Backup availability is an operator prerequisite. Activation (`025`) is
    downstream and cannot repair this. Never fabricate a callback, delete a decision, or fall back to
    `decided_at`. On EVERY abort path, explicitly re-enable OR deliberately keep-frozen retention.
    THE RESTORE PATH IS A SHIPPED CLI, reachable from HERE. It uses a pre-window maintenance stop,
    not the cutover, which 0.4 still gates. FIRST run the prerequisites check (read-only, takes NO
    lock) and confirm it is GREEN for the exact schema phase, correct role, `outbox_id_seq`
    ownership, and timeout budgets:

```operator
python -m kyc_tool.ops.verify_pr7b_ops_prerequisites --expect-revision 012
```

A wrong maintenance credential OR wrong phase is caught HERE, not at `ALTER SEQUENCE`
inside the stop, then pause submissions,
hard-stop and attest EVERY writer (API,
pipeline, outbox, `dev_worker`, retention), then run
the restore CLI (dry-run first, add `--apply` to perform):

```operator
python -m kyc_tool.ops.restore_pr7b_core_callback --evidence <file.json> \
    --expect-original-id <id> --expect-manifest-digest <sha256>
```

`--expect-manifest-digest` is MANDATORY and is an INTEGRITY check: the tool recomputes
the sha256 of the evidence file (``sha256sum <file.json>``) and refuses unless it matches, so a
tampered or wrong file is rejected before any DB work — the file cannot self-certify by carrying
its own digest. The tool does NOT verify a cryptographic signature, the digest's authenticity is
yours to establish out of band, from a trusted/signed backup manifest (machine-verified signing
is a future option).
It validates the whole
contract below, inserts the exact original row, floors the sequence past the restored id
(`GREATEST(max(id), original_id) + 1`) in the SAME transaction, and fail-closed read-backs both
the acceptance predicate and the sequence before committing — any mismatch rolls back row and
sequence together. Then rerun 0.4 (the gate that reopens cutover) and either RESUME service or
proceed to the window. Pasting the SQL below by hand is NOT a sanctioned path.
The restore must insert the row and advance the sequence past its original id
in one transaction before the diagnostic can pass.
0.6 RESTORE ACCEPTANCE CONTRACT (the restore in 0.5 is an executable identity requirement, not
    advice — the backfill ranks by `outbox.id`, so a wrong id silently reverses the legacy order):
    (a) BEFORE restoring, record from the backup the authoritative evidence tuple per missing
        callback: `decision_id` plus **every schema-012 `outbox` column**:
        `(id, kind, case_id, run_id, payload_json, status, attempts, next_attempt_at,
        delivered_at, last_error, created_at)`. Compute `body_digest` ON THE BACKUP ROW as
        `encode(sha256(convert_to(payload_json::text,'UTF8')),'hex')`. `md5(...)` is prohibited.
        This procedure runs BEFORE 013, so it must name NO 013-only column: `resolved_at`,
        `ordering_stream`, `decision_sequence` and the claim tuple do not exist yet. Omitting the
        retry/audit columns is what makes "exact" false — a previously retried callback restored
        with a reset `attempts`/`next_attempt_at`/`last_error` is NOT the row that was pruned.
    (b) The restore MUST re-insert the ORIGINAL primary key AND every other recorded column:
        `INSERT INTO outbox (id, kind, case_id, run_id, payload_json, status, attempts,
         next_attempt_at, delivered_at, last_error, created_at) VALUES (<original_outbox_id>, ...)`.
        Use every value from the evidence tuple, with none defaulted. A
        default-id INSERT is prohibited (it allocates a fresh id and re-ranks the restored older
        callback as newer), and substituting `now()` for `delivered_at` is prohibited (it falsifies
        the audit record). If the original id is unavailable, do NOT restore: remain
        `BLOCKED_NO_AUTHORITATIVE_MAPPING` on 012. The evidence tuple is captured into the JSON
        file the restore CLI's `--evidence` input consumes, `--expect-original-id` must repeat
        the id (double entry). The id, body digest and decision linkage are machine-refused on
        mismatch. The lifecycle fields are ATTESTED inputs from the backup, but the MANDATORY
        `--expect-manifest-digest` (sha256 of the whole evidence file, from the signed manifest)
        binds every one of them, so a falsified backup value cannot pass without also breaking the
        signed digest. The signed manifest and the documented capture query are the sanctioned source.
    (c) ACCEPTANCE PREDICATE — POSITIVE and fail-closed. Run per restored callback. It MUST return
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
        first. A `next_id > original_outbox_id` precondition would fail for the documented
        `max=5`/`missing-id=100` case, which is exactly the case the restore exists for.
        `restore_pr7b_core_callback` floors the
        sequence to `GREATEST(max(id), original_id) + 1` in the SAME transaction as the row
        insert, under `ACCESS EXCLUSIVE`, with a fail-closed read-back — whether the missing id is
        below OR above the current high-water. It never `setval`s (a read-modify-write on a
        non-transactional object that can rewind under concurrent `nextval`), `ALTER SEQUENCE …
        RESTART WITH` takes a literal and excludes `nextval` for the transaction. Run it (dry-run,
        then `--apply`) as step 0.5 above — the restore and the sequence floor are ONE action, not
        a check-then-repair sequence.
    (e) ``python -m kyc_tool.ops.repair_outbox_sequence`` is the SEPARATE DRAINED action for the
        ONLY case the restore does not cover: a divergent sequence high-water with NO row to
        restore (nothing missing, the counter itself is wrong). Same maintenance-stop
        preconditions and owner privilege, pass `--floor` with the id when an id above max must stay cleared.
        It is never a prerequisite the restore waits on.
    (f) Only then rerun 0.4 (it must be clean — it also proves existence/1:1 of every mapping).

**Cutover (only after 0.4 is green):**
1. Pause submission, edge-block the composer, disable autoscaling/restarts.
2. Hard-stop API, pipeline, outbox, `dev_worker` (queue AND outbox), retention, and every writer,
   attest zero at the orchestrator.
3. Run the shipped ``python -m kyc_tool.ops.requeue_interrupted_jobs``. NO outbox reset here — the
   pre-013 schema has no claim columns, an interrupted old claim simply waits until its already-
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
R3. While 013 still exists, run ``python -m kyc_tool.ops.reset_interrupted_outbox_claims`` (post-013-only,
    clears complete claim tuples, preserves `next_attempt_at`, atomically read-back-asserts zero) and
    verify zero claim tuples.
R4. **With `018` or anything above it installed there is no schema-downgrade path**: `018` through
    `022` refuse unconditionally — a walk from the head prints
    `MIGRATION_022_DOWNGRADE_REFUSED_FORWARD_ONLY` (`023`'s downgrade is a validation-only
    no-op the walk passes through first, the whole command is ONE transaction, so on refusal
    even that step rolls back and the schema does not move) — because walking below them would restore
    search-path-vulnerable authority functions, so rollback goes straight to R5 (image-only on
    the schema already installed). The walk below is the HISTORICAL path, reachable only on a
    schema that never reached `018`: run ``python -m alembic -c alembic.ini downgrade 012`` (the revision is a
    REQUIRED positional argument — a bare `alembic` downgrade invocation without it exits with a usage error
    mid-outage). That walk is `017 → 016 → 015 → 014 → 013 → 012`, and EACH revision preflights
    under
    `LOCK TABLE ... ACCESS EXCLUSIVE` (child-first from `015` on, `017` first takes the shared
    maintenance/writer advisory fence EXCLUSIVE, so it queues behind live witness writers instead
    of reasoning about their lock order). Sentinels in execution order:
    - `017` refuses with `MIGRATION_017_DOWNGRADE_REFUSED_WITNESS_IN_USE` when ANY attempt row,
      terminal wire digest, or `attempt_v1` decision callback exists. NEGATIVE evidence counts:
      an attempt-regime row with no attempt is the durable proof nothing was staged.
    - `016` refuses with `MIGRATION_016_DOWNGRADE_REFUSED_WITNESS_IN_USE`: same rule one revision
      down (defense in depth below `017`), including the `attempt_v1` negative-evidence case.
    - `015` refuses with `MIGRATION_015_DOWNGRADE_REFUSED_WITNESS_IN_USE` on any attempt row or
      terminal digest (child-first lock order, cannot deadlock a live writer).
    - `014` refuses with `MIGRATION_014_DOWNGRADE_REFUSED_WITNESS_IN_USE`: same witness rule.
    - `013` refuses on a `superseded` row, a surviving terminal digest
      (`MIGRATION_013_DOWNGRADE_REFUSED_WITNESS_IN_USE`), or the attempt table under a bare `013`
      stamp (`MIGRATION_013_DOWNGRADE_REFUSED_AMENDED_HISTORY`).
R5. ROLLBACK OUTCOME A: downgrade REFUSED (any sentinel above). The DB stays on the
    witness-authority schema, so KEEP or redeploy the reviewed **`024`-COMPATIBLE image** digest —
    an older publisher lacks the receipt/terminal contract and MUST NOT run against preserved
    evidence, PROHIBIT the pre-7b image outright. Rollback after first witness use is a
    FLAG/IMAGE rollback on the compatible schema, never a schema downgrade. A pre-7b image is
    permitted ONLY after the entire walk reaches `012` (outcome B). Verify `/readyz`, start + attest its fenced workers, then
    re-enable retention, autoscaling/restarts, and submissions and remove the composer edge block —
    OR remain in a DELIBERATELY DECLARED maintenance incident while the forward fix is applied. Do
    not end stopped.
R6. ROLLBACK OUTCOME B — downgrade SUCCEEDED: deploy the recorded prior-image digest, start API, probe
    `/readyz`, then start + attest its workers, attest image digest + running processes, then re-enable
    retention, autoscaling/restarts, and submissions and remove the composer edge block. Redeploying
    the pre-7b image BEFORE 013 is applied is also safe.

## 12. Live configuration cutover (migration 024)

Installing schema `024` does **not** activate configuration. Core migrations
`013`–`023` and configuration migration `024` are frozen independently. Do not
repair either owner's revisions in place. Platform activation `025` remains
unbuilt and fails closed. This procedure does not enable positive-decision
enforcement or alter callbacks.

1. Record a database backup and the exact digest of the configuration-capable
   release image being deployed. Record that same tested image as the recovery
   image. A pre-configuration image is not a supported rollback after activation.
   Install schema `024` using the normal migration process. The following CLI
   requires at least `024`. It is not a schema-`023` preflight.
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
   unfinished legacy run block activation. Drain them under the old behavior,
   do not relabel them complete, delete them, or invent historical snapshots.
   Review the entire legacy broker baseline: exact stable IDs, names, policies,
   all identifier classes, and notes. Invalid/oversized baselines refuse rather
   than truncate. The CLI also verifies the packaged/stored policy baseline.
4. Block new event admissions and stop all relevant writers: the API and
   composer, pipeline/dev workers, retry/recovery tools, both publishers and
   retention, plus deployment auto-restarts. Attest their stopped state at the orchestrator.
   Rerun the read-only preflight, then apply:

   ```bash
   python -m kyc_tool.ops.activate_live_configuration \
     --apply --attest-writers-stopped --operator-label "maintenance operator"
   ```

   The label is attribution, not verified individual identity. The attestation
   is operator-supplied, not fleet discovery. Apply rechecks under database
   writer fences, stores/verifies the policy, snapshots the full broker list
   including notes, and creates default mappings and the active revision in
   one transaction. Lock/statement budgets are 5/30 seconds, refusal writes
   no baseline. An already-active invocation verifies it and never resets it.
5. Start only the recorded compatible processes with pinning enabled. Require
   `/readyz` and `GET /ui/api/configuration` to verify active authority. Enter
   the admin credential in console Options for this session, read access alone
   is not save authority. Confirm authenticated Save and reload on disposable
   test state before claiming the console is editable. A single process's
   readiness does not attest the whole fleet. Then resume admissions.

Configuration requests are bounded at 32 MiB before decoding, configure the
ingress limit consistently. The local `scripts/devproxy.py` preserves the
browser-facing Host for same-origin checks and rejects oversized/malformed
configuration framing before reading the body. It is a loopback development
proxy, not a production forwarded-header trust policy. Do not restart an existing
demo via `scripts/dev.sh`: its cleanup deletes its temporary database. Preserve
the database and replace only compatible processes in a controlled maintenance
window, never resume a stale destructive watcher.

After activation, recover a prior desired configuration by saving its reviewed
sections as **new revisions**, retaining history. There is no pointer-reset or
rollback CLI. A missing/corrupt active revision is a maintenance incident:
restore verified authority from backup or forward-fix with the recorded compatible
image, do not substitute current packaged values. Downgrade `024` refuses any
recorded configuration history. Completed legacy runs stay explicitly unversioned,
ordinary requeue must not feed unfinished unversioned work to snapshot-only workers.

# Part 4: Operations runbook

## Processes

| Process | Command | Notes |
|---|---|---|
| API | `uvicorn kyc_tool.api.app:create_app --factory` | stateless, scale horizontally |
| Pipeline worker | `python -m kyc_tool.workers.pipeline_worker` | N processes, per-case FIFO is queue-enforced |
| Outbox publisher | `python -m kyc_tool.workers.outbox_worker` | delivers decision callbacks + POC emails |
| Retention | `python -m kyc_tool.workers.retention` | cron (daily), prunes per KYC_RETENTION_DAYS |
| Migrations | `alembic upgrade head` | before rollout, downgrade clean EXCEPT migration 010 and the 013-023 witness chain (see below). **018 through 022 are forward-only: once installed there is NO supported schema downgrade** — rollback is image-only. 017 and 018 both refuse to UPGRADE while any live outbox claim exists (`MIGRATION_017_PREFLIGHT_LIVE_CLAIMS`, `MIGRATION_018_PREFLIGHT_LIVE_CLAIMS` — publishers AND retention must be drained) |
| v1 witness activation | `python -m kyc_tool.ops.activate_hmac_v1_observation` | one-shot, after the migration-010 cutover, idempotent |
| Bundle preflight | `python -m kyc_tool.ops.verify_pinnable_backlog` | one-shot, before the bundle-pinning cutover (`docs/DEPLOYMENT.md` §10) — nonzero exit + the un-pinnable run ids blocks the cutover |
| Bundle seed | `python -m kyc_tool.ops.seed_policy_bundle --expect-hash <sha256>` | one-shot, stores a policy bundle only if it hashes to `--expect-hash` (no write on mismatch) — also the historical-recovery path when reprocessing a run under an older bundle |
| Bundle epoch activation | `python -m kyc_tool.ops.activate_bundle_pinning_epoch --expect-bundle-hash <sha256> --expect-engine <id>` | one-shot, after the bundle-pinning cutover (`docs/DEPLOYMENT.md` §10), idempotent on a matching re-run, fails on a mismatched one |
| 7b-core pre-window diagnostic | `python -m kyc_tool.ops.verify_pr7b_core_backfill` | one shot, compatible with schema 012, takes a SHARE lock and is read only. Run it before the window with retention suspended and zero activity attested. A nonzero exit plus `BLOCKED_NO_AUTHORITATIVE_MAPPING` blocks the cutover (see the cutover section). |
| 7b-core ops prerequisites | `python -m kyc_tool.ops.verify_pr7b_ops_prerequisites --expect-revision 012` | one-shot, READ-ONLY, takes NO lock (no writer stop needed) — run BEFORE pausing service to confirm the maintenance credential: refuses unless the schema is exactly `--expect-revision` (the restore path is `012`), then reports current role, `outbox_id_seq` owner, whether they match, and the lock/statement budgets, nonzero unless the current role OWNS the sequence, so a wrong credential OR wrong phase is caught before the outage, not inside it |
| Outbox claim reset | `python -m kyc_tool.ops.reset_interrupted_outbox_claims` | one-shot, post-013-only, ONLY with every publisher stopped + attested — clears complete claim tuples, preserves `next_attempt_at`, atomic (refuses on any surviving tuple) |
| Outbox sequence repair | `python -m kyc_tool.ops.repair_outbox_sequence [--floor N]` | one-shot, DRAINED maintenance stop only (takes `ACCESS EXCLUSIVE` on outbox), restarts `outbox_id_seq` at `GREATEST(max(id), floor)+1` with a fail-closed read-back — exit status IS the result |
| 7b-core callback restore | `python -m kyc_tool.ops.restore_pr7b_core_callback --evidence <file> --expect-original-id <id> --expect-manifest-digest <sha256> [--apply]` | one shot for schema 012 only, during the maintenance stop before the window. It defaults to dry run. `--expect-manifest-digest` (sha256 of the file, from the signed backup manifest) is MANDATORY. The file cannot certify itself. The command inserts the exact backed-up row, floors the sequence past it in one transaction and performs two fail-closed reads (see cutover step 0.5/0.6). |

> **Migration 010 is a non-hot, forward-only-after-reuse cutover.** It
> drops the global unique on `events.idempotency_key`, which the *old* image's
> ingest still references — deploy **stop/migrate/start**, never rolling. Once
> the tool has admitted the same idempotency key in two different cases, 010's
> downgrade **refuses** (it will not delete immutable audit events to recreate
> the old constraint), roll forward instead. After the new replicas are up and
> readiness-verified, run the activation command above once to start the v1
> observation clock.

> **Migrations 013-023 are forward-only after any wire witness — positive OR
> negative** (an `attempt_v1` decision callback with no attempt is durable proof nothing was
> staged, and counts). **`018` through `022` go further: they refuse downgrade
> unconditionally** (`MIGRATION_022_DOWNGRADE_REFUSED_FORWARD_ONLY`,
> `MIGRATION_021_DOWNGRADE_REFUSED_FORWARD_ONLY`,
> `MIGRATION_020_DOWNGRADE_REFUSED_FORWARD_ONLY`,
> `MIGRATION_019_DOWNGRADE_REFUSED_FORWARD_ONLY`,
> `MIGRATION_018_DOWNGRADE_REFUSED_FORWARD_ONLY`) — walking below them would restore
> search-path-vulnerable or under-validated authority functions, so once `018` is on the schema
> the ONLY rollback is redeploying the prior reviewed **024-compatible** image against it.
> `024` first refuses if any configuration history exists. With unused configuration additions,
> its downgrade removes only those additions, `023` is validation-only and its downgrade is a
> no-op, so the walk reaches `022` and refuses there. Do not apply `018` or anything above it in production until that bridge
> image has been reviewed and staged. Use roll-forward recovery before production. Below
> `018` the walk still preflights with stable sentinels, in execution order
> (`MIGRATION_017_DOWNGRADE_REFUSED_WITNESS_IN_USE`,
> `MIGRATION_016_DOWNGRADE_REFUSED_WITNESS_IN_USE`,
> `MIGRATION_015_DOWNGRADE_REFUSED_WITNESS_IN_USE`,
> `MIGRATION_014_DOWNGRADE_REFUSED_WITNESS_IN_USE`,
> `MIGRATION_013_DOWNGRADE_REFUSED_WITNESS_IN_USE`,
> `MIGRATION_013_DOWNGRADE_REFUSED_AMENDED_HISTORY`) once an attempt row, a
> terminal `callback_wire_sha256`, or a `superseded` row exists — immutable
> delivery evidence is never destroyed because local status looks terminal,
> for a pending/dead callback the attempt row is the only proof bytes were
> staged. On refusal, KEEP or redeploy the reviewed **024-compatible** image — an older
> publisher lacks the receipt/terminal contract and must not run against preserved evidence,
> a pre-7b image is permitted only after the entire walk reaches 012.

> **`022` and `023` need EVERY decision writer drained. This includes the pipeline and API,
> as well as the publishers.** They are the first revisions in the chain to take `ACCESS EXCLUSIVE` on
> `decisions` and `cases`. Two writers take those locks in the opposite order: the pipeline's
> decide transaction (case `FOR UPDATE`, then the `decisions` insert), and the **API process
> itself** — `reviewer.manual_approve` is handled inline in the ingest transaction with the
> same case-lock-then-decision-insert shape, with no job and no run, so a job/run drain check
> cannot see it. Running either migration against either writer **deadlocks** (Postgres
> reports `40P01` and kills one side, reproduced against both a live decide and a live inline
> manual approval, `021` does not do it). This is not silent corruption: DDL is transactional,
> so a killed migration rolls back whole and the schema stays where it was. But it costs the
> window and it can kill the approval instead of the migration, so before applying `022`/`023`
> pause event submission, stop and attest the API writers AND the pipeline workers (as well as
> publishers/retention), then re-run. Unlike the live-claim preflight, this one is **not
> machine-checked**. Migration `025` must provide the machine-checked fence
> before activation.

### Migration refusal sentinels

Every deliberate migration refusal raises a **stable sentinel string**, so a refused
`alembic upgrade`/`downgrade` reads as a designed stop rather than a broken migration.
Grep the sentinel out of the command's output and find it here. The exception
message names the offending rows or objects and a remediation. Older migration
errors may name an older compatible image. Follow this runbook when an error and
the current procedure differ. The image to keep must be compatible with the
**live head**, currently `024`.

Release validation checks that every migration refusal sentinel appears in
this table, so a new refusal cannot ship undocumented.

| Sentinel | Fires when |
|---|---|
| `MIGRATION_014_ATTEMPT_AUTHORITY_MISMATCH` | upgrade: the pre-existing `outbox_delivery_attempts` table being adopted is not the exact expected shape (the amended-013 history means 014 adopts-or-creates) |
| `MIGRATION_017_PREFLIGHT_LIVE_CLAIMS` | upgrade: an unexpired outbox claim exists — stop publishers **and** retention, attest zero old processes, let leases expire or run `reset_interrupted_outbox_claims`, retry |
| `MIGRATION_018_PREFLIGHT_LIVE_CLAIMS` | upgrade: same live-claim preflight as 017 |
| `MIGRATION_018_AUTHORITY_MANIFEST_MISMATCH` | upgrade: the observable authority surface (columns, named constraints, indexes, trigger set, function-body digests) is not the one the prior revision installed — refuses **before** any DDL |
| `MIGRATION_019_AUTHORITY_MANIFEST_MISMATCH` | upgrade: same manifest check, pinned to 018's surface |
| `MIGRATION_020_AUTHORITY_CODE_MISMATCH` | upgrade: an owned function body or `search_path` pin, or an enabled trigger, differs from the canonical code surface |
| `MIGRATION_021_AUTHORITY_CODE_MISMATCH` | upgrade: same code-surface check, pinned to 020's |
| `MIGRATION_021_PREFLIGHT_UNSENDABLE_PENDING_ROWS` | upgrade: `pending` rows carry a redacted body and can never be delivered — the message lists their ids and the `UPDATE … SET status='dead'` that retires them. Arming the guard over them would head-of-line block their streams |
| `MIGRATION_022_AUTHORITY_SURFACE_MISMATCH` | upgrade: 021's exact trigger definitions or origin-enable modes do not validate |
| `MIGRATION_022_MANUAL_POINTER_MISMATCH` | upgrade: a `cases.latest_manual_decision_row_id` does not reference a same-case **manual** decision, so the guard cannot be installed over the data |
| `MIGRATION_023_CROSS_TABLE_AUTHORITY_MISMATCH` | upgrade: a cross-table authority constraint (`fk_outbox_decision_triple`, `fk_cases_latest_decision`, `fk_cases_latest_manual_decision`, their unique targets) or a same-case pointer/outbox row does not validate |
| `MIGRATION_013_DOWNGRADE_REFUSED_AMENDED_HISTORY` | downgrade: 013's recorded history was amended, so its own downgrade cannot be trusted to be the inverse of what ran |
| `MIGRATION_013_DOWNGRADE_REFUSED_WITNESS_IN_USE` | downgrade: wire witness exists (attempt row, terminal `callback_wire_sha256`, or a `superseded` row) |
| `MIGRATION_014_DOWNGRADE_REFUSED_WITNESS_IN_USE` | downgrade: as 013 |
| `MIGRATION_015_DOWNGRADE_REFUSED_WITNESS_IN_USE` | downgrade: as 013 |
| `MIGRATION_016_DOWNGRADE_REFUSED_WITNESS_IN_USE` | downgrade: as 013, and additionally on NEGATIVE evidence (an `attempt_v1` row with no attempt is durable proof nothing was staged) |
| `MIGRATION_017_DOWNGRADE_REFUSED_WITNESS_IN_USE` | downgrade: as 016. This is the outermost witness authority, so a walk from above stops here first. |
| `MIGRATION_018_DOWNGRADE_REFUSED_FORWARD_ONLY` | downgrade: **unconditional** — walking below 018 restores search-path-vulnerable or under-validated authority functions |
| `MIGRATION_019_DOWNGRADE_REFUSED_FORWARD_ONLY` | downgrade: unconditional |
| `MIGRATION_020_DOWNGRADE_REFUSED_FORWARD_ONLY` | downgrade: unconditional |
| `MIGRATION_021_DOWNGRADE_REFUSED_FORWARD_ONLY` | downgrade: unconditional |
| `MIGRATION_022_DOWNGRADE_REFUSED_FORWARD_ONLY` | downgrade: unconditional — reached from head only when 024 has no configuration history, 023's downgrade is a validation-only no-op |
| `MIGRATION_024_CONFIGURATION_DOWNGRADE_REFUSED` | downgrade: any configuration revision, request, active pointer, or versioned run exists, restore a prior configuration through a new reviewed section save, never delete its history |
| `MIGRATION_024_CONFIGURATION_DOWNGRADE_BUSY` | downgrade: a configuration-authority lock is busy, acquisition uses NOWAIT so it cannot deadlock a concurrent writer. Quiesce writers before retrying, existing configuration history still requires roll-forward recovery |

> **`enforce_bundle_pinning` is a drained, not rolling, flag flip.**
> Off (default), every worker scores under its own process-loaded policy
> bundle — today's behavior, unchanged. On, a worker resolves and scores each
> run under **that run's creation-pin bundle** (`runs.policy_bundle_hash`)
> loaded from the durable `policy_bundles` store, and refuses the job
> (dead-letter, zero side effects — no adapter call, check, decision, token,
> or outbox row) rather than silently falling back if that bundle can't be
> loaded. An inconsistent flag value across the worker pool risks different
> workers resolving different rubrics for the same run — flip it only via
> the drained cutover in `docs/DEPLOYMENT.md` §10, preflighted by the bundle
> preflight command above.

## Production configuration (startup kill switches)

Every process (`api`, `pipeline_worker`, `outbox_worker`) refuses to boot when
`KYC_ENVIRONMENT=production` and any of these is unsafe — `ProductionConfigError`
lists **all** violations at once:

| Variable | Production requirement |
|---|---|
| `KYC_AUTH_DISABLED` | `false` |
| `KYC_PLATFORM_HMAC_SECRET` | ≥ 32 chars (v1 legacy secret) |
| `KYC_HMAC_INBOUND_KEY_ID` / `KYC_HMAC_INBOUND_SECRET` | non-empty / ≥ 32 chars (v2 inbound) |
| `KYC_HMAC_OUTBOUND_KEY_ID` / `KYC_HMAC_OUTBOUND_SECRET` | non-empty / ≥ 32 chars (v2 callbacks) |
| `KYC_HMAC_V1_INBOUND_SUNSET_AT` / `KYC_HMAC_V1_OUTBOUND_SUNSET_AT` | tz-aware ISO-8601, both required (naive/malformed refused at boot) |
| `KYC_HMAC_V1_OBSERVATION_WINDOW_DAYS` | ≥ 1 |
| `KYC_PLATFORM_CALLBACK_URL` | HTTPS, not localhost |
| `KYC_OBJECT_STORE` / `KYC_S3_BUCKET` | `s3` / non-empty |
| `KYC_OCR_ENGINE` | not the `json_scan` dev stub — required today regardless of the open document-extraction decision (`docs/PLATFORM_INTEGRATION.md` §6) |
| `KYC_EMAIL_PROVIDER` | not the `logging` dev stub |
| `KYC_ADAPTERS_PROFILE` | not the `fixture` stub |
| `KYC_FLOQER_API_KEY` | non-empty when `KYC_ADAPTERS_PROFILE` is not `fixture`, secret manager or `.env` only — never a task definition, a log, or a case snapshot |
| `KYC_FLOQER_SHORTCUT_ID` | non-empty when `KYC_ADAPTERS_PROFILE` is not `fixture`, the id of the ONE published shortcut the tool runs |
| `KYC_READ_AUTH_REQUIRED` | `true` (read API requires a signed request) |
| `KYC_UI_ADMIN_TOKEN` | required, not blank: it authenticates the always-mounted `/v1/ops` requeue endpoints and, when `KYC_UI_ENABLED=true`, the console |
| `KYC_OUTBOX_LEASE_SECONDS` | must EXCEED `4 × KYC_OUTBOX_HTTP_TIMEOUT_SECONDS + KYC_OUTBOX_LEASE_MARGIN_SECONDS` — the publisher enforces 4 × timeout as a hard per-attempt deadline, and a lease that expires mid-attempt makes every delivery unwitnessable |
| `KYC_OUTBOX_HTTP_TIMEOUT_SECONDS` | per HTTPX **inactivity** phase (not a total clock), 4 × this is the enforced whole-attempt deadline. Raising it raises the required lease FOUR-fold — move the two together or production refuses to boot |
| `KYC_OUTBOX_LEASE_MARGIN_SECONDS` | DB commit/processing room added to the deadline in the lease rule above |
| `KYC_OUTBOX_MAX_ATTEMPTS` | delivery-attempt ceiling (1 ≤ n ≤ int4 max). **It is not hot-swappable. ANY change, raise OR lower, is a DRAINED publisher cutover, never a rolling restart.** Each publisher enforces the ceiling it started with. Overlapping old/new publishers either send once past a lowered value or dead-letter before a raised value takes effect. A POC dead-letter also irreversibly redacts its token. Cutover, in order: disable autoscaling/rolling restart → stop ALL outbox publishers of every role (`outbox_worker` AND `dev_worker`) → attest zero running → attest every new task definition carries the exact new value → start. Canonical record: `kyc_tool.ops.cutover.OUTBOX_MAX_ATTEMPTS_CUTOVER` (DEPLOYMENT §8) |

Changing `KYC_OUTBOX_MAX_ATTEMPTS` in either direction requires a drained
publisher cutover (DEPLOYMENT §8):

KYC_OUTBOX_MAX_ATTEMPTS: both-direction DRAINED publisher cutover (NOT a rolling restart)
1. disable autoscaling and rolling restart
2. stop ALL publishers of roles: outbox_worker, dev_worker
3. attest zero publishers running of roles: outbox_worker, dev_worker
4. attest every new task definition carries KYC_OUTBOX_MAX_ATTEMPTS
5. start publishers of roles: outbox_worker, dev_worker

Real OCR, email, and adapter providers are not implemented. A production worker
cannot start until they exist; production startup fails closed.

> **Positive-decision enforcement.** `KYC_ENFORCE_POSITIVE_DECISIONS` defaults
> to `false`: a computed `approve` /
> `approve_buy_locked` is emitted as `manual_review_insufficient` (the callback
> carries an `enforcement_held` object with the computed decision, the audit
> trail records both). Keep it off in production until every requirement in
> `PRODUCTION_READINESS.md` passes, including the full production backlog,
> real-provider staging tests and platform cutover approval. Validator
> hardening alone is not sufficient.

## Ops console (`/ui`)

The console provides the main runbook operations. Overview shows health and
dead-letter work. Cases is the reviewer's working view. It puts the registration
and contact details beside two deliberately separate readings: the decision
already recorded for the case and the score of the evidence held now. New
evidence can change the latter. It never rewrites an earlier decision. The next
verification round appends another decision to the case history.

On a case, a reviewer can record that the contact was asked for a missing RIR
Org ID. That button records the request and who made it. It does **not** send an
email. The platform owns the contact message. If the contact supplies a handle
through another channel, **Record handle** saves it under the reviewer's name
and queues the case to be scored again. An open website task can be approved or
rejected only after an inline confirmation. The result and reviewer ID become
part of the permanent case record. **Approve manually** also records the named
reviewer and reason as a new decision without altering historical decisions.

Data Sources reports adapter mode,
configuration and reachability. Salesforce Fields previews the current case
projection. Decision Rules shows the active scoring and gate configuration.
Options holds appearance and operator access. Case Actions prepares signed
events for review before they are sent server-side. Treat an unsent Case Actions
entry as browser-page working state, not a durable record. The five-second
refresh waits while a person is typing, has armed a confirmation, or has a case
form open. Navigation still re-renders the page, so unsent case edits can be
lost when the reviewer leaves it.

Data Sources may hide a local evidence-storage path. Check
`KYC_OBJECT_STORE_ROOT` in the deployment configuration for the actual location.
The email status distinguishes log-only testing from the closed-staging file
sink; neither sends an email to the contact.

Options saves the System, Light or Dark theme in that browser. The admin
credential is kept only in memory for the current page session, is checked by
the server on every protected action, and is cleared by a reload. **Security**:
**off by default**
(`KYC_UI_ENABLED=false`), when enabled in production it requires
`KYC_UI_ADMIN_TOKEN`, and every mutating endpoint (composer, requeue, probe)
demands `Authorization: Bearer <token>`. Local dev: `bash scripts/dev.sh`
boots the whole stack and prints the console URL.

**Composer production prohibition.** In production
(`KYC_ENVIRONMENT=production`) the composer endpoint (`POST
/ui/api/send-event`) refuses `website.review_completed` and
`reviewer.manual_approve` with **403** — real reviewer actions must arrive as
signed platform events carrying a genuine reviewer actor (see
`docs/PLATFORM_INTEGRATION.md` §3, "Reviewer actor requirement"), not be
typed into the console by an operator. This server-side 403 is the
security boundary. Hiding the composer's controls for these two event types
in the console UI is not a substitute. In
dev/staging the composer still sends both event types, but with a real
`{"type": "reviewer", "id": <reviewer_id>}` actor instead of the generic
`system`/`ops-console` actor it uses for everything else, so console testing
exercises the same binding production enforces rather than bypassing it.

## Health & dashboards

- `GET /healthz` — liveness + the policy bundle hash. **A hash change without a
  deploy is an incident** (policy files are immutable per release).
- `GET /readyz` checks config validity in production, DB connectivity,
  migration head match and S3 access. It also checks **unconditionally, regardless of
  `enforce_bundle_pinning`**, that this process's own loaded policy bundle
  is durably resolvable from the `policy_bundles` store. `503` on any
  failing check, including a missing/corrupt bundle row, wire it to the load
  balancer so a mis-migrated, misconfigured, or un-seeded instance drains.
- `GET /v1/metrics` — watch: `jobs_by_status.dead` (alert > 0),
  `outbox_by_status.dead` (alert > 0), `runs_by_state.FAILED`,
  `review_tasks_open_by_type` growth, `event_to_decision_seconds.p95`
  (budget: < 10s adapter-light, < 120s full runs, human waits excluded),
  `adapter_latency[].error_rate` per upstream.

## Failure playbooks

### Dead-lettered job (`jobs.status = 'dead'`)
The run is FAILED with the error recorded, the case is untouched (no partial
writes because transitions are transactional). After fixing the cause, requeue through
`POST /v1/ops/requeue/job/{id}` with `Authorization: Bearer
<KYC_UI_ADMIN_TOKEN>`. This ops endpoint is always mounted, and the console
button calls the same service. It resets both the job and its FAILED run atomically. Hand-written SQL is NOT a
sanctioned path: the run reset is load-bearing (a requeued job whose run
is still FAILED completes immediately without doing anything), and a hand
UPDATE skips the endpoint's audit record. `recalculate.requested` also produces a
fresh decision from current live checks, but it does **not** re-run the broker
screen — after a blocklist update, re-send the original evidence event instead.

**Died with `BundleUnavailable` and `enforce_bundle_pinning=true`?** The
run's creation-pin bundle (`runs.policy_bundle_hash`) isn't in the
`policy_bundles` store. **Requeuing without seeding it first just fails
again the same way.** To reprocess the run, you must first seed that exact
historical bundle: run `ops.seed_policy_bundle --expect-hash
<runs.policy_bundle_hash>` pointed at a `KYC_POLICY_DIR` containing those
exact files (the command computes-then-compares-then-stores, so a
mismatched directory writes nothing) — only then does the requeue above
succeed.

### Dead outbox row (callback undeliverable)
The run sits in PUBLISH_DECISION (visible, correct). Confirm the platform
endpoint + HMAC secret, then requeue via the ops endpoint ONLY
(`POST /v1/ops/requeue/outbox/{id}`, `Authorization: Bearer <KYC_UI_ADMIN_TOKEN>`,
ALWAYS mounted — available with the console disabled, the console button calls
the same service). A hand
`UPDATE outbox ...` is NOT a sanctioned path: after migration 013 it would
leave the claim tuple untouched and bypass the 018+ transition-authority
validation, stranding or corrupting the row's delivery accounting.
Redelivery of a decision callback is safe — the platform dedupes on
(case_id, run_id). **A row whose body has been REDACTED is the exception, for
either kind** (migration 020+): a POC email's payload is scrubbed the moment it
dies, because the raw token is never retained, and a decision callback's is
scrubbed by retention once past `KYC_RETENTION_DAYS`. Either way there is
nothing deliverable left, so the console endpoint returns 409 — and any hand
`UPDATE ... SET status='pending'` is refused by the database with *"a redacted
outbox row can never be MADE sendable again"*. Recovery is a fresh `poc.submitted` (which cancels old tokens
and sends a new email) or, for a callback, `recalculate.requested`, which
produces a NEW decision under a new `run_id` — it does not restore the old
body. A pending row that somehow already carries a redacted body is unsendable
and should be retired, not requeued:
```sql
UPDATE outbox SET status='dead', last_error='body redacted; unsendable'
WHERE status='pending' AND payload_json = '{"redacted": true}'::jsonb;
```

### RIR / registry outage
Runs complete as `partial` (upstream_error recorded, prior checks stay live,
no failing check is invented — G12 semantics). No action needed, when the
upstream recovers, re-drive affected cases by re-sending the original
evidence event (fresh idempotency keys). `recalculate.requested` re-decides
without re-fetching and without the broker screen — use it only when no new
evidence or blocklist change is in play.

### Review queue growing
`GET /v1/review-tasks?status=open`. Website tasks award +10 on pass,
`poc_email_unavailable` needs an alternate-proof decision by compliance
(v1: resolve on the platform, the tool records outcomes via events).

### Latency
Adapter p95 in `/v1/metrics`, per-upstream rate caps via
`KYC_ADAPTER_RATE_LIMITS` (requests/sec, process-local — divide by worker
count). Queue depth is `jobs_by_status.queued`, scale pipeline workers
horizontally (SKIP LOCKED makes them safe, per-case processing order is
preserved. Callback delivery order is a separate contract,
`docs/PLATFORM_INTEGRATION.md` §4).

**Floqer.** The documented limits are 200 requests/minute and 10,000/day per
key. One case costs one run request plus its polls, about 12, and 1.6-9.6 credits.
The 180 s client deadline caps requests near 45. Set
`KYC_ADAPTER_RATE_LIMITS={"floqer_company_enrichment": <n>}` per worker so the
whole fleet stays under 200/minute, and remember credits and requests are
separate budgets. A run ending `outOfCredits` is a billing stop, not a fault to
retry: top the account up, do not re-drive the cases.

## Callback cutover — drained maintenance window (migration 013)

**Step 0 — pre-window diagnostic (BEFORE any outage):**
0.1 Suspend the retention schedule.
0.2 Terminate and wait for every active retention task.
0.3 Capture target-orchestrator zero-running evidence. `TODO(integration)`: the exact
    zero-running listing must use the `aws` CLI's `ecs list-tasks` scoped to the cluster and the
    retention family, or the EC2 equivalent. Its expected zero-task output MUST be
    recorded here as a typed operator command once the production deployment target is chosen.
    A pytest does not prove this. It is a deployment acceptance check and needs evidence from
    the selected orchestrator.
0.4 With the schedule still suspended, run the digest-pinned
    ``python -m kyc_tool.ops.verify_pr7b_core_backfill``. The result is valid ONLY while retention stays
    suspended AND the 0.3 attestation holds.
0.5 On failure, ABORT here — before stopping service (no outage begun). Recovery is restore-or-block:
    restore from authoritative backup the EXACT callback row, OR remain on 012 in
    `BLOCKED_NO_AUTHORITATIVE_MAPPING`. Backup availability is an operator prerequisite. Activation (`025`) is
    downstream and cannot repair this. Never fabricate a callback, delete a decision, or fall back to
    `decided_at`. On EVERY abort path, explicitly re-enable OR deliberately keep-frozen retention.
    THE RESTORE PATH IS A SHIPPED CLI, reachable from HERE. It uses a pre-window maintenance stop,
    not the cutover, which 0.4 still gates. FIRST run the prerequisites check (read-only, takes NO
    lock) and confirm it is GREEN for the exact schema phase, correct role, `outbox_id_seq`
    ownership, and timeout budgets:

```operator
python -m kyc_tool.ops.verify_pr7b_ops_prerequisites --expect-revision 012
```

A wrong maintenance credential OR wrong phase is caught HERE, not at `ALTER SEQUENCE`
inside the stop, then pause submissions,
hard-stop and attest EVERY writer (API,
pipeline, outbox, `dev_worker`, retention), then run
the restore CLI (dry-run first, add `--apply` to perform):

```operator
python -m kyc_tool.ops.restore_pr7b_core_callback --evidence <file.json> \
    --expect-original-id <id> --expect-manifest-digest <sha256>
```

`--expect-manifest-digest` is MANDATORY and is an INTEGRITY check: the tool recomputes
the sha256 of the evidence file (``sha256sum <file.json>``) and refuses unless it matches, so a
tampered or wrong file is rejected before any DB work — the file cannot self-certify by carrying
its own digest. The tool does NOT verify a cryptographic signature, the digest's authenticity is
yours to establish out of band, from a trusted/signed backup manifest (machine-verified signing
is a future option).
It validates the whole
contract below, inserts the exact original row, floors the sequence past the restored id
(`GREATEST(max(id), original_id) + 1`) in the SAME transaction, and fail-closed read-backs both
the acceptance predicate and the sequence before committing — any mismatch rolls back row and
sequence together. Then rerun 0.4 (the gate that reopens cutover) and either RESUME service or
proceed to the window. Pasting the SQL below by hand is NOT a sanctioned path.
The restore must insert the row and advance the sequence past its original id
in one transaction before the diagnostic can pass.
0.6 RESTORE ACCEPTANCE CONTRACT (the restore in 0.5 is an executable identity requirement, not
    advice — the backfill ranks by `outbox.id`, so a wrong id silently reverses the legacy order):
    (a) BEFORE restoring, record from the backup the authoritative evidence tuple per missing
        callback: `decision_id` plus **every schema-012 `outbox` column**:
        `(id, kind, case_id, run_id, payload_json, status, attempts, next_attempt_at,
        delivered_at, last_error, created_at)`. Compute `body_digest` ON THE BACKUP ROW as
        `encode(sha256(convert_to(payload_json::text,'UTF8')),'hex')`. `md5(...)` is prohibited.
        This procedure runs BEFORE 013, so it must name NO 013-only column: `resolved_at`,
        `ordering_stream`, `decision_sequence` and the claim tuple do not exist yet. Omitting the
        retry/audit columns is what makes "exact" false — a previously retried callback restored
        with a reset `attempts`/`next_attempt_at`/`last_error` is NOT the row that was pruned.
    (b) The restore MUST re-insert the ORIGINAL primary key AND every other recorded column:
        `INSERT INTO outbox (id, kind, case_id, run_id, payload_json, status, attempts,
         next_attempt_at, delivered_at, last_error, created_at) VALUES (<original_outbox_id>, ...)`.
        Use every value from the evidence tuple, with none defaulted. A
        default-id INSERT is prohibited (it allocates a fresh id and re-ranks the restored older
        callback as newer), and substituting `now()` for `delivered_at` is prohibited (it falsifies
        the audit record). If the original id is unavailable, do NOT restore: remain
        `BLOCKED_NO_AUTHORITATIVE_MAPPING` on 012. The evidence tuple is captured into the JSON
        file the restore CLI's `--evidence` input consumes, `--expect-original-id` must repeat
        the id (double entry). The id, body digest and decision linkage are machine-refused on
        mismatch. The lifecycle fields are ATTESTED inputs from the backup, but the MANDATORY
        `--expect-manifest-digest` (sha256 of the whole evidence file, from the signed manifest)
        binds every one of them, so a falsified backup value cannot pass without also breaking the
        signed digest. The signed manifest and the documented capture query are the sanctioned source.
    (c) ACCEPTANCE PREDICATE — POSITIVE and fail-closed. Run per restored callback. It MUST return
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
        first. A `next_id > original_outbox_id` precondition would fail for the documented
        `max=5`/`missing-id=100` case, which is exactly the case the restore exists for.
        `restore_pr7b_core_callback` floors the
        sequence to `GREATEST(max(id), original_id) + 1` in the SAME transaction as the row
        insert, under `ACCESS EXCLUSIVE`, with a fail-closed read-back — whether the missing id is
        below OR above the current high-water. It never `setval`s (a read-modify-write on a
        non-transactional object that can rewind under concurrent `nextval`), `ALTER SEQUENCE …
        RESTART WITH` takes a literal and excludes `nextval` for the transaction. Run it (dry-run,
        then `--apply`) as step 0.5 above — the restore and the sequence floor are ONE action, not
        a check-then-repair sequence.
    (e) ``python -m kyc_tool.ops.repair_outbox_sequence`` is the SEPARATE DRAINED action for the
        ONLY case the restore does not cover: a divergent sequence high-water with NO row to
        restore (nothing missing, the counter itself is wrong). Same maintenance-stop
        preconditions and owner privilege, pass `--floor` with the id when an id above max must stay cleared.
        It is never a prerequisite the restore waits on.
    (f) Only then rerun 0.4 (it must be clean — it also proves existence/1:1 of every mapping).

**Cutover (only after 0.4 is green):**
1. Pause submission, edge-block the composer, disable autoscaling/restarts.
2. Hard-stop API, pipeline, outbox, `dev_worker` (queue AND outbox), retention, and every writer,
   attest zero at the orchestrator.
3. Run the shipped ``python -m kyc_tool.ops.requeue_interrupted_jobs``. NO outbox reset here — the
   pre-013 schema has no claim columns, an interrupted old claim simply waits until its already-
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
R3. While 013 still exists, run ``python -m kyc_tool.ops.reset_interrupted_outbox_claims`` (post-013-only,
    clears complete claim tuples, preserves `next_attempt_at`, atomically read-back-asserts zero) and
    verify zero claim tuples.
R4. **With `018` or anything above it installed there is no schema-downgrade path**: `018` through
    `022` refuse unconditionally — a walk from the head prints
    `MIGRATION_022_DOWNGRADE_REFUSED_FORWARD_ONLY` (`023`'s downgrade is a validation-only
    no-op the walk passes through first, the whole command is ONE transaction, so on refusal
    even that step rolls back and the schema does not move) — because walking below them would restore
    search-path-vulnerable authority functions, so rollback goes straight to R5 (image-only on
    the schema already installed). The walk below is the HISTORICAL path, reachable only on a
    schema that never reached `018`: run ``python -m alembic -c alembic.ini downgrade 012`` (the revision is a
    REQUIRED positional argument — a bare `alembic` downgrade invocation without it exits with a usage error
    mid-outage). That walk is `017 → 016 → 015 → 014 → 013 → 012`, and EACH revision preflights
    under
    `LOCK TABLE ... ACCESS EXCLUSIVE` (child-first from `015` on, `017` first takes the shared
    maintenance/writer advisory fence EXCLUSIVE, so it queues behind live witness writers instead
    of reasoning about their lock order). Sentinels in execution order:
    - `017` refuses with `MIGRATION_017_DOWNGRADE_REFUSED_WITNESS_IN_USE` when ANY attempt row,
      terminal wire digest, or `attempt_v1` decision callback exists. NEGATIVE evidence counts:
      an attempt-regime row with no attempt is the durable proof nothing was staged.
    - `016` refuses with `MIGRATION_016_DOWNGRADE_REFUSED_WITNESS_IN_USE`: same rule one revision
      down (defense in depth below `017`), including the `attempt_v1` negative-evidence case.
    - `015` refuses with `MIGRATION_015_DOWNGRADE_REFUSED_WITNESS_IN_USE` on any attempt row or
      terminal digest (child-first lock order, cannot deadlock a live writer).
    - `014` refuses with `MIGRATION_014_DOWNGRADE_REFUSED_WITNESS_IN_USE`: same witness rule.
    - `013` refuses on a `superseded` row, a surviving terminal digest
      (`MIGRATION_013_DOWNGRADE_REFUSED_WITNESS_IN_USE`), or the attempt table under a bare `013`
      stamp (`MIGRATION_013_DOWNGRADE_REFUSED_AMENDED_HISTORY`).
R5. ROLLBACK OUTCOME A: downgrade REFUSED (any sentinel above). The DB stays on the
    witness-authority schema, so KEEP or redeploy the reviewed **`024`-COMPATIBLE image** digest —
    an older publisher lacks the receipt/terminal contract and MUST NOT run against preserved
    evidence, PROHIBIT the pre-7b image outright. Rollback after first witness use is a
    FLAG/IMAGE rollback on the compatible schema, never a schema downgrade. A pre-7b image is
    permitted ONLY after the entire walk reaches `012` (outcome B). Verify `/readyz`, start + attest its fenced workers, then
    re-enable retention, autoscaling/restarts, and submissions and remove the composer edge block —
    OR remain in a DELIBERATELY DECLARED maintenance incident while the forward fix is applied. Do
    not end stopped.
R6. ROLLBACK OUTCOME B — downgrade SUCCEEDED: deploy the recorded prior-image digest, start API, probe
    `/readyz`, then start + attest its workers, attest image digest + running processes, then re-enable
    retention, autoscaling/restarts, and submissions and remove the composer edge block. Redeploying
    the pre-7b image BEFORE 013 is applied is also safe.

## Retention & compliance

`workers.retention` prunes audit rows, delivered **`poc_email`** outbox rows,
and expired unverified POC tokens past `KYC_RETENTION_DAYS` (default 7y).
Checks, decisions, and raw evidence are NOT auto-pruned — deleting the
decision record requires compliance sign-off, do it as a supervised one-off.

**`decision_callback` outbox rows are not pruned, but their
bodies are destroyed past the window.** The row is the durable ordering
authority the platform reconciliation is built from. Its `id` records order and
`status` records the local delivery outcome, so the row is kept indefinitely. `payload_json`, which
carries `checks[].source` (can be reviewer-derived), is a different matter:
`workers.retention` redacts it to `{"redacted": true}` once
`COALESCE(delivered_at, resolved_at, created_at)` is past
`KYC_RETENTION_DAYS`, for any row whose `status` is `delivered`, `superseded`
**or `dead`** (migration 020 — a callback that exhausted its attempts during a
platform outage carries the same reviewer-derived body as any other, and
keeping it forever inverted this policy, a dead row has neither `delivered_at`
nor `resolved_at`, so it ages on `created_at`). Redaction **never** touches a
`pending` row: that body is still sendable, and the database refuses the write.
A redacted row can no longer be requeued. The console returns 409 and names
the remedy per kind. This is the intended trade: the body is destroyed on
schedule, and nothing is left able to transmit a `{"redacted": true}` payload
to the platform.

What survives redaction is pseudonymous, not out of scope: `case_id`, `run_id`,
`decision_sequence`, `status`, `delivered_at`/`resolved_at`, and the recorded
wire digest (`callback_wire_sha256`, `wire_version`). The surviving data is a
hash plus internal ordinals, still joinable back to a case. Retaining that
remainder past the window is a governed retention decision, not a claim that
it falls outside backup, erasure, privacy, or compliance review. Accountability
for it is the **deployer's data
controller's** — this repo cannot name that owner and does not discharge the
controller's obligations. It only bounds what it keeps and documents the
bound. Two things this repo does NOT reach: (1) an erasure request landing
**inside** the window must still reach the live callback snapshot (its body is
not yet redacted) as well as the decision record, (2) **backups taken before
redaction** are a separate durable copy and still contain the pre-redaction
body until they age out on the backup retention schedule, independent of
`KYC_RETENTION_DAYS`.

## Policy changes

After the explicit migration-024 activation in `docs/DEPLOYMENT.md` §12,
authorized console saves publish shared, durable revisions of scoring points,
the complete Allowed/Blocked broker list, and Salesforce destination names.
Points must be exact integers 0–1000. Broker limits are 200 entries, 1–200
characters per name, 100 entries per identifier class, 1–256 characters per
identifier, and at most 2000 characters of notes, Blocked wins on exact matches.
Allowed never bypasses another gate. New review runs, including new runs for
existing companies, use the saved revision. Existing runs and event replays keep
their creation revision, Save does not recalculate, rewrite evidence/decisions,
send events, write Salesforce, or enable automatic positive enforcement.

Saving requires a nonempty configured admin credential in **every environment**,
including development, and same-origin browser requests. A shared token proves
the authentication mechanism, not the operator label's personal identity.
Conflicts preserve the draft: reload/review the latest revision before resaving.
An uncertain result is not cancellation: keep the exact payload/request ID for
identical retry and check the returned current revision. Cancel discards only
unsent edits. Never automatically publish old browser-local previews.

Recover prior settings through new reviewed revisions, never by editing history
or resetting the active pointer. After activation `broker_entities` is only the
legacy/bootstrap source, not a second live editing interface. If active authority
is missing/corrupt, new admissions/saves fail closed and the console company
`/full` response can return 503 even when an old run snapshot is intact. For
read-only historical inspection use `GET /v1/cases/{case_id}` and
`GET /v1/cases/{case_id}/checks` under their existing read authorization.

Threshold, hard gates, evidence rules, identity invalidation, manual record-only
semantics, callback schema, and positive-decision enforcement remain unchanged. Changes to non-editable
packaged policy still ship as a deploy: bump `version`, update
`tests/policy_driven/policy_baseline.json` in the same release, and redeploy.
Every run and decision records the policy hash that produced it. Editable
point values must be whole numbers from 0 to 1000.

## Policy bundle pinning and provenance

Full cutover procedure: `docs/DEPLOYMENT.md` §10. Summary of the ongoing
operator surface once it's live:

- **The flag.** `enforce_bundle_pinning` (default off) — see the callout
  under Processes above. Flip it only via a drained cutover, never a rolling
  toggle.
- **`ops.verify_pinnable_backlog`** — preflight before enabling the flag,
  prints the run ids whose creation-pin bundle wouldn't resolve and exits
  nonzero if any exist.
- **`ops.seed_policy_bundle --expect-hash <sha256>`** — the only way to add
  a bundle to the durable store by hand (compute → compare to
  `--expect-hash` → store, a mismatch writes nothing). Every process also
  self-seeds its own on-disk bundle at startup, so day-to-day this command
  is for the preflight gap case above and for **historical recovery** — see
  the reprocess note below.
- **`GET /readyz`** — 503 when the process's own policy bundle isn't in the
  store (unconditional, both flag states).
- **Post-epoch NULL-provenance check.** Once
  `ops.activate_bundle_pinning_epoch` has run, any `checks` row with `NULL
  policy_bundle_hash`, or any `decisions`/`runs` row with `NULL
  engine_build_id`, created **after** `bundle_pinning_epoch.activated_at` is
  an anomaly — a rollover gap or a bypassed write path, never an expected
  state (a legitimately still-queued run is not flagged). The check is
  `kyc_tool.ops.activate_bundle_pinning_epoch.post_epoch_null_provenance(session)`
  — it returns `{"decisions": [...], "checks": [...], "runs": [...]}` of the
  offending ids. No `/v1/metrics` or CLI integration is available; run it
  from a Python shell against the production DB or integrate it with the
  deployment's alerting.
- **To reprocess a run, you must first seed its bundle.** Whether via
  `enforce_bundle_pinning` at the time (a live `BundleUnavailable`
  dead-letter — see the failure playbook above) or later, historical
  reprocessing under a run's original rubric requires that exact bundle to
  be in `policy_bundles` first, `ops.seed_policy_bundle --expect-hash
  <hash>` against the matching historical policy directory is the only
  supported path to add it back.

## Secrets

All via environment (see `.env.example`): platform HMAC, CH/ARIN keys,
email provider, S3. Nothing is ever logged, POC token emails log only a
recipient hash.

> **Upgrade preflight (`017`, `018`).** Both refuse with
> `MIGRATION_017_PREFLIGHT_LIVE_CLAIMS` / `MIGRATION_018_PREFLIGHT_LIVE_CLAIMS`
> while any unexpired outbox claim exists — the
> database checks live outbox claims, but it cannot prove that no API, publisher, pipeline,
> dev worker, or retention process is still running. Stop those processes, attest zero old
> processes at the orchestrator, then either let the leases expire
> (`KYC_OUTBOX_LEASE_SECONDS` bounds how long that takes) or run the shipped
> `python -m kyc_tool.ops.reset_interrupted_outbox_claims` (post-013-only, clears complete
> claim tuples, preserves `next_attempt_at`, atomically read-back-asserts zero — it refuses
> rather than partially resetting if any tuple survives), then retry. It MUST NOT run while
> any publisher is live. `018` and `019` additionally refuse with
> `MIGRATION_018_AUTHORITY_MANIFEST_MISMATCH` / `MIGRATION_019_AUTHORITY_MANIFEST_MISMATCH`
> when the
> observable authority surface (columns, constraints, indexes, trigger definitions, authority
> function bodies, pinned `search_path`) is not the one `017` installed — reconcile the
> environment, or restore from the reviewed image, before retrying. Every refusal happens BEFORE
> any DDL: the schema and the alembic head are left exactly as they were.

# Part 5: Alert reference

Scrape `GET /v1/metrics.prom` (signed read access — same auth as `/v1/metrics`; see the scrape-identity
note below). Latency series aggregate the declared 24h window.

| Alert | Expression | For | Why |
|---|---|---|---|
| KycDeadJobs | `kyc_jobs{status="dead"} > 0` | 5m | A dead-lettered job means a failed run that a human must requeue. See RUNBOOK and `/v1/ops/requeue/job/{id}`. |
| KycFailedRuns | `kyc_runs{state="FAILED"} > 0` | 5m | This zero-safe series identifies failed runs that need the same requeue attention. |
| KycDeadOutbox | `kyc_outbox{status="dead"} > 0` | 5m | An undeliverable callback or email usually indicates a platform endpoint or HMAC secret problem. Use `/v1/ops/requeue/outbox/{id}` after fixing the cause. |
| KycOutboxBacklogLevel | `kyc_outbox{status="pending"} > 100` | 15m | Level threshold, not a growth rule. The endpoint exposes gauges, not counters; no counter series is available for a `rate()` rule. |
| KycDecisionLatency | `kyc_event_to_decision_seconds_p95 > 120` | 15m | Enforces the DEPLOYMENT §5 budget for full runs. The exporter provides one latency aggregate. Separate alerting for the light-run budget below 10 seconds requires persisted run classes and separate exported series. |
| KycAdapterErrors | `kyc_adapter_error_rate > 0.2` | 15m | Sustained upstream error rate after in-adapter transient retry |
| KycReadyzDown | `probe_success{job="kyc-readyz"} == 0` | 5m | `/readyz` is the DB/migration/storage gate (blackbox-probe it) |

## Scrape identity (production)

Production requires a current path-bound signed request. A stock Prometheus cannot produce one, and
scraping with the legacy v1 scheme is prohibited because every accepted v1 request records v1 traffic in
the durable witness and would stall the v1 sunset forever. Until the dedicated least-privilege scrape
identity is available, scoped as a read-only bearer for `/v1/metrics.prom` only, run the scrape
through a sidecar that v2-signs each request. Staging hosts set up per `docs/DEPLOYMENT.md`
§2 require signed reads too, so scrape them the same way. Do NOT wire a v1 signer into a scraper.

# Part 6: Salesforce mapping

The KYC Tool **never** reads from or writes to Salesforce. The platform mirrors
tool state one-way after every enforced decision and material check change.
The mapping translates the tool's callback and read APIs into the
`salesforce_sync_fields.json` field set and defines the value for
`approved_manual`, which the base field list leaves unspecified.

Sources for every value below: the decision callback (`POST …/kyc/decision`),
`GET /v1/cases/{case_id}` and `GET /v1/cases/{case_id}/checks?all=1`. The
mapped, destination-keyed view of all of them is
`GET /v1/cases/{case_id}/salesforce-projection` (`PLATFORM_INTEGRATION.md` §7).

After explicit live-configuration activation, the field names below remain the
default destinations and stable source identities. The console edits destination
names only (1–80 ASCII identifier characters, case-insensitively unique), not
sources, types, or company values. Saves are shared server revisions, not previews.
The platform reads the destination-keyed values, `mapping_revision`, and source
identities from `GET /v1/cases/{case_id}/salesforce-projection`. The console's
own case view shows the same mapping to operators. Subsequent projection reads
use the saved mapping without rewriting old decisions or callback bytes. The
platform must explicitly consume the projection for a mapping change to reach
Salesforce.
A mapping save neither writes Salesforce nor proves that the platform adopted it.

## KYC_Case__c fields

| Salesforce field | Source in tool output | Mapping |
|---|---|---|
| `KYC_Status__c` | case `status` | `registered`* / `email_verification_pending`* / `email_verified`* / `enrichment_running`* → their labels. `kyc_pending` → "KYC Pending". `manual_review_insufficient` → "Manual Review - Insufficient Score". `account_approved` → "Account Approved". **`approved_manual` → "Account Approved"** (see `Platform_Action_Taken__c`). `rejected` → "Rejected". *Pre-tool statuses are platform-owned by contract. |
| `KYC_Score__c` | the pointed latest-decision row's `score` (what was decided — never the live recomputed case score. Blank while the pre-014 decision order is unresolved) | integer, as-is |
| `Buy_Enablement_Status__c` | case `buy_status` | `not_applicable` → "Not Applicable". `buy_locked_org_id_required` → "Buy Locked - ORG-ID Required". `org_id_validation_pending`† → "ORG-ID Validation Pending". `org_id_failed`† → "ORG-ID Failed". `buy_enabled` → "Buy Enabled". `buy_suspended` → "Buy Suspended". †Platform-derived transient states — the tool's callback only ever asserts `enabled` / `locked_org_id_required`. The platform may show finer-grained transitions between callbacks. |
| `Platform_Action_Taken__c` | callback `decision` + manual-approve event | `approve` → "Approve Account". `approve_buy_locked` → "Approve Account - Buy Locked". `reject` → "Reject" (or "Suspend" per platform policy). Manual approve (no callback — the platform initiated it) → "Manual Approve" |
| `ORG_ID__c` / `ORG_ID_Status__c` | live `org_id_match` check | Handle from check detail. Status: none → "Pending". `pass` → "Pass". `fail` → "Fail". Superseded rows → "Superseded" |
| `POC_Handle__c` / `POC_Verification_Status__c` | live `poc_verified` check + review state | No check, no token → "Pending". Token sent (`poc_tokens` outstanding) → "Token Sent". `pass` → "Verified". `fail` → "Failed". Superseded → "Superseded" |
| `Business_Document_Status__c` | live `business_document_verified` check | None → "None". Uploaded but unprocessed → "Uploaded". `pass` → "Verified". `fail` → "Failed" |
| `Website_Review_Status__c` | review task / check | Open task → "Open". Check `pass` → "Pass". Check `fail` → "Fail" |
| `Broker_Status__c` | case `broker_status` | `clear` → "Clear". `allowed_broker` → "Allowed Broker". `blocked` → "Blocked" |
| `Hard_Conflict__c` | callback `gates.no_hard_conflict` | **Nullable boolean**, negated when present (`no_hard_conflict: false` ⇒ `true`). It is `true` when a live check carries `hard_conflict`, `document_registry_conflict` or `registry_exact_company_inactive`. The last means the official registry reports the exact company as inactive. On its own, that routes the case to manual review, never to rejection. NULL occurs in exactly two conditions: there is no authoritative decision tuple because the pointer or legacy order is unresolved, or a manual approval bypassed the gates and they were never evaluated. The platform sync must carry NULL through and must not coerce it to `false`. Doing so would assert "no hard conflict" when no gate ran. The base `salesforce_sync_fields.json` type is `boolean`. This mapping adds the required nullable behavior. |
| `Review_Reason_Codes__c` | union of live checks' `reason_codes` | delimited text / multi-select |
| `Manual_Approved_By__c` / `Manual_Approved_At__c` | latest MANUAL decision row (sticky: a later automatic decision moves the latest-decision pointer but never blanks the manual attribution while the case stays `approved_manual`). Platform initiated it. Also in tool audit log. | reviewer id, timestamp |

## KYC_Check__c child records

One record per row of `GET /v1/cases/{id}/checks?all=1`:
`Check_Type__c` ← `type` · `Status__c` ← `status` · `Points__c` ← `points` ·
`Category__c` ← `category` · `Source__c` ← `source` · `Superseded__c` ←
`superseded_by_check_id IS NOT NULL` · `Reason_Codes__c` ← `reason_codes` ·
`Created_At__c` ← `created_at`.

## Delivery rules

- Upsert by external ID = platform case id. Never duplicate KYC Cases.
- The callback is at-least-once: **dedupe on (`case_id`, `run_id`)** — both are
  stable across redeliveries (verified by the tool's test suite).
- Until ordered delivery is activated in migration `025`, the wire provides no
  callback-order authority. Keep a manual approval authoritative. Acknowledge
  and record subsequent valid automatic callbacks, but hold unordered callbacks
  for review instead of applying them. If automatic callbacks conflict, use an
  ordering authority the platform owns or hold them for review. Never infer
  order from `decided_at` or `event_sequence`.
- After migration `025` is governed, bootstrapped and activated, apply its
  `decision_sequence` rules exactly as defined by the platform contract.
- A Salesforce outage must never block platform enforcement.
- Salesforce KYC fields are read-only for non-integration users. No Salesforce
  automation may call back into the tool or platform KYC actions.

# Part 7: Production readiness

This package supports closed staging. It is not approval to run the
service in production. Production remains **no-go** until every gate in this
document is supported by recorded evidence.

`PLATFORM_BRIEFING.md` §8 defines the 15 numbered inputs and decisions. The
integration, deployment and runbook documents own their respective wire and
operating procedures. If a checklist summary here conflicts with one of those
procedures, follow the procedure and resolve the conflict before deployment.

## Current boundary

The staging package includes signed event ingestion, scoring, read APIs,
decision callbacks, retries, dead-letter handling, an operator console, live
registry clients, S3-compatible evidence storage, Salesforce projection and a
conformance kit. Positive-decision enforcement may be exercised only in a
separately authorized, closed staging rehearsal using synthetic accounts.

Production work is still open in these areas:

- Select and wire the production POC directory and delivery path. The open
  choice is platform-owned delivery through a typed contract or tool-owned SES.
- Select and wire the production document path. The open choice is
  platform-extracted JSON or tool-side OCR with an approved provider.
- Agree how document authenticity and issuer provenance are established.
  Matching four extracted fields to the submission is a consistency check,
  not proof that the document is genuine. Define and test upload authorization,
  allowed types and sizes, safe storage, scanning and quarantine for the chosen
  path before relying on document points for unattended approval.
- Build the production provider profile. Production must not select fixtures,
  empty directories, file sinks or development stand-ins.
- Build and activate migration `025` with the platform-owned ordering bootstrap.
  Until then, callbacks have no wire ordering authority. The receiver must keep
  manual approvals authoritative, record later valid automatic callbacks and
  hold unordered or conflicting results for review.
- Publish the versioned contract bundle used by both sides.
- Run load and soak tests against agreed capacity and latency targets, plus the
  recovery objectives.
- Build the platform consumer that reads the public Salesforce projection and
  writes to the Salesforce sandbox and production orgs.

### Remaining tool-side database work

The current migration head is `024`. After the ordering work in `025`, the
following reserved revisions must be completed in order. These are remaining
IPv4.Global delivery work, not migrations TechCraft should attempt to run now.

| Reserved revision | Work still required |
|---|---|
| `026` | Revalidate existing evidence when validation policy changes, with staged rollout controls. |
| `027` | Add the durable lease token and single-runner backstop for background jobs. |
| `028` | Adopt structured object references and immutable source-evidence metadata before production document processing. |
| `029` | Add durable retry-limit checks and the shared cutover-attestation record. Complete the remaining operational controls, including rollout observation and bounded external-call execution. |

The current runtime protections do not mean these database and operating
requirements are finished. All of them belong to the production backlog that
must close before automatic positive decisions are enabled.

## Completion matrix

These are production acceptance requirements. Evidence has not been collected
for the following rows.

| Area | Current implementation | Responsible team | Observable acceptance result |
|---|---|---|---|
| Provider wiring | Live registry clients exist. The production POC, email and document profile is incomplete; document field matching does not establish authenticity. | IPv4.Global service team and TechCraft platform team | Uncollected — a production-profile run uses only approved live providers, completes the chosen POC and document paths, and passes the agreed authenticity and upload-control tests. |
| Applicant authority | Automated control requires a passed POC for the RIR-listed contact channel. This is not unrestricted legal authority. | Joint product approval | Uncollected — recorded policy defines when POC is sufficient and when human approval is required, with production cases demonstrating both paths. |
| Registry negatives | Exact matched inactive evidence holds for review. Generic historical inactive results do not create the hard conflict. | IPv4.Global service team | Uncollected — live-provider cases prove exact inactive, mismatched inactive and active-plus-inactive outcomes, followed by governed evidence revalidation. |
| Callback ordering | The current wire is unsequenced. Migration `025`, activation, the platform bootstrap and receiver acceptance remain open. | IPv4.Global service team and TechCraft platform team | Uncollected — the tool emits governed order and the durable receiver passes ordered, duplicate, delayed and conflicting callback cases without using arrival time. |
| Evidence freshness | The run and current-check reads are review aids only; no decision-bound source-age and coverage contract exists. | IPv4.Global service team and TechCraft platform team, with joint product approval of source-age and coverage rules | Uncollected — the teams implement the approved contract, bind its evidence to the decision, and prove that unavailable or unprovable freshness and coverage hold. The existing reads do not meet this acceptance result. |
| Reviewer workflows | Manual approval and review tasks exist. Platform assignment and user notifications remain external. | TechCraft platform team | Uncollected — staging proves assignment, evidence request, manual precedence and governed release without callback override. |
| Sanctions | The tool performs no sanctions screening. | TechCraft platform team | Uncollected — the platform records a passing sanctions control before calling the tool and demonstrates failure handling. |
| Salesforce reconciliation | The read-only projection exists. The platform writer, retry and backfill service is not built here. | TechCraft platform team | Uncollected — sandbox evidence shows writes, retries, mapping changes, backfill and reconciliation through the public projection. |
| Recovery | Retry, dead-letter and documented restore procedures exist. A full rehearsal is outstanding. | IPv4.Global service team and TechCraft platform team, with joint product approval setting recovery targets | Uncollected — database and object-store restore plus interrupted-cutover exercises meet approved recovery objectives. |
| Capacity | No approved load targets or completed soak result are recorded. | IPv4.Global service team and TechCraft platform team, with joint product approval setting targets | Uncollected — signed volume, latency and maintenance targets are met by the release image in a representative soak. |

## Production go/no-go gate

Production is **no-go** if any item below is false:

- Pull-request CI and the full PostgreSQL suite are green for the release
  artifact.
- A fresh database migrates to the declared production head.
- Every production process boots with no fixture-selected capability.
- All event variants pass the TechCraft conformance suite.
- The platform receiver commits before returning 2xx and passes replay tests.
- POC verification works end to end with the selected live directory and
  sender.
- Salesforce mapping changes reach the sandbox through the public projection.
- Migration `025` is bootstrapped and active before production automatic
  decisions depend on callback order.
- The full production backlog and platform cutover are complete before
  positive-decision enforcement is enabled. A human-review pilot keeps that
  enforcement off and does not complete the production program.
- Real-provider staging and the signed capacity targets pass.
- Full restore and interrupted-cutover rehearsals pass.
- Executable contracts, public schemas, deployed behavior and product documents
  agree.

## Required platform evidence

Before the go/no-go review, TechCraft must provide or confirm:

1. Staging and production callback base URLs.
2. Key identifiers and signing ownership for each wire direction. Secrets stay
   in the deployment secret manager.
3. The accepted-run ledger schema, including effective-source rules for manual
   approvals and reverted decisions.
4. S3 bucket and key conventions, plus IAM ownership.
5. Salesforce sandbox access and the service that consumes the public
   projection, including retry and backfill reconciliation.
6. The review-task integration: the default change webhook, or a polling
   contract with its cursor, freshness and missed-poll recovery rules.
7. The document-extraction choice and its production provider contract.
8. The POC-email choice and its production delivery contract.
9. Expected daily volume, peak concurrency, soak duration, maintenance-window
   limits and recovery objectives.

The complete decision list remains `PLATFORM_BRIEFING.md` §8 items 1–15.

## Evidence package for approval

The production review needs the exact image digest and policy-bundle hash, the
declared migration head, CI results, database migration output, process startup
attestations, conformance output, receiver replay results, real-provider staging
results, Salesforce sandbox evidence, capacity results, restore results and the
signed platform cutover artifacts.

The runbook must have been executed against staging. A document review is not a
substitute. A full database plus object-store restore must prove consistency and
record recovery time and data loss against the agreed objectives. Repairing one
row is not a disaster-recovery rehearsal.

## Controls that stay in force

- Production launches with positive-decision enforcement off. Enable it only
  after every gate above passes.
- Do not turn a drained flag flip or maintenance cutover into a rolling change.
  Follow `DEPLOYMENT.md` for migration `010`, bundle pinning, migration `013`
  and migration `024`.
- Do not downgrade through a migration refusal or delete immutable evidence to
  make a downgrade possible. Use the documented roll-forward or compatible-image
  recovery path.
- Do not retire inbound HMAC v1 until the configured inbound sunset date and
  required inbound zero-traffic witness both pass. Keep staging closed while
  inbound v1 remains accepted. Outbound v1 follows its separately configured
  outbound sunset and does not use the inbound zero-traffic witness.
- Do not infer callback order from timestamps, `event_sequence` or a local
  database sequence that is not present on the wire.
- Do not substitute the diagnostic conformance receiver for TechCraft's own
  durable receiver.
- Do not enable production with a stub provider, file email sink or missing
  authority artifact.

## Deliberate exclusions

This program does not replace the platform console, let the tool write to
Salesforce, automate website judgment, add fuzzy broker matching, add new paid
enrichment sources without approval, split the service into microservices or
rewrite historical decisions when configuration changes.
