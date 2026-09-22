# Integration Sheet

Release `__VERSION__` · source commit `__COMMIT__`

This build supports a closed staging integration. It is not approved for
production. Automatic approval remains off: a computed positive decision is
held for registration-team review.

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
5. Run the receiver acceptance cases in `docs/PLATFORM_INTEGRATION.md` and the
   staging procedures in `docs/DEPLOYMENT.md`. Record results for duplicates,
   delayed callbacks, manual approvals and failed delivery.

If the operator console is enabled, configure `KYC_UI_ADMIN_TOKEN` and enter it
under Options. Console data reads and actions use that credential; it is
separate from platform request signing.

## Before production

Production provider wiring, ordered callback delivery, the platform receiver,
Salesforce reconciliation and the real-provider staging tests are unfinished.
Migration 025 and its platform-owned ordering bootstrap have not shipped.
Do not infer callback order from timestamps or arrival order. Do not enable
automatic enforcement from a successful staging demo. The owners, open choices
and required evidence are listed in `docs/PRODUCTION_READINESS.md`.
