# Architecture Decisions

A running log of significant decisions and their rationale. Newest first.

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
