# Platform Integration Guide — MVP

For TechCraft's integration developers. This guide covers the event sender,
decision receiver, reviewer actions, Salesforce reads, and the proposed POC
confirmation page. Product decisions and the numbered delivery checklist are
in `PLATFORM_BRIEFING.md` §5 and §8.

This release is for closed staging, not a production launch. Production provider
wiring and ordered callback delivery are unfinished. The email and document
options in §5–§6 need agreement before their production paths can be built.

## 1. The model

A case is one registrant: the person signing up on your platform on behalf of a
company. You send us what they told you, we check it against public registries,
and we send back a verdict your registration team can act on.

You POST events such as registration data, a verified email or an ORG-ID.
Accepted new events normally queue background work: the tool gathers evidence,
scores it, and POSTs a decision to your webhook. Manual approval is handled
inline. Replaying an accepted event does not create another run (§3).

Decisions: `approve`, `approve_buy_locked` (account OK, purchasing held until
ORG-ID verifies), `manual_review_insufficient`, `reject`.

`platform_account_id` on the sign-up event is your id for that person, and
every decision we send back is about that case, so about that contact. Company
evidence (registry records, ORG-ID, website) is about the company they claim.
Company email, ORG-ID and LinkedIn results are supporting evidence. Automated
control proof requires a passed POC token sent to the RIR-listed contact channel
associated with the submitted organization or resource. That proves access to
the channel, not unrestricted legal authority to represent the company.
Approving a case approves the contact, not the company. A second registrant at
the same company is a second case, with its own `case_id`.

**Initial integration posture:** auto-enforcement is off. A computed `approve` /
`approve_buy_locked` is delivered as `manual_review_insufficient` with an
`enforcement_held` marker (§4), and the registration team confirms it. Flipping
enforcement requires the full M2 prerequisites, including completion of the
remediation backlog, real-provider staging tests and platform cutover approval.
An optional automation rehearsal uses synthetic sandbox accounts only, after
the approvals in `PLATFORM_BRIEFING.md` §6. It does not permit live permission
changes or bypass the interim receiver holds in §4. This guide is not permission
to enable production enforcement.

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

Everything you tell us arrives as an event on one endpoint. There is no
registration call and no re-verify call: you post what happened, and the tool
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
  our side. Delivery failures then need operator recovery (§8). Retries are
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

## 5. POC verification page (you build this)

The POC exchange proves access to the RIR-listed contact channel associated
with the submitted organization or resource. It does not prove unrestricted
legal authority to represent an entity. A code goes to the address the
regional registry lists, and the registrant types it back. You host the page
they type it into. Who sends that email is the first open question.

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

**Open question: can your platform send that email?**

- **If yes** — the platform sends it through its existing transactional email.
  We hand you the token, the registry-listed recipient and the case reference
  over a typed delivery contract still to be written, and we host no mail.
- **If no** — the tool sends it through an Amazon SES sender that IPv4.Global
  provisions with an identity and a sending domain. That sender is not built,
  and a production process refuses to boot while the email provider is the dev
  stub.

The token checks are built. The shipped worker still uses an empty POC
directory, so staging cannot complete this flow until the built live directory
is wired in. Production also needs the agreed email path. A file email sink
can support closed-staging tests. It is not a production sender.

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

You do not need this field to identify a missing ORG-ID today. The decision
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
| v1 signature retirement (v2 path-bound signing is live now, §2) | agreed dates and the recorded inbound zero-v1 observation window |
| Ordered callback delivery (`decision_sequence`) | migration `025` and the platform bootstrap/receiver agreement; not built in this release |
| Auto-enforcement | the full M2 gate: completed remediation backlog, real-provider staging end-to-end tests and platform cutover sign-off |

The two provider decisions and ordering activation require additional contracts.
Do not treat proposed fields or delivery paths as available API features.

## 10. Answers we need

Every answer we still need from the platform team and from IPv4.Global is in
`docs/PLATFORM_BRIEFING.md` §8. Its §4 lists what your team builds, and its §5
holds the two questions still open.

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
