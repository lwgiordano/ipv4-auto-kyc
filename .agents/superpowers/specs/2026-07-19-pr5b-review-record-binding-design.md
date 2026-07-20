# PR 5b — Review-record binding (design, rev 8)

Status: **Codex AUDIT-CLEAN at rev 8** (`fc6802e..32c8130`, bus 2026-07-20) —
"no correctness, security, or conformance finding survives"; 8 review rounds
(findings 7→2→2→3→4→3→1→0), the actor/locking design frozen since rev 2 and the
tail all cutover/rollback ops precision. → **awaiting human spec sign-off** →
`writing-plans` → human plan gate.
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
   unvalidated: any `actor.type`, and no cross-check against the payload's
   required `reviewer_id`. `Actor.id` is *required* by the public schema
   (`api/schemas.py:28-31`; `routes_events.py` validates the envelope before
   ingest), so a **missing** id can't arrive over signed HTTP — but the schema
   permits **blank/whitespace** ids, which is the live hole. Today a validly
   signed completion with the default
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
- **Single source of eligibility, two derived views.** The guard decision is
  computed once. It yields **two** views, both derived from that one decision:
  - a **pipeline-internal guard** holding the live locked `ReviewTask` ORM
    object — consumed only by orchestration/`side_effects` (which legitimately
    mutate the task to close it); it never crosses the validator boundary.
  - a separate **immutable scalar view** — `eligible: bool`, `reviewer_id: str
    | None`, `task_id: str | None`, `skip_reason: str | None` (plain strings/
    bools, no ORM entity) — passed to the pure validator via
    `_validation_extras`. This respects the `AGENTS.md` pure-validator boundary
    (`ValidationContext` is read-only data): a frozen wrapper does NOT freeze a
    contained SQLAlchemy entity, so the ORM task must never be handed to a
    validator.
  `website_intent` emits a check **only if** the scalar view says eligible;
  `on_event` closes **only** the locked task in the internal guard. Neither
  re-decides eligibility — the current duplicated payload-reads are removed.

**Broker-blocked short-circuit (live path).** `website.review_completed` runs
the broker gate (`triggers.py:39`); a broker-blocked case hops straight to
DECIDE (`pipeline.py:195-196`), and `_decide_txn` builds validator intents only
when `from_state is VALIDATE`. Left as-is, a blocked case would close the
eligible task with no `website_verified` check (or, if the close were gated on
the intent, strand an accepted completion open). Fix: for
`website.review_completed`, the authoritative guard **and** the `website_intent`
emission run on the DECIDE short-circuit too (all *other* validators stay
skipped) — so task-close + the +10 check still commit together even when the
decision remains `reject` (blocked). §8 adds a blocked-broker test proving
task+check commit atomically while the decision stays reject.

An ineligible-at-decide event (task no longer open, or a pre-upgrade event with
a bad actor) follows §4.

## 4. Duplicate / ineligible semantics (precise no-op)

The queue is per-case FIFO (`queue/jobs.py:6` — oldest `queued`/`running` job
per case). The winner invariant, stated precisely: **the first ELIGIBLE task
close whose decide transaction commits wins.** Neither "lowest sequence" nor
"first committed run" is exact, because two kinds of earlier runs can commit
*without* winning:

- an **ineligible** earlier completion (e.g. a pre-upgrade `system`-actor
  event, seq-1) commits its audited-skip run and callback while leaving the
  task **open** — a later valid completion (seq-2) then legitimately closes
  it (FIFO releases seq-2 once seq-1's job is done, `jobs.py:37-41,93-98`);
- an earlier **dead-lettered** completion never commits its close at all: it
  exhausts `max_attempts` (`jobs.py:101-108`), its decide txn rolled back
  (task still open — §4 atomicity), and the later completion becomes claimable
  and closes.

In the common path (all admitted completions eligible, none dead-lettered) this
reduces to "lowest `event_sequence` wins". A later-or-otherwise-ineligible
completion's run:

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
- `docs/architecture-decisions.md`: **ADR-004** for the trust model (signed
  platform-asserted actor; consistency-check equality; decide-txn authority).
  The latest ADR is 003, but ROADMAP `:245-252,274-276` already **reserves
  ADR-004** for the PR 10 recalculate broker-gate deviation — so this PR pins
  PR 5b = **ADR-004** and, in the same ROADMAP edit, moves that future
  reservation to **ADR-005** (avoiding two ADR-004s under the next-number
  convention).
- `.agents/ROADMAP.md`: PR 5b status on ship + the ADR-004→005 reservation move.

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
3b. **Ineligible-then-valid:** seed seq-1 with a `system` actor (invalid) and
   seq-2 with a valid reviewer for the same open task. Seq-1's run commits the
   audited skip (task stays open, no check); FIFO then releases seq-2, which
   legitimately closes the task. Proves "first ELIGIBLE close that commits
   wins" — the earlier committed-but-ineligible run does not.
4. Conflicting duplicate: winner `result=pass`, loser `result=fail` — the
   live check stays PASS (no flip, no supersession).
4b. **Winner dead-letters:** admit seq-1 PASS and seq-2 FAIL; force seq-1
   through `max_attempts` to dead-letter (its decide txn rolls back, task stays
   open); run the worker again — seq-2 becomes claimable and legitimately
   closes the task FAIL. The dead-lettered earlier completion never committed a
   close, so the invariant holds: first eligible close that commits wins.
4c. **Blocked-broker completion:** complete an open website task with a valid
   reviewer actor while the case is broker-blocked (DECIDE short-circuit) — the
   guard + `website_intent` still run: the task closes and the +10 check is
   written atomically, while the decision stays `reject`.
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
8b. **Interrupted-job recovery command** (`ops.requeue_interrupted_jobs`):
   (i) a job left `running` on its **final attempt** is requeued without
   consuming the forced-stop attempt — NOT dead-lettered — and later reaches
   the §3 guard normally; (ii) a job `running` under a **still-unexpired
   lease** is ALSO requeued (the command treats every `running` row as
   interrupted regardless of lease time — the caller guarantees all workers are
   stopped); (iii) idempotent — a second invocation is a no-op; (iv)
   asserts/reports zero `running` jobs on completion.
8c. **Guard-behavior canary — a test / staging gate, NEVER a production probe.**
   Seed a pre-upgrade `system`-actor `website.review_completed` event+run
   directly, run the decide path, assert `completion_skipped` audit + task still
   `open` + no `website_verified` check (this is test 2, re-used as the
   pre-window verification the cutover runs against the exact image digest in
   staging / an isolated DB). It must NOT be run against production data: a real
   run writes a decision + enqueues a callback unconditionally and the outbox
   publisher would deliver it.
9. Full suite green.

## 9. Non-goals

- Tool-side reviewer authentication (OIDC) — future.
- POC-task (`poc_email_unavailable`) resolution — stays platform-side (v1).
- Any change to `KYC_Tool_Build_Package/` (normative, untouched).
- M2 — the enforcement hard stop is untouched; PR 5b does not gate or lift it.

## 10. Rollout — brief full maintenance window (NOT a rolling deploy)

No migration, but a rolling deploy is **unsafe** for this change: during old/new
overlap an **old** replica still honors the exact forgery PR 5b closes — an old
API applies `reviewer.manual_approve` inline with no actor floor
(`ingest.py:195-199,233-257`), and an old worker (which claims purely by job
**kind**, `queue/worker.py:42-61`, no event-type filter) closes a queued
`system`-actor website completion with the old actorless semantics
(`validators/build.py:47-48`, `side_effects.py:181-197`). There is no way to
keep old replicas serving *any* traffic while guaranteeing none of them touches
a sensitive event. So PR 5b ships as a **brief full maintenance window** — the
same non-hot **stop → deploy → start** pattern PR 5a used and
`docs/DEPLOYMENT.md` §4 already documents — which removes old/new overlap
**entirely**, collapsing the whole class of cutover-ordering hazards.

**Interruption is explicit, not hidden.** For the window, `POST
/v1/cases/{case_id}/events` (`routes_events.py:17-50`) — the entry for *every*
event type — is unavailable, and the pipeline is stopped. This is a real
interruption, not a seamless roll. The "no loss" guarantee is a **platform
prerequisite**, not a property of the current contract: today the contract only
directs retry on a *network failure* (`PLATFORM_INTEGRATION.md:131-143`), but a
load balancer with every API target down returns **502/503/504**, which is not
covered — and a delayed retry that reuses the original signature blows the
300-second HMAC skew (`security.py:79-91`). So the window requires the platform
to **pause/buffer ALL event submission** for its duration and drain afterward
(re-signing each retried body with a **fresh timestamp/signature**, same
idempotency key). Equivalently, if the platform prefers to keep sending, it must
treat 502/503/504 **and** transport failure as retryable with the same body/key
and a fresh signature — stated and tested as a platform prerequisite. Either
way, work queued before the window resumes when the new workers start.

Cutover contract (added to `docs/DEPLOYMENT.md`, in scope):

0. **Before the window:** build and publish the reviewed image and record its
   **digest**. Every task run below (the recovery one-shot, the new API, the new
   workers) is pinned to that one digest — the recovery module
   (`kyc_tool.ops.requeue_interrupted_jobs`) exists only in the new image, so a
   step that runs on the still-current old task definition would exit
   `No module named …`.
1. **Pause ALL platform event submission** (not only the two sensitive types)
   and the ops composer — the whole window is a maintenance pause, and per the
   interruption note a partial pause can't guarantee no-loss. Concretely:
   platform stops sending events (buffers them) and the composer route
   (`POST /ui/api/send-event`) is edge-blocked / old replicas get
   `KYC_UI_ENABLED=false` (an operator on an old replica could otherwise post an
   inline `system`-actor manual-approve, `ui/routes.py:355-391`).
2. **Stop ALL old processes together — APIs and pipeline workers as ONE
   coordinated action, no graceful drain, do not await either pool before
   signaling the other.** Confirm both pools are at **zero** before continuing.
   Stopping them in sequence would leave the un-stopped pool live and able to
   commit a forgery during the gap; the edge block can't revoke a request
   already in an old API threadpool, so old APIs must be *stopped*, not drained.
   Crash-equivalent termination is safe by design: each transition (and ingest)
   commits in one transaction, so interrupted work rolls back.
3. **Recover interrupted jobs — run `kyc_tool.ops.requeue_interrupted_jobs` as a
   one-shot task PINNED TO THE §0 DIGEST**, after both pools are confirmed at
   zero and before any new worker starts. With all workers stopped and none
   restarted, **every `status='running'` row is by definition interrupted**
   (no worker registry is needed — the procedure guarantees no worker is alive),
   **regardless of lease expiry**. It requeues that whole set transactionally
   **without consuming the forced-stop attempt** (operator-initiated
   termination, not a handler failure — otherwise an expired *final*-attempt job
   would be dead-lettered by the passive reaper, `queue/jobs.py:128-150`, and
   never reach the §3 guard), and asserts zero `running` on completion. It MUST
   NOT run while any worker is live — the "both pools at zero" gate is its
   precondition.
4. **Deploy new API and worker services, both pinned to the §0 digest** (attest
   it). Bring up the new **API** first and keep **workers at zero** until step 5
   passes — workers serve no HTTP (`DEPLOYMENT.md:15-23`), so the API probes
   below cannot vouch for a stale/mismatched worker image, and starting workers
   before verification would let one honor a recovered pre-upgrade completion.
5. **Direct-probe each new API replica** via a trusted path that bypasses the
   edge rule (internal target-group address / port-forward) — probing through
   the edge would let the LB's own 403 falsely certify a broken app. These
   probes are **side-effect-free** (each is rejected at the ingest floor →
   `_FloorReject` rollback, no rows). Assert application-identifying response
   **bodies**, not just status: a signed `system`-actor completion on a **valid
   open task** → the app's 422 (a 404/409 must not mask the actor floor); a
   mismatched-actor manual-approve → the app's 422; the composer → the app's 403
   for both sensitive types. The decide-guard *behavior* is proven **before the
   window in staging** (§8·8c), not by a live production canary — a real
   pipeline run in production would write a decision + enqueue a callback
   unconditionally (`pipeline.py:386-408`) and the still-running outbox
   publisher (a separate process, NOT stopped in step 2, `DEPLOYMENT.md:15-19`)
   would deliver it to the real platform. So: run §8·8c against the exact §0
   digest in staging/an isolated DB pre-window, and attest that same digest here.
6. **Start the new workers** (they now claim the recovered queue under the §3
   guard), then **resume** event submission — unpause the platform and unblock
   the console route.

Rollback mirrors the same window and MUST pin the recovery one-shot to the last
image that still contains it: pause all submission, stop ALL new processes
together (same coordinated hard stop + the same recovery command on a
digest-that-has-the-module), redeploy the prior image for API **and** workers,
verify, start workers, resume — accepting that the prior image restores the
pre-PR 5b behavior. **Rollback verification is NON-MUTATING only** — `/readyz`,
`/healthz`, and prior-image digest attestation. The step-5 sensitive mutation
probes are **prohibited** against the prior image: that image is the current
vulnerable code, with no actor floor, so a mismatched-actor `manual_approve`
probe would `approve` the case inline (`ingest.py:195-199,233-267`) and a
`system`-actor completion probe would queue a run the restored old worker can
honor (`:201-228`) — the probe would *perform* the forgery, not detect it. If
prior behavior must be exercised, do it in staging / an isolated DB; all
submission and the composer stay blocked until the safe checks pass.

Behavior change at the contract surface: completions and manual-approvals must
now carry a consistent reviewer actor, and pre-upgrade queued events with
invalid actors are skipped (audited) rather than honored — the intended
fail-closed outcome.
