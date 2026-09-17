# Platform Integration Guide — MVP

For the IPv4.Global platform team. Everything needed to integrate the KYC tool:
the API you call, the two things you build (a webhook receiver and a POC
confirmation page), the MVP scope, and the open decisions.

## 1. The model

The tool is an async verification service. You POST events (registration data,
a verified email, an uploaded document, an ORG-ID). Each event is acknowledged
immediately and processed in the background: the tool gathers evidence from
public registries, scores it, and POSTs a decision to your webhook.

Decisions: `approve`, `approve_buy_locked` (account OK, purchasing held until
ORG-ID verifies), `manual_review_insufficient`, `reject`.

A case is one registrant — the person (the contact) signing up, on behalf of a
company. `platform_account_id` on the sign-up event is your id for that person,
and every decision we send back is about that case, so about that contact.
Company evidence (registry records, ORG-ID, website) is about the company they
claim; contact evidence (an inbox at the company's domain, a LinkedIn profile
showing them at that company, later the RIR contact token) ties the person to
it. Approving a case approves the contact, not the company. A second registrant
at the same company is a second case, with its own `case_id`.

**MVP posture:** auto-enforcement is off. A computed `approve` /
`approve_buy_locked` is delivered as `manual_review_insufficient` with an
`enforcement_held` marker (§4), and the registration team confirms it. Flipping
enforcement on later changes no part of this contract.

## 2. Authentication (both directions)

Every request — yours to us, our webhook to you — carries:

```
X-KYC-Timestamp: <unix seconds, e.g. "1752681600">
X-KYC-Signature: <hex HMAC-SHA256(secret, timestamp + "." + raw_body)>
```

- The signed message is the timestamp string, a literal `.`, then the **raw
  request body bytes**. Sign the exact bytes you send; verify the exact bytes
  you receive (before any JSON parsing).
- Requests older/newer than 300 seconds are rejected — keep clocks on NTP.
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
dual-accept window and give you fixed sunset dates; integrate v2 at your pace,
nothing breaks in the meantime. Headers change to:

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

One recipe covers everything you send — the only per-request variables are the
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

Rules: **if you send any v2 header, the request must be complete, valid v2** — we
do not fall back to v1 for a v2-labelled request, and sending *any* v2 header
(even present-but-empty) locks the request to v2. `key_id` is a constant from your
config (it only changes on a secret rotation). Our webhook callbacks dual-emit
both signatures until the outbound sunset, so your receiver can migrate whenever
it's ready.

## 3. Sending events

```
POST /v1/cases/{case_id}/events
Idempotency-Key: <unique string per event attempt>
X-KYC-Timestamp / X-KYC-Signature: as above
```

`case_id` is your identifier for the registrant (stable across all events for
that person). Cases are created on first event, one per registrant.

Envelope (exactly these four keys):

```json
{
  "event_type": "kyb.run_requested",
  "occurred_at": "2026-07-16T12:00:00Z",
  "actor": {"type": "user", "id": "platform-user-123"},
  "payload": { ... }
}
```

`actor.type` is `user`, `reviewer`, or `system`.

### Responses

| Code | Meaning | Handling |
|---|---|---|
| 202 | Accepted; body `{"run_id": ..., "status": "queued"}` | done — result comes by webhook |
| 200 | Replay of an already-processed key; stored response returned | safe retry, done |
| 400 | Missing `Idempotency-Key` | fix request |
| 401 | Bad/missing signature or stale timestamp | fix signing |
| 409 | Same `Idempotency-Key`, different body | bug on your side — never reuse keys |
| 422 | Payload failed validation | fix payload |

Retry on network failure with the **same** key and **same bytes**; you'll get
200 instead of a duplicate run.

### Event types and payloads

| event_type | Payload (required unless noted) | Notes |
|---|---|---|
| `kyb.run_requested` | `company_legal_name`, `contact`, `platform_account_id`; optional `address`, `registration_number`, `jurisdiction`, `website` | send at registration; full check run; `contact` is the registrant, an object with required `name` and `email` (the address they signed up with) and optional `title`, `first_name`, `last_name`; the LinkedIn check compares the person's name and the company domain (company name and title are recorded for the reviewer, not compared), and uses `email` and the split names to find the right profile |
| `email.verified` | `email`, `domain`, `verified_at` | you own email verification; this asserts it happened; `email` must be the contact's sign-up address (`contact.email`) |
| `org_id.submitted` | `rir`, `org_handle` | `rir` ∈ `arin, ripe, apnic, lacnic, afrinic`; send it right after the sign-up event when the registrant supplied a handle at registration, and again whenever they add or change it later — it is optional at registration, and the check runs the moment it arrives. A reviewer may also record one in the operator console when the contact supplies it by other means; the envelope's `actor` says which (`reviewer` rather than your own `user`/`system` actor) |
| `poc.submitted` | `rir`, `poc_handle`; optional `org_handle`, `resource` | starts the verification email (§5) |
| `poc.token_verified` | `token_id`, `token`, `verified_at` | posted by your confirmation page (§5) |
| `document.uploaded` | `object_ref`, `doc_type` | see §6 |
| `reviewer.manual_approve` | `reviewer_id`; optional `note` | inline 200 with case state; no run, no callback; buying still locked without a verified ORG-ID; requires a matching reviewer actor (below) |
| `recalculate.requested` | `{}` | re-decides from current evidence |

Unknown extra payload fields are accepted and preserved. Send events in the
order they happen; each triggers its own run and its own decision callback.

### Reviewer actor requirement (`website.review_completed`, `reviewer.manual_approve`)

These two events are reviewer-sensitive: the signed envelope's `actor` must
identify the reviewer who acted, not just any authenticated caller.

- `actor.type` MUST be `"reviewer"`.
- `actor.id` MUST equal the payload's `reviewer_id` — both **nonblank** after
  trimming whitespace, compared **exact, case-sensitive**. A matching pair of
  blank strings authorizes nothing.
- A mismatch, a blank id on either side, or the wrong `actor.type` is rejected
  **422** — the request is authenticated (it carried a valid signature) but
  internally inconsistent, so it is not a 401/403.
- The tool records the **actor-derived** reviewer identity (`actor.id`) as the
  reviewer of record on the task, check, and audit trail — never the payload's
  `reviewer_id` field. Send both, and make them match.

### When to send each event

Evidence is optional at every step — the tool scores whatever exists. The
platform's whole job is: when verification-relevant information is added **or
changed**, send the matching event. The tool re-runs and sends a fresh verdict;
there is no separate "retry" or "re-verify" call.

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

Expose HTTPS `POST {your_base_url}/kyc/decision`. We sign it per §2 — dual-emitting
v1 (the legacy shared secret) and v2 (the dedicated **outbound** secret + key id)
until the outbound sunset, then v2 only. The v2 signature binds the **literal**
request path, so if `{your_base_url}` has a path prefix (e.g. `…/hooks`), we sign
`/hooks/kyc/decision`, not `/kyc/decision` — verify against the full path you
received. Respond 2xx to acknowledge; anything else and we retry.

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
  Acknowledge every exact valid duplicate as processed; acknowledging a
  callback does not mean applying it to the case.
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
features; the tool supplies the statuses. Suggested mapping:

| Result | Suggested platform handling |
|---|---|
| `approve` | activate the account; notify the user |
| `approve_buy_locked` | activate; notify the user with the ORG-ID prompt |
| `manual_review_insufficient` **with** `enforcement_held` | "ready to confirm" queue — the tool computed a positive; an admin confirms (MVP only) |
| `manual_review_insufficient`, no marker | manual-review queue; assign a reviewer; notify admins |
| `reject` | admin notification; user handling per ops policy |

Two boundaries that shape this: only a broker-blocklist match ever auto-rejects
(everything else that falls short goes to review, so expect the review queue,
not rejections, to carry the volume), and sanctions screening happens on the
platform **before** the tool is called — a sanctioned registrant never reaches
it.

**Display guidance.** Per case you have: decision, buy state, score, the five
gate booleans, and per-check status with reason codes. Show users the status
and the next useful step (verify your email, add your ORG-ID). Keep score,
gates, and reason codes in admin views — publishing exactly why checks fail
makes them easier to game. Wording is yours; the reason codes are stable
strings safe to key copy on.

## 5. POC verification page (you build this)

Flow for proving control of IP resources:

1. You post `poc.submitted`.
2. The tool looks up the POC in the registry directory. Whichever side sends
   the email (open decision, below), it goes to the **registry-listed**
   address, never a user-supplied one. The email contains:
   `Your verification token: <secret>` and `Verification reference: <id>`.
3. The user enters both on your confirmation page.
4. You post `poc.token_verified` with `token` (the secret) and `token_id` (the
   reference). Both are required; a placeholder `token_id` fails.
5. Result arrives as a normal decision callback.

Rules your page must respect:

- **Single-use.** A verified token is spent (`poc_token_consumed` on reuse).
- **Expires in 72 hours** (`poc_token_expired`).
- **Bound to the submitted identity.** If the user changes their ORG-ID, POC
  handle, or resource after the email went out, the old token fails
  (`poc_token_binding_mismatch`).
- Recovery is always the same: re-submit the POC (`poc.submitted` again) — old
  tokens are cancelled and a fresh email goes out. Don't build a "resend same
  code" button.
- Changing identity details also suspends previously earned proof: expect
  scores to drop after an ORG-ID/POC edit until re-verified
  (`org_id_revalidation_pending`, `poc_not_associated`). Not a bug.

**Who sends the email (open decision).**

- Option A — the tool sends it, through an Amazon SES sender the tool owns
  (not built yet; needs an SES identity and sending domain from
  IPv4.Global).
- Option B — the platform sends it, through its existing transactional
  email, in which case the tool hands the platform the token and reference
  through a typed delivery contract to be written.

In both options the token rules above (single-use, 72 hours, binding) are
enforced by the tool.

## 6. Documents (open decision: who extracts)

The kickoff call leaned toward the platform extracting the document fields,
but that is not decided. Two options are open until IPv4.Global confirms one:

**Option A — platform extracts.** The platform stores the upload (your
existing virus scanning and quarantine unchanged), extracts the four fields
below, writes them as a JSON object to the shared object store, and posts
`document.uploaded` with `object_ref` pointing at that JSON. The tool runs no
OCR — it reads that JSON as posted. The numbered steps below are Option A's
contract.

**Option B — tool extracts.** The platform stores the upload and posts
`document.uploaded` with `object_ref` pointing at the **original file** (PDF
or image) in the shared object store. The tool runs an OCR engine and
extracts the same four fields itself. What it requires: an OCR provider
chosen and contracted by IPv4.Global (none is built; the current engine only
reads extracted JSON, and production refuses that stub), file-type and size
limits agreed, and the same `document.uploaded` event — the wire contract
does not change either way.

Documents are optional at registration — a case scores without them, and a
later upload just re-runs verification (§3).

1. Put a JSON object in the shared object store:
   `{"fields": {"name": "...", "address": "...", "number": "...", "jurisdiction": "..."}}`

   | Key | Meaning |
   |---|---|
   | `fields.name` | legal name exactly as printed on the document |
   | `fields.address` | registered address as printed |
   | `fields.number` | registration / company number as printed |
   | `fields.jurisdiction` | issuing jurisdiction, e.g. `GB` |

   Each key is individually optional; anything missing routes toward review,
   never toward a pass. Extract what the document says, not what the user
   typed — the tool's job is exactly to compare the two.
2. Post `document.uploaded` with `object_ref` (storage key) and `doc_type`
   (`registration_certificate` for formation/registration documents; more
   types can be added as needed).
3. Keep the original upload on your side for audit.

Until this is answered, staging uses Option A with hand-extracted JSON;
production cannot start on either option before it is decided.

## 7. Read API and review tasks

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

**Completing a website review** is a normal signed event, not a separate
endpoint: post `website.review_completed` to `POST /v1/cases/{case_id}/events`
with payload `{"task_id": "...", "result": "pass"|"fail", "reviewer_id": "..."}`.
The envelope's `actor` must identify the same reviewer — `actor.type:
"reviewer"` and `actor.id` equal to `reviewer_id` (§3) — or the event is
rejected 422; the tool records the actor-derived reviewer, never the payload
field. The tool validates the task exists, is a website task on that case, and
is open (else 404/409/422); the transition, check, and audit are identical to
any other event. (The old `POST /v1/review-tasks/{id}/complete` endpoint is
retired — it duplicated this event.)

**What a reviewer has asked the contact for.** `GET /v1/cases/{id}` carries
`information_requested`: one entry per outstanding ask, shaped `{"field":
"org_id" | "registration_number" | "address" | "poc", "requested_at": …,
"requested_by": …, "note": … or null}`. A reviewer raises one from the operator
console when a case is stuck for want of evidence the registrant never
supplied. The tool records the ask; the platform owns the message that reaches
the contact. An entry drops off by itself once the case receives that evidence
— `org_id` clears when `org_id.submitted` arrives, and so on — so there is
nothing to close and nothing to acknowledge.

You do not need this field to chase a missing ORG-ID today: the decision
webhook's `checks[].reason_codes` already carry
`org_id_submission_incomplete`, which is the same fact at decision time. How
you would rather learn of a reviewer's request — polling this field, or a
message we send you — is `PLATFORM_BRIEFING.md` §8 item 13, and nothing
outbound is built until you answer.

### Salesforce projection (pull)

`GET /v1/cases/{case_id}/salesforce-projection` is how the platform reads the
Salesforce-shaped view of a case. It is a pull: call it after a callback, or on
your own schedule, for the case you are about to mirror. The tool never writes
Salesforce; this endpoint is the only sanctioned source for the mirror, and
nothing in a production integration reads the console's `/ui/api/…` routes.

One response is one consistent snapshot (a repeatable-read, read-only
transaction), so a mapping saved in the console mid-request cannot produce a
half-old, half-new body.

| Field | Meaning |
|---|---|
| `case_id` | the case |
| `fields` | object keyed by the **destination** name currently saved in the console; each entry carries `source_field` (the canonical `salesforce_sync_fields.json` name, e.g. `KYC_Status__c`), `source_identity` (the tool value it came from, e.g. `case.status`), `value_type` (`enum`, `integer`, `text`, `boolean`, `datetime`, `check_records`), `nullable`, and `value` |
| `mapping_revision` | the active configuration revision the destination names were read from; `null` when no live configuration has been activated (destinations then equal the canonical names) |
| `configuration_revision` | the configuration revision the pointed automatic decision ran under; present only for a resolved automatic decision, otherwise `null` |
| `decision_authority` | which decision the values come from: `provenance`, `decision_row_id`, `run_id`, `decision_kind` (`automatic` or `manual`), `decision`, `run_provenance` |
| `manual_approval_authority` | the latest manual approval, if any: `provenance`, `decision_row_id`, `reviewer_id`, `decided_at` |
| `projection_timestamp` | RFC 3339 UTC timestamp of the snapshot |

Statuses: `200`; `401` (signature invalid, missing, or retired); `404` (unknown
case); `503` (configuration or the v1 signature witness unavailable; retry).
Error bodies are `{"detail": "..."}`. The exact schema is
`SalesforceProjectionResponse` in `/openapi.json`.

A console mapping change affects the next read. It never rewrites earlier
decisions or callback bytes, and it does not by itself prove the platform
adopted the new destination names. `docs/SALESFORCE_MAPPING.md` has the
per-field value rules.

In production these reads also require the §2 signature headers. There is also
an operator console (`/ui`) for the registration team — dashboards, case
detail, review queue — independent of this API.

## 8. Hosting and deployment (you run this too)

The platform team hosts and operates the tool in IPv4.Global's AWS account;
IPv4.Global maintains the code and cuts releases. An update is: pull the
release, build the image, run the migration, restart — no code is edited on
the server. A `Dockerfile` ships in the repo; `docs/RUNBOOK.md` is the
operator guide (every env var, health checks, dead-letter recovery).

- **Stack:** Python 3.11, FastAPI. **PostgreSQL 14+ is the only hard
  infrastructure dependency** — queue and webhook outbox live in Postgres. No
  Redis/broker.
- **Also needed in production:** an S3-compatible bucket (evidence), outbound
  HTTPS (RDAP registries, Companies House, GLEIF), and — if the tool sends the
  §5 email — an email provider.
- **Processes** (stateless, scale horizontally): API (`uvicorn
  kyc_tool.api.app:create_app --factory`), pipeline worker, outbox worker, and
  a daily retention cron.
- **Deploy:** `alembic upgrade head`, start processes. Wire `GET /readyz` to
  the load balancer (checks config, DB, migration version, storage);
  `GET /healthz` for liveness.
- Config is environment variables prefixed `KYC_` (full table:
  `docs/RUNBOOK.md`). With `KYC_ENVIRONMENT=production` a misconfigured process
  refuses to boot and lists every violation — intentional fail-closed.

## 9. MVP scope and what comes later

Works now: full event flow, registry + broker + document (extracted-fields) +
email checks, scoring, webhooks, review queue, audit trail, idempotent replays.

| Added later | Unblocked by |
|---|---|
| Tool-side OCR of raw files | extraction decision + engine choice |
| Live POC verification emails | the §5 sender decision, then an email provider + sending domain, or the platform hand-off contract |
| Live Companies House lookups | API key (free registration) |
| LinkedIn/company enrichment | Floqer access |
| `event_sequence` in callbacks | your confirmation |
| v1 signature retirement (v2 path-bound signing is live now, §2) | agreed inbound/outbound sunset dates |
| Auto-enforcement (the flag flip) | staging end-to-end on real providers + platform cutover sign-off |

None of these change the API in §§2–7.

## 10. Answers we need

The consolidated list of what we need from the platform team and from
IPv4.Global is `docs/PLATFORM_BRIEFING.md` §8; the questions already answered
are in its §4 and §5.

Secrets never travel in chat, email, tickets, or documents: use the deployment
secret manager.
