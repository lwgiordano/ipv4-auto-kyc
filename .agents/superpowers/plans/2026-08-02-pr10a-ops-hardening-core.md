# PR 10a — Production ops hardening, core half (ROADMAP item 13, split)

**Split decision.** ROADMAP PR 10 bundles autonomous ops hardening with new product surfaces.
Following the project's own precedent (PR 5 → 5a/5b, PR 7 → 7a/7b-core/7b-activation), this unit
ships the core half now; **PR 10b** (own spec later) keeps: durable CHECKs and cutover-attestation,
`recalculate.requested` broker gate + ADR-007, `evidence.refresh_requested`
extension contract, the contracted M2 rollout-observation endpoint, and tree-wide `ruff format`
adoption + CI format-check (deferred deliberately: a ~139-file mechanical churn commit would pollute
the next audit's diff range; 10a's diff stays purely semantic).

## In scope (10a)

1. ~~Migration 024 CHECKs~~ **MOVED to 10b (spec-time discovery; reservations updated 2026-09-11):** `025` is reserved
   for 7b-activation (guard test `test_activation_live_contract_points_at_025_not_frozen_owners` +
   docs), and §C reserves `029` for PR 10's own migration. A 10a-only migration would steal a
   reserved number or fork the lineage against the reservation guards. The durable
   `outbox.attempts >= 0` / `jobs.attempts >= 0` / `jobs.max_attempts >= 1` CHECKs ride in 10b's
   migration 029. Broker snapshot DDL and per-run match provenance moved to PR Console
   Configuration (024) by the approved 2026-09-11 design. This keeps the other debts intact: the
   R4/R5 release notes name the runtime fail-closed guards as the authority until the durable layer,
   and Codex's R5 verify confirmed the deferred CHECK is "real scheduled work, not a release
   blocker."

2. **`adapters/retry.py` — transient classification + Retry-After.** One shared helper:
   connect/read timeouts, 429, 5xx ⇒ transient (bounded in-adapter retries, honoring a sane-capped
   `Retry-After`); other 4xx ⇒ permanent (fail fast — job-layer backoff must not hammer a permanent
   failure). Wire into the httpx-backed adapters (companies_house, gleif). Job-layer backoff already
   exists and is untouched.

3. **Bounded metrics windows.** The `/v1/metrics` latency percentiles must aggregate a fixed recent
   window (24h; index exists per ROADMAP) with the window declared in the response — not full
   history. Verify current state first; fix if unbounded.

4. **Prometheus exposition + alerts.** `GET /v1/metrics.prom`: hand-rendered text exposition of the
   same gauges (no new dependency), same read-auth as `/v1/metrics`. `docs/ALERTS.md`: the alert
   rules the DEPLOYMENT §5 budgets imply (dead-letter > 0, readiness failing, latency p95 over
   budget, outbox pending growth).

5. **RUNBOOK repair.** Delete the raw-SQL requeue blocks (the outbox one is broken post-7b-core: no
   claim-tuple clear, bypasses the 018+ transition authority; the jobs one duplicates the endpoint
   less safely) → point exclusively at the authenticated console requeue endpoints, with one line on
   WHY hand-SQL is not sanctioned. Governance test: no ```sql UPDATE jobs/outbox``` block remains in
   RUNBOOK.

6. **Supply-chain + hooks.** `requirements.lock` (pip freeze) consumed by the Dockerfile; base image
   pinned `@sha256`; `core.hooksPath` wired durably in `manage.sh setup`.

## Out of scope (10b or later)
Everything in the split list above; M2 stays frozen; migrations 013–023 + build package stay
byte-frozen.

## Task order (TDD each)
T1 migration 024 + lineage/guard/RUNBOOK-number fixes → T2 retry.py + adapter wiring → T3 metrics
window → T4 Prometheus + ALERTS → T5 RUNBOOK repair + governance test → T6 pins + hooksPath →
full-suite gate, bus note, push, CI green.
