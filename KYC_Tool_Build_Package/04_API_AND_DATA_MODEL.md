# API & Data Model

## 1. Direction of calls

- Platform → Tool: events (the only way work starts).
- Tool → Platform: decision callbacks + review-task status changes.
- Tool ↔ Salesforce: **none**. The platform owns the Salesforce mirror.

## 2. HTTP API (tool-side)

### Ingestion

```
POST /v1/cases/{case_id}/events
Headers: Idempotency-Key: <uuid>     (required)
Body: {
  "event_type": "kyb.run_requested" | "email.verified" | "org_id.submitted"
              | "poc.submitted" | "poc.token_verified" | "document.uploaded"
              | "website.review_completed" | "reviewer.manual_approve"
              | "recalculate.requested",
  "occurred_at": iso8601,
  "actor": { "type": "user"|"reviewer"|"system", "id": string },
  "payload": { ...event-specific fields... }
}
→ 202 { "run_id": uuid, "status": "queued" }
→ 200 { ...previous result... }        // idempotent replay
→ 422 on schema violation
```

Event payload essentials:

| event_type | payload must include |
|---|---|
| `kyb.run_requested` | full submission snapshot: company legal name, address, jurisdiction, submitted domain/website, contact, any ORG-ID/POC/document refs |
| `email.verified` | email address, domain, verification timestamp |
| `org_id.submitted` | rir (`arin`\|`ripe`\|`apnic`\|`lacnic`\|`afrinic`), org handle |
| `poc.submitted` | rir, poc handle, associated org handle/resource |
| `poc.token_verified` | token id, verified timestamp |
| `document.uploaded` | object-storage ref, declared doc type |
| `website.review_completed` | task id, result pass/fail, reviewer id, reason codes |
| `reviewer.manual_approve` | reviewer id, note |
| `recalculate.requested` | (none) |

### Read

```
GET /v1/cases/{case_id}                 → case, live checks, score, gates, latest decision
GET /v1/cases/{case_id}/checks?all=1    → full supersession history
GET /v1/runs/{run_id}                   → run state machine position, adapter statuses
GET /v1/review-tasks?status=open        → website / POC-email-unavailable tasks
POST /v1/review-tasks/{id}/complete     → { result, reason_codes, reviewer_id }  (alternative to the event form)
```

### Decision callback (tool → platform)

```
POST {PLATFORM_CALLBACK_URL}/kyc/decision
Body: {
  "case_id", "run_id", "event_id",
  "decision": "approve" | "approve_buy_locked" | "manual_review_insufficient" | "reject",
  "score": int,
  "gates": { "score_met": bool, "legal_proof": bool, "control_proof": bool,
             "broker_ok": bool, "no_hard_conflict": bool },
  "buy_enablement": "enabled" | "locked_org_id_required",
  "checks": [ { "type", "status", "points", "source", "reason_codes" } ],
  "decided_at": iso8601
}
```
Delivery: at-least-once with exponential backoff; platform dedupes on (`case_id`,`run_id`). Fast synchronous runs may return the same body inline on the ingestion call; the callback is authoritative.

## 3. Run state machine

`machine_readable/state_machine.json`:

```
QUEUED → RESOLVE_INPUTS → BROKER_GATE → RUN_ADAPTERS → VALIDATE
       → WRITE_CHECKS → SCORE → DECIDE → PUBLISH_DECISION → COMPLETE
BROKER_GATE --exact blocked match--> DECIDE (reject, short-circuit)
any state → FAILED (retryable; dead-letter after N attempts)
```

## 4. Data model (Postgres)

```
cases            id, platform_account_id, company_name, jurisdiction, submitted_json,
                 status, buy_status, current_score, latest_decision, created_at, updated_at

events           id, case_id, idempotency_key UNIQUE, event_type, actor_json,
                 payload_json, received_at, processed_at, run_id

runs             id, case_id, triggering_event_id, state, partial bool,
                 started_at, finished_at, error

adapter_results  id, run_id, adapter_id, status, raw_ref, normalized_json,
                 fetched_at, latency_ms

checks           id, case_id, check_type, status(pass|fail|needs_review),
                 points_awarded, category, source, source_detail_json,
                 reason_codes text[], created_by_run_id,
                 superseded_by_check_id NULL, created_at
                 -- immutable rows; live check = superseded_by_check_id IS NULL

review_tasks     id, case_id, task_type(website|poc_email_unavailable),
                 context_json, status(open|done), result, reviewer_id,
                 reason_codes, created_at, completed_at

poc_tokens       id, case_id, poc_handle, rir_listed_email, token_hash,
                 sent_at, verified_at NULL, expired_at NULL

decisions        id, case_id, run_id, decision, score, gates_json,
                 buy_enablement, decided_at, published_at, manual bool,
                 reviewer_id NULL

audit_log        id, case_id, at, actor, action, detail_json   -- append-only
broker_entities  id, name, policy(blocked|allowed), aliases[], domains[],
                 email_domains[], org_ids[], poc_handles[], asns[],
                 effective_date, last_reviewed_at, notes
```

Rules:

- `checks` is append-only; supersession sets `superseded_by_check_id` on the old row inside the same transaction that inserts the new row.
- Score/decision recomputation happens in one transaction with check writes, so a decision always corresponds to an exact set of live checks.
- `events.idempotency_key` is the replay shield; a replay returns the stored outcome without re-processing.
- Raw evidence (RDAP JSON, registry payloads, OCR output, uploaded documents) lives in object storage; the database stores refs.

## 5. Security & compliance basics

- AuthN between platform and tool: mTLS or signed bearer (HMAC of body + timestamp) — pick one in the plan; reject unauthenticated events.
- PII minimization in logs (no full documents/emails in log lines; use refs and case IDs).
- Retention: raw evidence and audit log retained per compliance policy (configurable, default 7 years).
- Secrets via environment/secret manager only.
