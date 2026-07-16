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
- One shared secret per environment (staging ≠ production), ≥ 32 chars.

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

A v2 scheme (signing method + path as well) ships later with a dual-accept
window — integrate against v1 now; nothing breaks at cutover.

## 3. Sending events

```
POST /v1/cases/{case_id}/events
Idempotency-Key: <unique string per event attempt>
X-KYC-Timestamp / X-KYC-Signature: as above
```

`case_id` is your identifier for the applicant (stable across all events for
that applicant). Cases are created on first event.

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
| `kyb.run_requested` | `company_legal_name`; optional `address`, `registration_number`, `jurisdiction`, `website`, `contact`, `platform_account_id` | send at registration; full check run |
| `email.verified` | `email`, `domain`, `verified_at` | you own email verification; this asserts it happened |
| `org_id.submitted` | `rir`, `org_handle` | `rir` ∈ `arin, ripe, apnic, lacnic, afrinic` |
| `poc.submitted` | `rir`, `poc_handle`; optional `org_handle`, `resource` | starts the verification email (§5) |
| `poc.token_verified` | `token_id`, `token`, `verified_at` | posted by your confirmation page (§5) |
| `document.uploaded` | `object_ref`, `doc_type` | see §6 |
| `reviewer.manual_approve` | `reviewer_id`; optional `note` | inline 200 with case state; no run, no callback; buying still locked without a verified ORG-ID |
| `recalculate.requested` | `{}` | re-decides from current evidence |

Unknown extra payload fields are accepted and preserved. Send events in the
order they happen; each triggers its own run and its own decision callback.

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

Expose HTTPS `POST {your_base_url}/kyc/decision`. We sign it per §2 with the
same shared secret. Respond 2xx to acknowledge; anything else and we retry.

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
- Per-case order is preserved. An optional `event_sequence` integer (per-case
  ordinal of the triggering event) can be enabled once you confirm you'll use
  it.

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
2. The tool looks up the POC in the registry directory and emails the
   **registry-listed** address (never a user-supplied one). The email contains:
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

## 6. Documents (decided: you extract)

Settled on the kickoff call: uploads stay on the platform (your existing virus
scanning and quarantine unchanged), the platform extracts the fields, and the
tool cross-checks them against the registries. Documents are optional at
registration — a case scores without them, and a later upload just re-runs
verification (§3).

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

Add-later: the tool OCRs raw PDFs/images itself, once an OCR engine is chosen.
The event contract does not change — only what `object_ref` points at.

## 7. Read API and review tasks

- `GET /v1/cases/{id}` — status, score, latest decision, live checks with
  reason codes ("what's missing" for follow-up).
- `GET /v1/cases/{id}/checks?all=1` — full check history.
- `GET /v1/runs/{id}` — one run's state and adapter results.
- `GET /v1/review-tasks?status=open` — open human-review tasks (website
  checks, hidden POC email).
- `POST /v1/review-tasks/{id}/complete` — body
  `{"result": "pass"|"fail", "reviewer_id": "...", "reason_codes": []}`, signed.

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
  HTTPS (RDAP registries, Companies House, GLEIF), an email provider (only for
  the §5 flow).
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
| Live POC verification emails | email provider + sending domain |
| Live Companies House lookups | API key (free registration) |
| LinkedIn/company enrichment | Floqer access |
| `event_sequence` in callbacks | your confirmation |
| HMAC v2 | agreed cutover date |
| Auto-enforcement (the flag flip) | staging end-to-end on real providers + platform cutover sign-off |

None of these change the API in §§2–7.

## 10. Answers we need

From the platform team:

1. Callback base URLs (staging, production).
2. Secret exchange procedure.
3. ~~Documents~~ — answered on the kickoff call: platform extracts (§6);
   tool-side OCR stays a later option.
4. Will you consume `event_sequence`?
5. Confirm you'll host the POC page and echo back both `token` and `token_id`.
6. ~~Where the tool runs~~ — answered: the platform team deploys and operates
   it in IPv4.Global's AWS account (RDS Postgres, an S3 bucket, outbound
   HTTPS).

From IPv4.Global:

7. Email provider choice and sending domain.
8. Companies House API key.
9. ~~Who staffs manual review~~ — answered: the IPv4.Global team, working in
   the platform admin. The platform should therefore surface held cases and
   their reason codes (from the webhook body or the read API).
