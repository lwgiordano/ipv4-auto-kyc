# Architecture Decisions

A running log of significant decisions and their rationale. Newest first.

---

## ADR-009 — Shared configuration with future-run snapshots (2026-09-11)

The approved console configuration design uses immutable database revisions and
one active pointer, reusing the policy bundle store and per-run pinning. Each
revision includes the full broker list (stable IDs and notes) and destination
mappings. Admissions pin the active revision under the transaction lock; saves
affect new runs, including new runs for existing companies, never prior runs or
replays. Broker snapshots and per-run match provenance are pulled forward from
PR 10 into configuration `024`; its remaining scope stays in `029`. Pending
platform activation/revalidation/queue/evidence reservations are `025`–`028`.
Nothing enables platform ordering or M2, or changes callback/manual semantics.

Approved deviation `AUDIT:D-LIVE-CONFIG`: editable exact-integer points are 0–1000.
ORG-ID and POC per-type caps follow their edited single-check weight, including
matching derived description metadata, rather than retaining packaged 25-point
caps. Each type still counts once; threshold, gates, and evidence rules are fixed.
The normative package remains unmodified. Mapping saves affect destination keys
on subsequent projection reads, not values or Salesforce itself.

Activation is explicit and drained, requires pinning plus a nonempty admin token,
and refuses unfinished legacy work or invalid baselines. Schema installation
alone does not enable it. No historical broker snapshots are fabricated; recover
settings with new revisions, not pointer resets. See `docs/DEPLOYMENT.md` §12 for
the shipped CLI and recovery contract. Shared-token authentication and the entered
operator label are recorded separately; neither proves an individual's identity.

## ADR-008 — Local delivery evidence vs platform authority (PR 7b)

**Context.** PR 7b-core (migrations `013`-`023`) built an in-database witness
authority for decision callbacks: attempts are staged before transmission,
admission is stamped only by a trigger and only under an unexpired claim, a
terminal digest may be written only on the `pending → delivered` transition
and only when an ADMITTED attempt matches it, terminal rows are frozen, and
decision callbacks can be neither deleted nor re-identified. Two adversarial
audit rounds pressed on what that authority actually proves, and the loop had
to answer a question it had been leaving implicit: when the tool says
`delivery_witnessed`, what exactly is it asserting, and against whom?

The pressure came from two prescriptions, both formally rebutted and both
dispositions ACCEPTED by the reviewing agent:

- **R1 — an additional local terminal-provenance column** (`terminal_v1`),
  stamped by a trigger on a legal delivery, with all pre-existing terminals
  demoted to "unverified" pending reconciliation. Declined: the threat it
  addresses is an actor with raw SQL/DDL privileges, and such an actor can
  mint the new bit exactly as easily as the old one — provenance-on-provenance
  moves the trust question one level up without ever terminating it. The
  demotion would additionally re-label, retroactively, every row delivered
  under the already-fenced `015`/`016`/`017`/`018`/`019`/`020`/`021` path.
- **R2 — proving the prior authority before recreating it.** Declined for
  prior *execution* history, which PostgreSQL does not record anywhere: no
  catalog says what a function body DID between two migrations, and any check
  runs inside the same database whose integrity is in question. ACCEPTED for
  *present observable structure*, which is provable — and migration `018`
  implements exactly that carve-out.

**Decision.**

1. **`delivery_witnessed` is a LOCAL PUBLISHER 2xx ATTESTATION BOUND TO EXACT
   STAGED BYTES.** It is the strongest statement this database can make, and
   it is NOT proof of a receiver fact. A database cannot observe a socket; it
   observes that its own application code reported one. The taxonomy's other
   states (`send_intent_witnessed`, `legacy_unwitnessed`, `not_accepted`) each
   say precisely what local evidence does and does not settle, and
   `legacy_*` means "local history cannot prove this either way".
2. **The terminating authority is the platform's signed accepted-request
   ledger.** PR 7b-activation introduces it, and must reconcile every
   `delivery_witnessed` row against it, requiring digest and encoding
   agreement before platform authority is declared. A mismatch is a
   fail-closed `integrity_mismatch` terminal — never "nothing to reconcile".
3. **In-database authority defends against application defects and races**,
   which is what `013`-`023` do exhaustively. It does not defend against an
   adversary holding the database's own privileges, and the schema does not
   pretend otherwise.
4. **What IS provable at migration time gets validated, completely.** `018` and `022`
   pin the full observable surface of both authority tables — ordered columns
   with types, nullability and defaults; every named constraint's exact
   definition; every index's exact definition; the complete enabled trigger
   set compared as exact `pg_get_triggerdef` (timing, events, target relation
   and target function all live inside that one string); and each owned
   function's normalized-body digest together with its pinned `search_path` —
   refusing before any DDL. It claims nothing about earlier execution.

**Consequences.** Operators and the platform team get one honest sentence per
row rather than a confident one: the tool can say what it staged and what its
publisher observed, and it defers to the platform for what was accepted. PR
7b-activation's convergence contract is therefore not an optimisation but the
completion of this ADR — until it ships, reconciliation of a
`delivery_witnessed` row against the platform ledger is manual. The rebuttal
dispositions above are recorded so a future round does not relitigate them
from scratch; the boundary itself is restated in `AUDIT_FINDINGS.md`
(D-7bcore-boundary) and in `src/kyc_tool/outbox/witness.py`, which is the
module every reconciliation query goes through.

---

## ADR-005 — Per-run policy bundle pinning (PR 6)

**Context.** Three problems compounded around the policy bundle a run is
scored and decided under. (1) A run recorded the bundle it was **created**
under (`runs.policy_bundle_hash`, written once at ingest) but nothing
guaranteed the run was later **scored**, or its **decision recorded**, under
that same bundle: the worker used its own process-loaded bundle — whatever
was on disk when it last started — for both scoring and for the provenance it
stamped, so a run could silently be scored and decided under a bundle
different from the one it was created under, with no way to tell after the
fact. (2) No record existed of which **engine build** — the scoring, gate,
validator, and decision *code*, as distinct from the policy *data* — produced
a given decision; a code change to scoring/gate/decision semantics left no
trace on the decision row, so two decisions with identical policy shas could
still have been produced by different rules. (3) The bundle itself lived only
as bytes on disk at the moment a process happened to read them: nothing made
it durably reconstructable, so a past decision's exact rubric could not be
proven after the fact, and there was no way to reload a specific historical
bundle on demand (e.g. to reprocess an old run) once the on-disk files moved
on.

**Decision.** A DB-backed, content-hashed bundle store
(`policy_bundles`, keyed by `bundle_hash`, `files_json` base64-encoded, every
write and read reconstruct-verified so a corrupt row fails loudly rather than
silently drifting) replaces "whatever is on disk" as the durable source of
policy bundles. The pipeline **resolves the bundle to score under at job
entry** — before any adapter call, side effect, or token/email is created:
flag off, always the process-loaded bundle (byte-identical to pre-PR6); flag
on, the run's immutable creation pin (`runs.policy_bundle_hash`) loaded from
the store, with a miss raising `BundleUnavailable` and dead-lettering the job
with zero side effects rather than silently falling back to the process
bundle. Flag-on, **decision-time scoring is pinned to the resolved rubric**:
`score()`, `evaluate_gates()`, and the callback checks-summary re-derive
points and gate-category for **every live check** from the resolved bundle,
not from whatever value happened to be stamped on the row when it was
written — so a check that survived from an earlier, different bundle is
re-priced under the rubric the run is actually being decided under (immutable
check rows and validator PASS/FAIL themselves are untouched; re-judging
evidence is PR 6b). Every automatic decision **atomically** stamps both
bundle provenance (`checks.policy_bundle_hash`, `decisions.policy_shas`, the
audit `resolved_policy_bundle_hash`) and engine provenance
(`runs.engine_build_id` = `decisions.engine_build_id` = `ENGINE_BUILD_ID`) in
the same decide transaction, while `runs.policy_bundle_hash` — the creation
pin — is never rewritten. `ENGINE_BUILD_ID` (`domain/engine.py`) makes the
engine build an explicit, versioned identifier, held honest by a **framed
whole-tree guard test**: a sha256 over every `src/kyc_tool/**/*.py` file,
each framed as `relpath \x00 len(bytes) \x00 bytes` in sorted order, so a
rename, a byte-identical cross-file move, or an empty-file add all change the
digest and the whole source tree — not a curated list that could omit a file
— is in the closure. A durable, singleton **activation epoch**
(`bundle_pinning_epoch`) marks the point after which every check/decision is
expected to carry its provenance stamp, backing a per-surface alert for any
post-epoch row that doesn't.

**Rollout.** `Settings.enforce_bundle_pinning` defaults `false`. Phase 1
ships rolling: migration 011, the durable store, and bundle+engine
provenance recording go out first, without universal coverage (a rollover
old replica can still write NULL provenance). Migration 011 adds the three
provenance-column CHECKs `NOT VALID` — a brief, metadata-only lock with no
scan of existing rows — rather than a single validating `ADD CONSTRAINT`,
which would hold `ACCESS EXCLUSIVE` for a full table scan and stall every
ingest/decide writer until commit; a follow-on migration 012 then runs
`VALIDATE CONSTRAINT`, which needs only `SHARE UPDATE EXCLUSIVE` and does not
block concurrent reads or writes, so both steps stay hot-compatible on
production-sized `checks`/`decisions` tables. `ops.activate_bundle_pinning_epoch`
is the durable boundary that makes provenance trustworthy from that point
on — run only from the pinned release image, only after every old API and
worker is confirmed gone; it verifies the local engine build and process
bundle against the operator-supplied expectations, writes the epoch with
database time, and read-back-fails rather than silently no-op-ing if a
concurrent activation wrote different values. Phase 2 is a **drained**
worker-pool cutover, not a rolling deploy: `ops.verify_pinnable_backlog`
preflight (every runnable/requeueable run's bundle must resolve; seed any
gap with `ops.seed_policy_bundle`) → disable autoscaling/restarts and
confirm at the orchestrator that zero old workers remain → run
`ops.requeue_interrupted_jobs` (its precondition is "all workers confirmed
stopped") → enable the flag and start only flag-on workers, each logging a
startup attestation (`bundle_pinning_ready`: flag / bundle_hash /
engine_build_id) → resume, with the API staying up throughout. Rollback is
the same drained shape in reverse, flag-off, on the PR6 image — never a
resume on the pre-epoch writer, which would mint permanent post-epoch NULLs.
`docs/DEPLOYMENT.md` has the full operator sequence; `docs/RUNBOOK.md` has
the ops commands and the alert.

**Consequences.** Flag-off is a strict **scoring no-op** — golden cases stay
byte-identical to pre-PR6 — while still recording bundle **and** engine
provenance on every automatic and manual decision, so Phase 1 alone earns
audit value with zero behavior change. Once the epoch is active, a post-epoch
check or decision missing its provenance stamp is always an anomaly (a
rollover gap or a bypassed path), never an expected state, and is exactly
what the per-surface alert watches for. Migration 011's downgrade is
**forward-only after use**: once any `policy_bundles` row, either provenance
column, or the epoch row is populated, downgrade refuses rather than
deleting audit evidence to recreate the empty schema — matching the
precedent migration 010 set in ADR-003. Explicitly out of scope and left to
later units: revalidating pre-existing checks under a since-changed rubric
(PR 6b), immutable source-evidence containment (PR 8), and broker-state
reproducibility (PR 10).

---

## ADR-004 — Review-record binding (PR 5b)

**Context.** Two holes remained on the human-review path. On website
completion, the persisted `reviewer_id` was copied verbatim from the event
*payload*; the signed envelope's `actor` was never checked against it, so a
validly signed completion carrying the default `system`/`test` fixture actor
was accepted and could attribute a review to anyone, or to `""`. On manual
approve the reviewer *was* actor-derived, but the actor itself was
unvalidated: any `actor.type` was accepted, with no cross-check against the
payload's required `reviewer_id`. Separately, the PR 5a floor checked
`status == "open"` unlocked in the ingest transaction while the close
happened later, also unlocked, in the worker's decide transaction — two
completions admitted under different idempotency keys could both pass the
floor before either ran, so the check-write was decoupled from the close that
a second completion could still flip.

**Decision.** (1) **Trust model:** the signed request stays the trust
boundary (HMAC v2, PR 5a); tool-side reviewer authentication (OIDC) is a
future alternative, out of scope here. On top of that assertion, both
`website.review_completed` and `reviewer.manual_approve` now require
`actor.type == "reviewer"` and `actor.id == payload.reviewer_id`, both
nonblank after trimming whitespace and compared exact/case-sensitive; a
mismatch, a blank id on either side, or the wrong `actor.type` is rejected
**422** (authenticated but internally inconsistent — not a signature/permission
failure). This equality is a **consistency check**, not the security boundary
— the signature is. No global schema change follows: the nonblank-actor rule
is scoped to these two review events, not imposed on `Actor.id` generally.
(2) **Authoritative guard:** the ingest-time floor stays advisory and
unlocked (fast-fail UX, so a bad request gets an immediate rejection with no
orphan rows); the sole authority is the decide transaction, which locks the
**ReviewTask** `FOR UPDATE` — after the existing case-row lock, preserving a
fixed case-then-task lock order — and computes one immutable guard decision
(task exists · right type · right case · open · valid, matching actor),
re-validated against the **persisted** `event.actor_json` so a pre-upgrade
event queued under the old floor can't drain through unchecked after this
deploy. That one decision yields two views: a pipeline-internal guard holding
the live locked ORM task (consumed only by orchestration/side-effects, which
legitimately mutate it to close it) and a separate immutable scalar view
(`eligible`, `reviewer_id`, `task_id`, `skip_reason`) handed to the pure
validator — the locked ORM entity never crosses the validator boundary. An
ineligible completion (task no longer open, or a bad actor) still completes
as a normal run — never an early return — but writes no check, does not touch
the task, and records a `review_task.completion_skipped` audit entry; the
first *eligible* close whose decide transaction commits wins. (3)
**Persistence:** for an eligible website completion, `ReviewTask.reviewer_id`,
the check's `source`, and the audit `actor` all derive from
`event.actor_json["id"]` — the platform-asserted identity — never the payload
field; the signed payload itself is preserved unchanged on the event row as
evidence. `DecisionRow.reviewer_id` stays untouched (NULL, `manual=false`)
for automatic runs — that column is coupled to the Salesforce "Manual
Approved By" projection and belongs only to `reviewer.manual_approve`, which
continues to populate it, as today, from the same actor-derived identity. (4)
The ops-console composer refuses both event types with **403** in production,
so real reviewer actions can only arrive as signed platform events; in
dev/staging it now sends a genuine `{"type": "reviewer", "id": ...}` actor
instead of a generic `system` actor, so console testing exercises the same
binding production enforces.

**Rollout.** No migration, but a rolling deploy is unsafe: during any old/new
overlap an old replica still honors the exact forgery this decision closes
(an old API applies manual-approve inline with no actor floor; an old worker
claims jobs purely by kind, with no event-type filter, and can close a queued
`system`-actor completion under the old actorless semantics). There is no way
to keep an old replica serving traffic while guaranteeing it never touches a
sensitive event, so this ships as a **brief full maintenance window** — the
same non-hot stop → deploy → start pattern PR 5a used, extended to workers as
well as the API, rather than a rolling upgrade. `docs/DEPLOYMENT.md` §9 has
the operator cutover contract: build+digest the image first; pause all event
submission and edge-block the composer; stop the old API and worker pools
together with no graceful drain; run the digest-pinned interrupted-job
recovery one-shot; deploy the new API first with workers held at zero;
direct-probe bypassing the edge rule with side-effect-free requests; start
workers; resume. Its rollback verifies with non-mutating checks only
(`/readyz`, `/healthz`, prior-image digest) — the sensitive mutation probes
are prohibited against the prior (still-vulnerable) image, and the recovery
one-shot is re-run pinned to the last image that contains it.

**Consequences.** Pre-upgrade queued events with invalid actors are skipped
(audited) rather than honored once they reach the new guard — the intended
fail-closed outcome, not a regression. The maintenance window is a real
interruption: event submission is unavailable for its duration, which the
API contract does not itself guarantee lossless today (retry is directed only
for network failure, not for a load balancer returning 502/503/504 with every
target down) — treated as a platform prerequisite for the window, not a
contract change.

---

## ADR-003 — Per-case idempotency (D3) + path-bound HMAC v2 (PR 5a)

**Context.** Event idempotency was globally unique on `events.idempotency_key`,
and request signing (v1) covered only `{timestamp}.{body}` — not the `case_id`
carried in the URL path. Together these let a captured signed clean-company event
be replayed against a *different* case within the skew window. This is the
load-bearing reason the M2 enforcement kill switch stays frozen.

**Decision.** (1) **D3:** idempotency is `(case_id, idempotency_key)`; the same
key in another case is an independent event, never another case's replay
(cross-case reuse → two runs; same-case same-key different-payload → 409). (2)
**HMAC v2** binds method + full path + direction + key_id + timestamp + slot +
`sha256(body)`; the verifier is sticky (any v2 header ⇒ v2-only, no v1 fallback).

**Rollout.** Dual-accept with **independent** inbound/outbound sunset dates: the
inbound date is gated by a durable, fail-closed, cross-replica v1 **witness**
(zero v1 accepted across a configured window) activated post-cutover; the
outbound date by a v2-only staging callback E2E + sign-off (never inferred from
inbound telemetry). Migration 010 is **not hot-compatible** (the old image's
`ON CONFLICT (idempotency_key)` needs the global unique 010 drops) — deploy
**stop/migrate/start**; 010's downgrade is **forward-only after cross-case reuse**
(it refuses rather than deleting immutable audit events).

**Consequences.** The cross-case redirect closes for v2 traffic immediately and
for everyone once inbound v1 is disabled (the sunset takes effect); until then a
v1-only replay remains possible (documented residual risk of dual-accept). The
duplicate `/v1/review-tasks/{id}/complete` endpoint is retired and `request_nonces`
omitted (both recorded in `AUDIT_FINDINGS.md` D8). M2 remains gated on the full
platform cutover.

---

## ADR-002 — Resolving the post-audit architecture findings

**Context.** A max-effort code review surfaced a cluster of architecture-level
findings: single-source-of-truth (SSOT) violations where hardcoded tables
duplicate the normative policy bundle and can drift; a fail-open gap in the
RDAP compliance gate; a dead conflict-detection branch; and reliability/perf
gaps. This ADR records how each was resolved and why.

### Decision 1 — Guard the SSOT duplication; don't dynamically derive it

The run-plan table (`triggers._PLANS`), the full-run adapter order
(`FULL_RUN_ADAPTERS`), the gate vocabulary, and the console's run-state strip
all duplicate data that already lives in the loaded `PolicyBundle`.

The tempting "architectural" fix is to derive them from the bundle at runtime.
We rejected that: `_PLANS` encodes **per-event routing intent** (which adapters
run for `email.verified` vs `document.uploaded`) that is *not* present in the
catalog, so deriving it would either lose information or require re-encoding the
same intent elsewhere. Per "simplicity first / don't abstract everything," the
duplication is cheap and readable; the only real risk is silent drift.

**Resolution:** pin the duplication with CI guard tests
(`tests/policy_driven/test_ssot_guards.py`) that assert each table matches the
bundle. A spec change now fails a test instead of drifting into production.
Trade-off accepted: a spec change touches two files (the table + nothing, the
test just confirms) rather than one — but it can never silently diverge.

### Decision 2 — Collapse duplicated rules into one helper

The buy-enablement rule ("enabled only when a live ORG-ID passed") was encoded
three times, once as raw string literals in the manual-approve path.

**Resolution:** one `decision.buy_enablement_for(org_passed)` SSOT helper used
by both `decide()` and the manual-approve path; the string literals are gone.

### Decision 3 — Fix the conflict check at the right layer

`document_intent` read the registry's registration number from
`normalized.registry_detail`, which the OCR adapter never emits — so the
"document contradicts the registry" hard-conflict check was dead code and a
conflicting document could silently PASS (+25, a legal-proof gate).

**Resolution:** the registration number is authoritative on the **registry
check itself**. We exposed `source_detail` on `CheckView` (additive) and read
the number from the live registry check. The conflict check now works.

### Decision 4 — Fail visibly, not falsely, on the RDAP gap (#6)

Four of the five ORG-ID `needs_review` routing rules and the gate-5
`conflicting_entity` flag are consumed by the validator but never produced by
the real RIR strategies (only fixtures inject them), so those human-review
routes are unreachable in production — a fail-open on a compliance gate.

We deliberately did **not** fabricate RIR semantics we cannot verify (guessing
which RDAP statuses mean "not in good standing" risks wrongly failing
legitimate orgs). Instead the gap is made explicit and tracked: a prominent
`TODO(integration)` limitation note at the producer (`rir_rdap/base.py`),
noting that exposure is bounded because `org_id_match` still requires positive
name+address matching to PASS. Implementing the real detection is scoped to the
RIR integration work — the validator side is already ready.

### Decision 5 — Ship safe perf wins; defer risky ones

Added the missing supporting indexes (migration `007`) for the queries that
grow with retention and run on every console poll (audit feed, latest decision,
adapter latency) and for the queue-claim per-case subquery. Pure additive DDL.

**Deferred, with reason:** metrics query *windowing/caching* changes result
semantics and belongs in its own pass with dedicated tests; the **lease
heartbeat + per-case advisory lock** (a long run can outlive its 120s lease and
be double-claimed) is concurrency-critical and must be isolated and stress-
tested rather than bundled into a mixed commit. The reaper fix already makes
such a run fail *loudly* rather than zombie, and the `adapter_results` unique
constraint prevents duplicate check writes, so the residual risk is bounded to
wasted upstream calls on genuinely slow runs.

---

## ADR-001 — Original build

The engine's foundational decisions (policy-as-data with per-file sha
provenance, deterministic pure validation, append-only supersedable checks, the
Postgres job queue + transactional outbox, HMAC-signed both directions, and the
tool-scores / platform-enforces separation) are documented in
`docs/OVERVIEW.md` §11 and `AUDIT_FINDINGS.md`.
