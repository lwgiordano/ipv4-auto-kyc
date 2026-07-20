# Architecture Decisions

A running log of significant decisions and their rationale. Newest first.

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
