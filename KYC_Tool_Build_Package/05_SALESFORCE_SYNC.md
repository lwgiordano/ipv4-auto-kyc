# Salesforce Sync (One-Way, Platform-Owned)

Salesforce is the system of record for humans — sales, compliance, reporting. It mirrors platform state. It does not drive anything.

## Rules

1. Direction: **platform → Salesforce only.** The KYC Tool never reads from or writes to Salesforce.
2. No Salesforce flow, trigger, Platform Event, or callout may invoke the tool or the platform's KYC actions. If such automation exists from the v1 concept, decommission it as part of rollout.
3. Manual approval is **not** a Salesforce action. Reviewers approve on the platform; Salesforce then receives the mirrored result. (Salesforce edits to KYC fields must be locked down to read-only for non-integration users.)
4. Sync happens after every enforced decision and after every material check change the platform receives from the tool.

## What the platform mirrors (field mapping)

Normative JSON: `machine_readable/salesforce_sync_fields.json`.

| Salesforce field (KYC Case) | Source of truth | Values |
|---|---|---|
| `KYC_Status__c` | decision | Registered, Email Verification Pending, Email Verified, Enrichment Running, KYC Pending, Manual Review — Insufficient Score, Account Approved, Rejected |
| `KYC_Score__c` | tool score | integer |
| `Buy_Enablement_Status__c` | buy_enablement | Not Applicable, Buy Locked — ORG-ID Required, ORG-ID Validation Pending, ORG-ID Failed, Buy Enabled, Buy Suspended |
| `Platform_Action_Taken__c` | platform enforcement | Approve Account, Approve Account — Buy Locked, Reject, Suspend, Manual Approve |
| `ORG_ID__c`, `ORG_ID_Status__c` | ORG-ID check | handle; Pending / Pass / Fail / Superseded |
| `POC_Handle__c`, `POC_Verification_Status__c` | POC check | handle; Pending / Token Sent / Verified / Failed / Superseded |
| `Business_Document_Status__c` | document check | None / Uploaded / Verified / Failed |
| `Website_Review_Status__c` | website review task | Open / Pass / Fail |
| `Broker_Status__c` | broker gate | Clear / Allowed Broker / Blocked |
| `Hard_Conflict__c` | gates | boolean |
| `Review_Reason_Codes__c` | check reason codes | multi-select / delimited text |
| `Manual_Approved_By__c`, `Manual_Approved_At__c` | manual approve event | reviewer, timestamp |
| `KYC_Check__c` (child object) | live + superseded checks | one record per check: type, status, points, source, superseded flag, timestamps |

## Delivery notes for the platform team

- Upsert by external ID = platform account/case ID; never create duplicate KYC Cases.
- Sync must be idempotent and ordered per case (queue per case ID or version stamp).
- Failures retry with backoff and alert; Salesforce being down must never block platform enforcement.
