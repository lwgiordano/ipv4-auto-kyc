# Agent Brief — Build the IPv4.Global KYC/KYB Tool

You are the implementing engineer for the KYC Tool: a hosted service that scores business customers of the IPv4.Global marketplace and returns approval decisions to the platform. This brief tells you what to build, what is non-negotiable, and what your plan must contain.

## Mission

Build a production-grade KYC/KYB scoring engine that:

1. Is invoked by the IPv4.Global platform on customer lifecycle events (registration, email verified, ORG-ID submitted, POC submitted, document uploaded, KYB requested, recalculation).
2. Gathers evidence through eight adapters (one of which is a human review queue, not an automated fetch).
3. Validates evidence **deterministically** — exact, explainable match rules; never fuzzy inference for points.
4. Records every evidence result as an immutable, supersedable KYC Check.
5. Scores the case against a 100-point threshold and four hard gates.
6. Returns one of four decisions to the platform: `approve`, `approve_buy_locked`, `manual_review_insufficient`, `reject`.
7. Leaves enforcement to the platform, and leaves Salesforce as a one-way mirror the platform writes to.

## Read before planning

Read all of: `01_ARCHITECTURE.md`, `02_SCORING_AND_DECISIONS.md`, `03_ADAPTERS_AND_EVIDENCE.md`, `04_API_AND_DATA_MODEL.md`, `05_SALESFORCE_SYNC.md`, `06_IMPLEMENTATION_PHASES.md`, `07_TEST_PLAN.md`, and every file in `machine_readable/`. The JSON files are normative; if prose and JSON ever disagree, the JSON wins and you must flag the discrepancy.

## Non-negotiables

These are architecture decisions already made. Do not revisit them in your plan.

1. **The platform is the hub.** Every entry point is a platform event. The KYC Tool never initiates work on its own and never polls Salesforce for triggers.
2. **Salesforce is downstream only.** One-way sync: platform → Salesforce. No Salesforce flows, triggers, or callouts drive the tool. Salesforce field changes never cause tool runs.
3. **Manual approve happens on the platform.** A reviewer approves directly on the platform UI. It bypasses score and hard gates, the platform enforces it, and the result is synced to Salesforce. The tool records it for audit but does not gate it.
4. **Deterministic validation only.** A check passes on exact, auditable criteria (see pass rules). No LLM judgment calls, no fuzzy name matching for awarding points.
5. **Website verification is manual.** There is no automated website adapter. The tool creates a review task; a human marks pass/fail; pass awards +10. Build the queue, not a crawler.
6. **Broker rejection is exact-match only in v1.** The blocked list (Larus, Brander, InterLIR) rejects on exact identifier match. The allowed list (Silicon Desert, IP Trading, IPXO) is tagged and continues through scoring. There is **no risk-flag review path in v1** — fuzzy broker signals are a documented future extension (`02_SCORING_AND_DECISIONS.md §7`), not something you build now.
7. **Checks are immutable and supersedable.** Never update a check in place. New evidence creates a new check that supersedes the old one; score recalculates from live (non-superseded) checks.
8. **Floqer is discovery-only.** Its output can seed other adapters and support the LinkedIn check (+20 after deterministic match), but Floqer alone can never satisfy email, ORG-ID, POC, or registry points.
9. **ORG-ID and POC are the highest-accuracy checks.** ORG-ID validates against direct RIR/RDAP records. POC tokens go to the **RIR-listed email**, never the user-submitted one. Approval can happen without ORG-ID, but **buying cannot be enabled without a passed ORG-ID** (`approve_buy_locked`).
10. **Idempotency everywhere.** Every platform event carries an idempotency key; replays must not double-run adapters, double-create checks, or double-send POC tokens.

## Deliverables of your plan (plan mode output)

Your implementation plan must include:

1. **Stack proposal** — language, framework, queue, database, storage, deployment target. Default assumption: a single service with background workers and Postgres (the simple version in `01_ARCHITECTURE.md §6`) unless the repository you are in dictates otherwise. Justify deviations.
2. **Repository layout** — modules for API, event ingestion, adapter workers, scoring engine, decision engine, sync publisher, review queue.
3. **Data model migration plan** — tables from `04_API_AND_DATA_MODEL.md §4`.
4. **API surface** — endpoints from `04_API_AND_DATA_MODEL.md §2` with request/response schemas.
5. **Adapter build order** — respecting the phase gates in `06_IMPLEMENTATION_PHASES.md`.
6. **Test strategy** — implementing `07_TEST_PLAN.md`, with the golden cases as fixtures from day one.
7. **Milestone checklist** — one checkbox list per phase, each ending in its acceptance criteria.

## Rules of engagement while building

- Work phase by phase; do not start a phase before the previous phase's acceptance criteria pass.
- Write tests alongside each scoring or decision rule — the rubric and decision policy JSONs are the fixtures' source of truth.
- Secrets (RIR endpoints needing keys, Floqer, registry APIs, Salesforce credentials, email provider) come from environment/secret manager — never hardcode, never commit.
- Log every adapter call, check creation, supersession, score calculation, and decision with the case ID and event ID that caused it. The audit trail is a first-class feature: a compliance reviewer must be able to reconstruct any decision.
- If an external API contract is unknown (exact Floqer response shape, platform callback URL), stub it behind an interface, mark it `TODO(integration)`, and list it in the plan's open-questions section. Do not invent undocumented fields silently.
