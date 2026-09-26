# Integration Sheet

Release `__VERSION__` · source commit `__COMMIT__`

This build supports a closed staging integration. It is not approved for
production. Initial integration keeps `KYC_ENFORCE_POSITIVE_DECISIONS=false`:
a computed positive decision is held for registration-team review. A later
automation rehearsal requires both integration owners' approval and synthetic
sandbox accounts only. It never changes live permissions.

## What each system does

The platform sends signed events for each applicant and company. The tool
collects available evidence, records checks and sends a decision callback.
TechCraft builds the receiving ledger, reviewer screens and Salesforce writer;
the tool does not apply account permissions or write Salesforce records.

## Connect the staging systems

1. Agree separate staging URLs and signing keys for platform requests and
   tool callbacks. Use the v2 signing rules and exact examples in
   `docs/PLATFORM_INTEGRATION.md` §2. Keep secrets in the deployment secret
   manager, not this package.
2. Send events to `POST /v1/cases/{case_id}/events` with one idempotency key per
   logical event. Reuse that key when retrying the same event. A new event
   normally returns 202; 200 can mean a replay or an inline manual action.
3. Verify every callback signature. Commit the callback, its deduplication
   record and the resulting effective decision in one durable transaction
   **before** returning 2xx. A later automatic callback must not silently
   replace a recorded manual approval. The callback and receiver rules are in
   `docs/PLATFORM_INTEGRATION.md` §4.
4. Connect reviewer task handling and the public Salesforce projection. The
   platform owns the Salesforce writes, retries and reconciliation. See
   `docs/PLATFORM_INTEGRATION.md` §7 and
   `docs/SALESFORCE_MAPPING.md`.
5. Pass all eight receiver acceptance cases in `docs/PLATFORM_INTEGRATION.md`
   before an automation rehearsal. Record results for duplicates, delayed and
   conflicting callbacks, manual approvals and failed delivery. Follow the
   staging procedures in `docs/DEPLOYMENT.md`.

A held callback can say `decision=manual_review_insufficient` while
`buy_enablement=enabled`. That field reports ORG-ID eligibility, not permission
to buy. Keep both account approval and buying off while the decision is held;
receiver case A4 tests this exact combination.

Configure `KYC_UI_ADMIN_TOKEN` on every staging host, and enter it under
Options when the operator console is enabled. Console reads, including
configuration, and operator actions use that token. Platform request signing
uses different keys. Set `KYC_READ_AUTH_REQUIRED=true` on any staging host another
machine can reach, and never set `KYC_AUTH_DISABLED` there
(`docs/DEPLOYMENT.md` §2).

## Before production

Production provider wiring, ordered callback delivery, the platform receiver,
Salesforce reconciliation, document-authenticity policy and real-provider
staging tests are unfinished.
Migration 025 and its platform-owned ordering bootstrap have not shipped.
Do not infer callback order from timestamps or arrival order. Do not enable
automatic enforcement from a successful staging demo. The owners, open choices
and required evidence are listed in `docs/PRODUCTION_READINESS.md`.
