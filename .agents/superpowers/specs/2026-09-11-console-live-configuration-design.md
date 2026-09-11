# Console live configuration and usability

Date: 2026-09-11

Status: design proposal for human review, not a build authorization. The human
approved shared, versioned live configuration and future-review-only activation.
The migration reorder in section 8 still needs approval. No runtime behavior,
existing migration, or ROADMAP reservation changes in this design commit.

## 1. Outcome and boundaries

An authorized operator can save scoring points, the Allowed/Blocked broker list,
and Salesforce destination field mappings from the console. A save is shared and
durable, survives process restarts, and is used by the service. Browser storage
is not an authority and an old preview is never automatically published.

Saving does not recalculate a company, rewrite a decision, supersede evidence,
send a platform event, or enable automatic positive decisions. New review runs
use the saved configuration; already-created runs keep their creation version.
This applies to a new run for an existing company as well as a new company.
Replaying an existing event keeps that event's original run and configuration.

Salesforce editing means destination names, not company values. The tool still
does not write to Salesforce. Saved mappings drive the service's field projection;
the external platform must consume that projection to change its actual sync.
Neither the UI nor this release may claim that a mapping save changed Salesforce.

The threshold, five hard gates, evidence pass/fail rules, identity invalidation,
manual-approval semantics, callback schema, and M2 hold remain unchanged. The
normative build package and migrations 013-023 remain untouched.

## 2. Current mechanisms and why this needs a backend unit

Source inspected at `8256d5e`:

- `policy/loader.py` loads seven files into a process-held bundle; its documented
  deployment model has no hot reload. `api/app.py` and both pipeline entry points
  load that bundle at startup.
- `policy_store/repo.py` already stores and verifies bundles by content hash.
  Its singleton pinning epoch is a write-once cutover record, not a mutable
  active-settings pointer. Do not repurpose it.
- `events/ingest.py` pins the passed policy to each new run. With bundle pinning
  enabled, `Pipeline.resolve_bundle` loads that run's bundle before doing work.
  With pinning disabled it uses process memory, which cannot support this promise.
- `BrokerGate` reads mutable `broker_entities` when called. A live edit there can
  affect a run that was already admitted. Full-list snapshots are required to keep
  the newly approved future-review-only behavior.
- `ui/salesforce_projection.py` uses fixed destination keys. The preview editor
  changes browser-local names only; it does not change this backend projection.
- Broker search interpolates `I.search`, but the `I` icon registry has no `search`
  entry. The visible `undefined` is a missing icon, not a broker API field.

Three approaches were considered:

1. **Recommended: database-backed versions and an active pointer.** Reuses the
   verified bundle store and per-run policy resolution, adds the missing active
   configuration and broker history, and works across processes.
2. File edits plus process restarts. Keeps the deploy-time model, but cannot offer
   immediate shared Save behavior across independently deployed processes.
3. Keep previews. Smallest change, but no longer meets the human's request.

## 3. Configuration version and save transaction

Use the existing Python/PostgreSQL application, not a new service or cache.
Introduce immutable configuration revisions and a singleton active-revision row.
Each revision contains a verified policy-bundle hash, a complete broker list,
destination mappings, schema version, parent revision, timestamp, change kind,
and audit attribution. Broker snapshots include stable IDs and notes, including
empty lists, so matches and non-matches are reproducible.

Each section saves independently. The server derives a complete candidate from
the active revision, replacing only the requested section. Clients cannot supply
an arbitrary complete policy bundle or alter hidden settings.

Proposed endpoints under the existing authenticated console API:

- `GET /ui/api/configuration`: active revision and editable sections.
- `PUT /ui/api/configuration/points`: exact check-type/point map.
- `PUT /ui/api/configuration/brokers`: complete reviewed broker snapshot.
- `PUT /ui/api/configuration/mappings`: exact source/destination map.

Every write includes the revision that was displayed and a unique request ID.
Validate the input, lock the active row, compare the expected revision, persist
the immutable candidate, advance the pointer, and record the audit event in one
transaction. The response is sent only after commit and includes the committed
revision. An unchanged submission is a no-op, not an empty version.

Concurrent edits based on an old revision return a conflict; neither browser
silently merges or overwrites the other's work. Preserve the local draft and
offer Reload. A request ID is persisted with its request digest and result in
the same transaction: an identical retry returns the original result; reuse with
different content refuses. Returning a previous result must also report the
current active revision if a later save has replaced it.

An ambiguous network result is not displayed as either success or cancellation.
Keep the submitted payload and request ID and offer a status check or identical
retry. Cancel discards unsent local edits only; it cannot retract a committed
save. Never reuse the composer endpoint for configuration writes.

## 4. Admission, scoring, broker evaluation, and field projection

New run admission reads the active configuration inside the event transaction
and records its revision together with the existing bundle hash. Acquire the
configuration-row shared lock before the case lock; saves lock no case rows.
This defines the boundary without using wall-clock timestamps: a run using the
old pointer finishes admission before the save can advance it, and subsequent
admissions see the new revision.

This applies to every event ingress, including synthesized console/review-task
events and dev-worker paths; passing a process-loaded bundle must not bypass it.
Manual record-only events retain their existing semantics and record the active
policy provenance without creating an automatic run or callback.

Workers resolve the run's immutable configuration before adapter calls. Missing,
corrupt, or inconsistent references refuse; never fall back to current settings.
Only immutable objects keyed by revision/hash may be cached. No worker has a
cached mutable "active" configuration. Live administration requires the existing
per-run bundle-pinning mode; the old flag-off process-memory path cannot be used
for newly versioned runs.

Broker matching consumes that run's complete saved list, preserving exact-match
normalization and Blocked precedence. Allowed never bypasses another gate.
Record matched entity and identifier class against the run's snapshot; an empty
match still retains the snapshot reference. Once activated, `broker_entities`
is a legacy/bootstrap source, not a second editable live authority.

Point edits produce a derived local policy bundle in the existing store, without
editing packaged JSON files. Only point weights change. For ORG-ID and POC, the
per-type maximum follows the edited single-check weight; derived cap metadata and
the corresponding policy description must agree. Each check type still counts
once. Record this user-approved deviation from the packaged 25-point defaults in
the implementation ADR/AUDIT amendment. No change to threshold or evidence rules.

Use the resolved rubric consistently for scoring, gates, and callback summaries.
Historical check rows stay immutable. Company displays must distinguish the
published decision from current evidence and use the appropriate run/decision's
rubric, not relabel an old score with today's point weights.

Mapping saves affect subsequent field-projection reads. The projection returns
the mapping revision and destination-keyed values; its value computation and
decision/manual-pointer authority remain unchanged. Old decisions and callback
bytes do not change. No new callback field is introduced. A persisted map is not
proof the external platform has adopted it; the page states that boundary quietly
without calling the service-side configuration a preview.

## 5. Validation, access, and failure behavior

- Writes require the configured admin credential, including in development; a
  permissive empty-token development route must not authorize live policy saves.
  Preserve the existing read authorization and check same-origin browser writes.
- Audit the authenticated mechanism separately from the operator-entered reviewer
  label. A shared admin token does not prove an individual's identity. Never log
  credentials, request authorization headers, or full secret-bearing settings.
- Strict write models forbid extra fields and coercions. Points are exact integers
  0-1000, with exactly the existing check-type set. Booleans are not integers here.
  The range is validation/help text, not permanent scoring-header clutter.
- Destination names use the existing 1-80 ASCII identifier contract and must be
  unique case-insensitively. Sources and source/value types cannot be edited.
- Carry forward broker limits: at most 200 brokers, name 1-200 characters, at most
  100 entries per identifier class, each entry 1-256 characters, notes at most
  2000 characters. Persist real stable IDs rather than row-index preview IDs.
  Reject duplicate IDs, normalized names, invalid policy values, and duplicate
  identifiers within an entry. Show actual matcher-equivalent overlap warnings;
  unrelated identifier classes must not create false overlap warnings.
- Bound configuration requests at 32 MiB in aggregate, in addition to field limits;
  apply the bound before decoding and never include the body in error logs. The
  existing ingress/proxy limit must agree with the route's documented limit.
- A bootstrap baseline exceeding the editor's limits refuses activation with a
  report; it is never truncated to fit. The limit can be revised in the build
  review based on real operator needs, not bypassed in a hidden import path.
- Lock waits and save transactions are bounded. Validation and conflict errors
  preserve edits and focus the relevant control. No saved indicator before commit.

## 6. Console changes

Keep the established visual system and both light/dark themes. This is a refinement,
not a new visual identity. Use shared control styles rather than page exceptions.

| Area | Required behavior and presentation |
| --- | --- |
| All pages | Remove standalone legends completely. Keep meaningful status pills, accessible labels, and contextual help. Consistent space below subheaders; inline small helper text baseline-aligns with its heading. |
| Buttons | Primary actions solid; secondary actions outlined/ghost. Save is left of Cancel. Preserve visible keyboard focus and disabled/loading states. |
| Scoring | Tooltip directly beside Scoring. Remove the permanent threshold/range sentence. Only Edit Points at the right of the header in read mode; Save/Cancel replace it while editing. |
| Broker rules | Title Brokers, not Broker Preview. Add Broker at right; entering add/edit mode replaces it with Save/Cancel. One entry form at a time; Save validates and commits the updated list, with no separate Apply Entry step. Row Edit/Remove remain available in read mode; removal needs confirmation and a versioned save. |
| Broker search | Supply a real search icon or omit it. Clear accessible search label, compact search/filter row, aligned input heights, no undefined text. All/Allowed/Blocked filters search the full returned snapshot. |
| Field mappings | Edit Mappings at right, replaced by Save/Cancel. No Reset to Live, Save Preview, or browser-preview banner after live saving is enabled. |
| Data Sources | Remove legend. Credential-variable names, optional/rate-limit notes, and call counts use smaller muted secondary text. Status pills and actions retain priority; keep contrast readable in both themes. |
| Rules metadata | Move full labelled Rules fingerprint and Copy into its own bottom section. Also show the active configuration revision; the policy hash alone does not identify a broker or mapping-only edit. |
| Company Actions | Rename Send Message in navigation, page title, links, help, and breadcrumbs; preserve the existing route. Align Technical event as a labelled read-only field alongside Action. Primary Review Message/Confirm Send precedes Reset Example. |
| Document details | Object Reference and Document Type labels, controls, and helper rows share the same grid structure and heights, including optional/required markers. |
| Individual company | Remove legend. Manual approval has appropriate solid action styling, distinct from passive status pills; actions wrap/stack without overlapping title/help on mobile. Do not change its record-only semantics. |
| Companies | Integrate the count with the heading/results toolbar. Display an accurate filtered total and a separate shown count when the list is limited. Add the filters below. |

Companies filters: All; New (no recorded decision yet); Awaiting review;
Approved; Buying locked; Rejected. New is explicitly labelled "Not yet decided"
in its help, not an invented date window. Use the existing authoritative pointer,
manual state, safety-hold, and unresolved-provenance taxonomy. A raw approve that
is held is not an effectively approved company. Implement filters on the server
before pagination; use the same predicate for count and rows. Search combines
with filters. Empty results state the filter and offer Clear filters.

Keep the guided composer's immutable review step, confirmed company identity,
response handling, and retained ambiguous-send history. Its separate timestamp/
idempotency backend defect is not included in this unit.

## 7. Cutover and recovery

Schema installation alone must not enable live editing. Initial activation is a
drained maintenance procedure: stop new event admissions and all relevant writers,
attest no running/pending legacy work, seed and verify the policy store, snapshot
the complete broker list including notes, create the baseline mapping/configuration,
and start only compatible API/pipeline workers with per-run pinning enabled.
Subsequent startup may verify/store its packaged baseline but must never reset an
existing active configuration pointer to that baseline.

Do not invent historical broker snapshots or attach today's list to old completed
runs. Legacy dead/retryable work is an activation blocker unless it can be drained
under the old behavior; ordinary requeue must not later admit an unversioned legacy
run into the new snapshot-only worker. The cutover preflight reports these rows
before the maintenance window, then rechecks under the writer fence.

Missing baseline/configuration prevents live saves and new admissions after this
feature is enabled. Read-only historical inspection remains possible with explicit
legacy provenance. The readiness check covers the active revision, bundle integrity,
and required pinning mode; it does not claim fleet attestation from one process.

Before activation, downgrade removes only unused new structures. After any
versioned run or live edit, refuse destructive downgrade; recovery restores a
previous configuration as a new version. Preserve all history. The runbook must
name a compatible recovery image and cannot promise old code is rollback-safe.

## 8. Proposed sequencing decision: requires human approval

The current reservation chain is 023 shipped, then 024 platform activation, 025
revalidation, 026 queue schema, 027 evidence storage, 028 PR 10. Platform activation
is blocked on external inputs. Shipping a new live-configuration schema now needs
an explicit change to that plan; no slot may be reused silently.

Recommended proposal: insert live configuration as 024 after 023. Move only the
unbuilt reservations one place: platform activation 025, revalidation 026, queue
schema 027, evidence storage 028, remaining PR 10 work 029. Pull the broker snapshot
and per-run match-provenance portion of PR 10 into this new unit. Leave its other
requirements assigned to PR 10. This proposal does not build or enable platform
activation, its wire field, input resolution, retirement capabilities, or M2.

If approved, update the canonical ROADMAP, pending specs/plans, executable
reservation/activation guards, and current operator references together before
the build. Preserve historical release records, frozen migration hashes, and
all existing fail-closed activation conditions. Tests must identify the activation
unit by its revised ownership, not accidentally apply its blocked gate to the new
configuration migration merely because it took the old number.

The alternative is to keep the chain and wait for the existing dependencies before
live saving ships. UI corrections can be built independently, but cannot advertise
live saving or remove honesty notices while the backend is still preview-only.

## 9. Implementation units and acceptance evidence

After design approval, write the TDD plan in the repository's existing plan folder.
Fresh implementer and independent review subagents per task; parent owns all git
and bus writes. Do not write concurrent changes into the shared console file while
browser captures run (the dev proxy reloads on changes).

1. Reservation/ADR alignment and the new schema, immutable versions, admission
   references, constraints, bootstrap, and refusal paths.
2. Authenticated versioned configuration APIs and real runtime consumers: admission,
   scoring, broker matching, and field projection, including restart behavior.
3. Console editors and the complete requested visual/filter refinement.
4. Cross-process/concurrency tests, operator documentation, browser visual review,
   whole-unit adversarial review, and release.

Mandatory negative proofs through the actual boundaries:

- Old in-progress run keeps old points AND broker snapshot after a save; a new
  event for the same company uses the new version. Replay preserves its old run.
- Two separately constructed API/worker instances observe the same committed state
  without restart; a restarted instance also reads the saved version.
- Two editors saving the same base cannot overwrite each other. A failed commit
  leaves neither an active pointer nor a misleading success audit. Lost-response
  retries produce one edit and distinguish its original result from current state.
- Unauthorized/direct writes, forged revisions, bool/float/string point coercion,
  omitted/extra check types, duplicate mappings, and malformed broker snapshots
  refuse. Cancel emits no request. A stale browser preview cannot auto-publish.
- Points actually change a later scored decision/callback summary, not just UI
  text; unchanged gates and M2 still prevent prohibited positive enforcement.
- Broker list edits reproduce both matches and non-matches; blocked precedence
  and exact-match semantics remain. Mapping saves change backend destination keys
  while preserving values; they send nothing to Salesforce.
- Legacy queued/dead work, missing configuration, wrong pinning mode, corruption,
  and preflight races exercise the stated cutover refusal rather than a test double.
- Filter counts match the full database population, including more than one page,
  manual approvals, holds, unresolved pointers, and zero results.
- Browser checks intercept event sends and use disposable state for admin-save
  tests; never change the user's configuration merely to prove the UI works.

Run targeted tests, local PostgreSQL 16 full suite, lint/import guards, and the
engine-source pin in each source-changing commit. Confirm exact source CI on
PR #2. Old test counts are not evidence for this unit.

Visual evidence: both themes; mobile, tablet, desktop, and actual user viewport;
read/edit/error/loading states and long broker/company names. One batched capture,
one correction pass, then an independent finish review using the user's screenshots.
No claim of completion until that review and the shared design documentation agree
with the built screens. This document itself ships no runtime or visual change.
