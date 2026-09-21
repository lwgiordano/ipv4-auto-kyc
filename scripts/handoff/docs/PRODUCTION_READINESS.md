# Production Readiness

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
closed staging environment.

Production work is still open in these areas:

- Select and wire the production POC directory and delivery path. The open
  choice is platform-owned delivery through a typed contract or tool-owned SES.
- Select and wire the production document path. The open choice is
  platform-extracted JSON or tool-side OCR with an approved provider.
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
