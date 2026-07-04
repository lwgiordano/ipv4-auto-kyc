# Salesforce Sync Mapping — hand-off to the platform team

The KYC Tool **never** reads from or writes to Salesforce (05, non-negotiable
#2). The platform mirrors tool state one-way after every enforced decision and
material check change. This document translates the tool's callback/read API
into the `salesforce_sync_fields.json` field set, including the resolution of
audit finding **A5** (`approved_manual` has no `KYC_Status__c` value).

Sources for every value below: the decision callback (`POST …/kyc/decision`),
`GET /v1/cases/{case_id}` and `GET /v1/cases/{case_id}/checks?all=1`.

## KYC_Case__c fields

| Salesforce field | Source in tool output | Mapping |
|---|---|---|
| `KYC_Status__c` | case `status` | `registered`* / `email_verification_pending`* / `email_verified`* / `enrichment_running`* → their labels; `kyc_pending` → "KYC Pending"; `manual_review_insufficient` → "Manual Review - Insufficient Score"; `account_approved` → "Account Approved"; **`approved_manual` → "Account Approved"** (see `Platform_Action_Taken__c`); `rejected` → "Rejected". *Pre-tool statuses are platform-owned (AUDIT:C1). |
| `KYC_Score__c` | callback `score` | integer, as-is |
| `Buy_Enablement_Status__c` | case `buy_status` | `not_applicable` → "Not Applicable"; `buy_locked_org_id_required` → "Buy Locked - ORG-ID Required"; `org_id_validation_pending`† → "ORG-ID Validation Pending"; `org_id_failed`† → "ORG-ID Failed"; `buy_enabled` → "Buy Enabled"; `buy_suspended` → "Buy Suspended". †Platform-derived transient states — the tool's callback only ever asserts `enabled` / `locked_org_id_required`; the platform may show finer-grained transitions between callbacks. |
| `Platform_Action_Taken__c` | callback `decision` + manual-approve event | `approve` → "Approve Account"; `approve_buy_locked` → "Approve Account - Buy Locked"; `reject` → "Reject" (or "Suspend" per platform policy); manual approve (no callback — the platform initiated it) → "Manual Approve" |
| `ORG_ID__c` / `ORG_ID_Status__c` | live `org_id_match` check | handle from check detail; status: none → "Pending"; `pass` → "Pass"; `fail` → "Fail"; superseded rows → "Superseded" |
| `POC_Handle__c` / `POC_Verification_Status__c` | live `poc_verified` check + review state | no check, no token → "Pending"; token sent (poc_tokens outstanding) → "Token Sent"; `pass` → "Verified"; `fail` → "Failed"; superseded → "Superseded" |
| `Business_Document_Status__c` | live `business_document_verified` check | none → "None"; uploaded but unprocessed → "Uploaded"; `pass` → "Verified"; `fail` → "Failed" |
| `Website_Review_Status__c` | review task / check | open task → "Open"; check `pass` → "Pass"; check `fail` → "Fail" |
| `Broker_Status__c` | case `broker_status` | `clear` → "Clear"; `allowed_broker` → "Allowed Broker"; `blocked` → "Blocked" |
| `Hard_Conflict__c` | callback `gates.no_hard_conflict` | boolean **negated** (`no_hard_conflict: false` ⇒ `Hard_Conflict__c = true`) |
| `Review_Reason_Codes__c` | union of live checks' `reason_codes` | delimited text / multi-select |
| `Manual_Approved_By__c` / `Manual_Approved_At__c` | manual-approve audit (platform initiated it; also in tool audit log) | reviewer id, timestamp |

## KYC_Check__c child records

One record per row of `GET /v1/cases/{id}/checks?all=1`:
`Check_Type__c` ← `type` · `Status__c` ← `status` · `Points__c` ← `points` ·
`Category__c` ← `category` · `Source__c` ← `source` · `Superseded__c` ←
`superseded_by_check_id IS NOT NULL` · `Reason_Codes__c` ← `reason_codes` ·
`Created_At__c` ← `created_at`.

## Delivery rules (05 + AUDIT:A6)

- Upsert by external ID = platform case id; never duplicate KYC Cases.
- The callback is at-least-once: **dedupe on (`case_id`, `run_id`)** — both are
  stable across redeliveries (verified by the tool's test suite).
- Order per case (queue per case id or version stamp); a Salesforce outage
  must never block platform enforcement.
- Salesforce KYC fields are read-only for non-integration users; no Salesforce
  automation may call back into the tool or platform KYC actions.
