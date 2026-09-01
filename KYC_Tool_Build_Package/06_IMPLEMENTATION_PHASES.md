# Implementation Phases

Each phase ends with acceptance criteria. Do not start a phase until the previous one passes. Adjust granularity in your plan, but keep the gate order: core loop before adapters, adapters before integration, integration before hardening.

## Phase 0 — Skeleton & contracts

Build: repo scaffold, service + worker processes, Postgres migrations for the full data model (`04 §4`), event ingestion endpoint with idempotency, run state machine with a no-op adapter, decision callback publisher (stub target), structured logging, config/secrets loading.

**Accept when:** posting the same `kyb.run_requested` event twice yields one run and identical responses; a run walks QUEUED→COMPLETE with a stub adapter; all tables migrate up/down cleanly; callback fires with a well-formed decision body.

## Phase 1 — Scoring & decision core (pure logic first)

Build: check store with supersession, scoring engine, hard gates, decision engine — all as pure functions fed by fixtures from `machine_readable/scoring_rubric.json` and `decision_policy.json`; the golden cases from `07_TEST_PLAN.md` as tests.

**Accept when:** every golden case passes; supersession removes points and cascades ORG-ID→POC correctly; decisions recompute deterministically from any set of live checks; 100% branch coverage on gates and decision enum.

## Phase 2 — Deterministic adapters (no paid sources)

Build: broker exact-match gate (with short-circuit), platform email validation, registry adapters (Companies House, GLEIF), document OCR pipeline, website **review queue** (task creation + completion event + check writing), POC token machinery (send to RIR-listed email via provider, verify, expire).

**Accept when:** a full run against recorded fixtures produces correct checks and decision for: clean UK company (registry pass), blocked broker (reject, adapters skipped), document match, website pass via simulated reviewer, POC token round-trip. Raw responses land in object storage; upstream errors mark runs partial without creating failing checks.

## Phase 3 — RIR & discovery adapters

Build: RDAP adapters for ARIN/RIPE/APNIC/LACNIC/AFRINIC with per-RIR quirks isolated, ORG-ID validation + needs_review routing, Floqer client (behind interface, recorded fixtures) + LinkedIn deterministic match, adapter orchestration order from `03`.

**Accept when:** ORG-ID pass/needs_review fixtures behave per pass rules; Floqer alone never creates point-awarding checks except LinkedIn after full deterministic match; buy-lock upgrade path works (approve_buy_locked → org_id pass event → approve with buying enabled).

## Phase 4 — Platform integration & manual approve

Build: real platform callback wiring, `reviewer.manual_approve` handling (audit, `approved_manual`, gates bypassed), review-task surfacing for the platform back-office, full dynamic loop (every event → affected adapters → supersede → rescore → redecide → callback), Salesforce mapping doc handed to platform team (`05`).

**Accept when:** end-to-end staging scenario passes: register → insufficient → add ORG-ID → approve_buy_locked→… (per golden case G7); manual approve bypasses gates and still yields buy-locked without ORG-ID; replayed callbacks dedupe on the platform side.

## Phase 5 — Hardening & operations

Build: rate limiting per upstream, dead-letter handling + alerts, metrics dashboard (runs, adapter latency/error rates, decision distribution, review-queue depth), retention jobs, load test (N concurrent runs), runbook.

**Accept when:** chaos test (RIR timeout, Floqer 500, queue backlog) degrades to partial runs without wrong decisions; dead-letters alert; p95 event→decision latency within agreed budget for adapter-light events (<10s) and full runs (<2min excluding human/website/POC waits).

## Deliberately later (post-v1 backlog)

- Risk-flag review decision + fuzzy broker signals (`02 §7`).
- Automated website analysis to pre-fill the manual review.
- Additional paid enrichment sources.
- Back-office UI beyond minimal review-queue endpoints.
