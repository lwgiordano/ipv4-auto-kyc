# Test Plan

## Principles

- The JSONs in `machine_readable/` are the fixture source of truth — generate rubric/decision tests from them, don't hand-copy values.
- All external calls replay from recorded fixtures in CI (no live RIR/Floqer/registry calls).
- Every golden case is an end-to-end test through the real event API against a test database.

## Golden cases (must pass from Phase 1 onward at the logic level, Phase 4 end-to-end)

| ID | Scenario | Expected |
|---|---|---|
| G1 | Clean UK company: company email verified (+25/+10), Companies House match (+25), website review pass (+10), LinkedIn match (+20), ORG-ID pass (+25) | score ≥ 100, all gates pass → `approve`, buying enabled |
| G2 | Same as G1 but **no ORG-ID submitted** | gates pass at 100 (email 25+10, registry 25, website 10, LinkedIn 20 = 90 → below!) — construct with document +25 instead: score ≥100 → `approve_buy_locked` |
| G3 | Only free email verified (+10) and website pass (+10) | score 20 → `manual_review_insufficient` |
| G4 | Exact blocked broker (Larus by ORG-ID) | `reject`; adapters after broker gate skipped; no further checks created |
| G5 | Allowed broker (IPXO exact match), full evidence like G1 | `broker_status=allowed_broker`, still scored → `approve` |
| G6 | Registry says active company but RIR entity is a different company (hard conflict) | gate 5 fails → not approvable regardless of points; stays `manual_review_insufficient` with `hard_conflict` reason |
| G7 | Dynamic loop: G3 state → `org_id.submitted` (pass +25) → `document.uploaded` (pass +25) → registry match event (+25) → crosses 100 with gates | auto-clears: final decision `approve_buy_locked`→`approve` as ORG-ID present |
| G8 | ORG-ID change after G1: new ORG-ID fails, old superseded; verified POC belonged to old ORG-ID | ORG-ID points removed, POC superseded too, score drops, buying re-locked |
| G9 | POC email hidden at RIR | no +25, review task `poc_email_unavailable` created, decision unchanged |
| G10 | Manual approve on a G3-state case (score 20) | `approved_manual`, gates bypassed, buy-locked (no ORG-ID), audit row with reviewer id |
| G11 | Duplicate event replay (same idempotency key) of G7's `org_id.submitted` | second post returns first result; exactly one new check exists |
| G12 | Upstream RDAP timeout during full run | run marked partial, prior checks intact, no failing check created, decision computed from available live checks |

(Note G2: pick evidence composition so the arithmetic works; the point is approval at threshold **without** ORG-ID → buy-locked.)

## Unit layers

- **Scoring:** table-driven from `scoring_rubric.json` — every check type, caps, category sums.
- **Gates:** all 2^5 combinations — only all-pass approves.
- **Decision enum:** property test — decision function is total and deterministic over its input space.
- **Supersession:** chains of 3+ checks; cascade ORG-ID→POC; same-transaction atomicity.
- **Validators per adapter:** pass/fail/needs_review fixtures per pass rule, including the needs_review routing list for ORG-ID.
- **Broker matching:** exact match on each identifier class; near-miss names must NOT match (fuzzy is out of scope and must stay out).

## Integration layers

- Event ingestion: schema violations 422; idempotent replays; per-case ordering.
- Callback publisher: retry/backoff on 5xx, dedupe semantics, payload schema.
- Review queue: task lifecycle open→done→check written→rescore fired.
- POC tokens: hash-at-rest, expiry, resend supersedes old token.

## Non-functional

- Load: sustained concurrent full runs (target set in plan) without queue starvation.
- Chaos: kill a worker mid-run — run resumes or dead-letters without partial-write corruption (checks+score+decision transactional).
- Security: unauthenticated event rejected; secrets absent from logs; PII scrubbing verified.
