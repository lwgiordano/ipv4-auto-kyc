# Salesforce Sync Mapping

The KYC Tool **never** reads from or writes to Salesforce. The platform mirrors
tool state one-way after every enforced decision and material check change.
This document translates the tool's callback and read APIs into the
`salesforce_sync_fields.json` field set. It also defines the value for
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
| `Hard_Conflict__c` | callback `gates.no_hard_conflict` | **Nullable boolean**, negated when present (`no_hard_conflict: false` ⇒ `true`). NULL occurs in exactly two conditions: there is no authoritative decision tuple because the pointer or legacy order is unresolved, or a manual approval bypassed the gates and they were never evaluated. The platform sync must carry NULL through and must not coerce it to `false`. Doing so would assert "no hard conflict" when no gate ran. The base `salesforce_sync_fields.json` type is `boolean`. This mapping adds the required nullable behavior. |
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
