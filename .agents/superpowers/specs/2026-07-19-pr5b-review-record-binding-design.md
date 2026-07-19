# PR 5b — Review-record binding (design, rev 1)

Status: awaiting Codex spec review → human sign-off → `writing-plans`.
Unit: ROADMAP item 11 ("Review-record binding"). Predecessor: PR 5a
(AUDIT-CLEAN at `2240fc5..28a7f7e`), which built the ingest validation floor and
explicitly deferred actor trust + task locking to this PR.

## 1. Problem

Two holes remain on the human-review path (`website.review_completed`,
`reviewer.manual_approve`):

1. **No actor trust.** On website completion the persisted `reviewer_id` is
   copied verbatim from the event *payload* (`side_effects.py:186`) — the
   signed envelope's `actor` is never checked against it. On manual approve
   the reviewer IS actor-derived (`ingest.py:245`) but the actor itself is
   unvalidated: any `actor.type`, blank or missing id (defaults to
   `"unknown"`), and no cross-check against the payload's required
   `reviewer_id`. `Actor.id` accepts blank strings (`api/schemas.py:28` — no
   nonblank constraint). Today a validly signed completion with the default
   `{"type": "system", "id": "test"}` fixture actor is accepted 202
   (`tests/integration/test_review_completed_event.py:33`) — a signed caller
   can attribute a review to anyone, or to `""`.
2. **The open-check races across transactions.** The PR 5a floor tests
   `status == "open"` unlocked in the ingest txn (`ingest.py:53`); the close
   happens later in the worker's decide txn (`side_effects.py:184`), also
   unlocked. Two completions with different idempotency keys both pass the
   floor before either run executes; the check-write (`validators/website.py`,
   emitted during VALIDATE from the *payload*) is decoupled from the close, so
   a second completion with a different result could flip the decided check.

## 2. Trust model (actor binding)

The **signed request is the trust boundary**: the platform asserts who acted
(HMAC v2 per PR 5a). Tool-side reviewer authentication (OIDC) is a future
alternative, out of scope. On top of that assertion:

- `actor.type` MUST be `"reviewer"` for both event types.
- `actor.id` and `payload.reviewer_id` MUST both be **nonblank** after
  stripping whitespace, and MUST be equal — **exact, case-sensitive** string
  comparison. A matching pair of blank strings authorizes nothing.
- Mismatch/blank/wrong-type → **422** (the request is authenticated but
  internally inconsistent — not 401/403, which are signature/permission
  failures).
- The `actor.id == payload.reviewer_id` equality is a **consistency check**,
  not the security boundary (ROADMAP wording). The boundary is the signature.

No global schema change: a nonblank constraint is **not** imposed on
`Actor.id` for every event (system/user actors elsewhere are out of scope);
the reviewer rules live in the floor/guard, so the contract change is scoped
to the two review events.

## 3. The authoritative guard (Option A, human-approved)

One guard, two call sites, one authority:

- **Ingest floor (fast-fail, advisory).** `_validate_website_review_completed`
  keeps its current checks and adds the §2 actor rules, so a bad request gets
  an immediate 404/409/422 and no orphan rows (`_FloorReject` rollback,
  unchanged). It stays **unlocked** — it is UX, not the authority.
- **Decide-txn guard (authoritative).** At the top of the decide transaction
  for a `website.review_completed` run, after `_load` (which already locks the
  **case row** `FOR UPDATE`, `pipeline.py:149`), acquire
  `SELECT … FOR UPDATE` on the **ReviewTask** — preserving the lock order
  **case first, then task** (the case lock already exists at `_load`; the task
  lock is always taken after it, and nowhere else, so no deadlock cycle).
  Compute **one immutable guard result** covering: task exists · task_type ==
  "website" · task.case_id == run's case · status == "open" · actor.type ==
  "reviewer" · nonblank exact actor/payload identity match — re-validating the
  **persisted** `event.actor_json` even though ingest checked it, because
  events admitted under the PR 5a floor (queued before this deploy) never
  passed the actor rules. Ingest-only validation would let those drain through
  unchecked.
- **Single source of eligibility.** The guard result (an immutable snapshot:
  `eligible: bool`, `skip_reason: str | None`, the locked task where eligible)
  is threaded to the validator via `_validation_extras` and to the closing
  side-effect. `website_intent` emits a check **only if** the guard says
  eligible; `on_event` closes **only** the same locked task the guard
  evaluated. Neither layer re-decides eligibility independently — the current
  duplicated payload-reads are removed.

An ineligible-at-decide event (task no longer open, or a pre-upgrade event with
a bad actor) follows §4.

## 4. Duplicate / ineligible semantics (precise no-op)

The queue is per-case FIFO (`queue/jobs.py:6` — oldest queued job per case),
so of several admitted completions the **lowest `event_sequence` wins**. A
later (or otherwise ineligible) completion's run:

- writes **no** `website_verified` check intent;
- does **not** supersede the winning check (no churn, no result flip);
- does **not** modify the completed task (no reviewer/result/timestamps
  overwrite);
- records a skip in the audit log (`review_task.completion_skipped`, with
  `reason` ∈ {`task_not_open`, `actor_invalid`, `task_missing`,
  `wrong_case`, `wrong_type`} and the skipped event id);
- **still completes as a normal run**: proceeds through the decide txn,
  recomputes score/decision from the (unchanged) live checks, and emits the
  standard decision callback. Explicitly NOT an early return — an early return
  would strand the job/run and break the "every event → its own run+callback"
  contract. The platform dedupes callbacks on `(case_id, run_id)` as today.

Failure atomicity: if the decide txn rolls back after the close/check were
staged, nothing committed — the task is still `open`, the retry re-runs the
guard cleanly, and the +10 is never lost.

## 5. Reviewer persistence (what field gets what)

For an **eligible website completion**:

- `ReviewTask.reviewer_id`, the check's `source` (`reviewer:<id>`), and the
  audit `actor` all derive from **`event.actor_json["id"]`** — the
  platform-asserted identity — not the payload field.
- The signed payload is preserved **unchanged** on the event row as evidence
  (no rewriting `payload_json`).
- `DecisionRow.reviewer_id` is **not** populated for automatic runs: that
  column is coupled to `manual=True` and the Salesforce "Manual Approved By"
  projection (`ui/salesforce_projection.py:135`); setting it on automatic
  website-completion decisions would corrupt the manual-approval projection.

For **`reviewer.manual_approve`**: the same §2 actor rules are enforced inline
before `_handle_manual_approve` (raising `_FloorReject` so a rejected approval
rolls back with no orphan event, same as the website floor), and the manual
decision row continues to carry the actor-derived `reviewer_id` as today —
that is the one place `DecisionRow.reviewer_id` belongs. Manual approve is
handled entirely in the ingest txn (no run), so ingest-time enforcement IS
authoritative for it; the pre-upgrade-queue concern in §3 does not apply.

## 6. Production `/ui` composer bar

- When `settings.environment == "production"`, the ops-console composer
  endpoint rejects `website.review_completed` and `reviewer.manual_approve`
  with **403** before ingest. The server-side 403 is the security boundary;
  hiding the controls in the console UI is optional UX polish, not security.
- In dev/staging the composer keeps working for these types but sends a real
  reviewer actor (`{"type": "reviewer", "id": <payload.reviewer_id>}`) instead
  of today's `{"type": "system", "id": "ops-console"}`, so console testing
  exercises the production binding rather than bypassing it.

## 7. Documentation (in scope — this changes the platform contract)

- `docs/PLATFORM_INTEGRATION.md`: reviewer actor requirements (`actor.type:
  "reviewer"`, nonblank `actor.id` == `payload.reviewer_id`) on **both**
  sensitive event payload tables, with the 422 semantics.
- `docs/RUNBOOK.md`: the production composer prohibition (403) and why.
- UI/console docs (`docs/OVERVIEW.md` console note or `RUNBOOK` §console):
  server-side 403 is the boundary; hiding controls is optional UX.
- `docs/architecture-decisions.md`: ADR entry for the trust model (signed
  platform-asserted actor; consistency-check equality; decide-txn authority).
- `.agents/ROADMAP.md`: PR 5b status on ship.

## 8. Testing

All against real Postgres (existing fixtures); reviewer actors passed
**explicitly** in tests — the shared `post_event`/`envelope` fixtures are NOT
taught to infer reviewer actors (defaulting would hide contract mistakes):

1. Actor floor (ingest): valid reviewer actor accepted; `actor.id !=
   reviewer_id` → 422; `actor.type != "reviewer"` → 422; blank/whitespace
   `actor.id` or `reviewer_id` (incl. both-blank-equal) → 422; rejected
   events leave no rows (rollback).
2. **Pre-upgrade queued event:** seed an event+run directly in the DB with a
   `system`/blank actor (bypassing the floor, as a PR 5a-era deploy would have
   admitted), execute the worker: the decide-txn guard skips it — no check, no
   task mutation, skip audit recorded, run completes with a callback.
3. Concurrency: two completions admitted before any worker execution (two
   runs); after both decide: exactly one close, one live `website_verified`,
   task fields from the winning (lowest-sequence) event's actor; the loser
   recorded the skip audit and produced a normal callback.
4. Conflicting duplicate: winner `result=pass`, loser `result=fail` — the
   live check stays PASS (no flip, no supersession).
5. Rollback: force a decide-txn failure after guard/close staging; task still
   `open`; retry completes cleanly (+10 exactly once).
6. Persistence: `ReviewTask.reviewer_id`/check `source`/audit actor ==
   actor.id (not payload); event `payload_json` unchanged; automatic
   decision rows have `reviewer_id IS NULL`/`manual=false`; Salesforce
   projection unaffected by an automatic completion.
7. Manual approve: same actor floor matrix; manual decision row carries the
   actor-derived reviewer; rejected approve leaves no rows.
8. Composer: production settings → 403 for both types (and only these types);
   dev settings → works, sends the reviewer actor, floor accepts it.
9. Full suite green.

## 9. Non-goals

- Tool-side reviewer authentication (OIDC) — future.
- POC-task (`poc_email_unavailable`) resolution — stays platform-side (v1).
- Any change to `KYC_Tool_Build_Package/` (normative, untouched).
- M2 — the enforcement hard stop is untouched; PR 5b does not gate or lift it.

## 10. Rollout

Code + tests + docs only. **No migration** (no schema change, no new task
states), so a standard rolling deploy — no stop/migrate/start. Behavior
change at the contract surface: completions/manual-approvals must now carry a
consistent reviewer actor; pre-upgrade queued events with invalid actors are
skipped (audited) rather than honored, which is the intended fail-closed
outcome.
