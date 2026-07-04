# Scoring & Decisions

Normative JSON: `machine_readable/scoring_rubric.json`, `machine_readable/decision_policy.json`.

## 1. Scoring rubric

Threshold: **100 points**, computed only from **live** (non-superseded) checks.

| Check | Points | Category | Source | Mode |
|---|---|---|---|---|
| Verified company email | +25 | control proof | platform email verification | automated |
| Official registry match | +25 | legal proof | Companies House / GLEIF / state-country registry | automated |
| ORG-ID match | +25 | control proof | direct RIR RDAP/Whois | automated |
| POC verified | +25 | control proof | RIR POC record + token to RIR-listed email | automated |
| Business registration document | +25 | legal proof | upload + OCR + field match | automated |
| LinkedIn tied to company | +20 | supporting | Floqer discovery + deterministic match | automated |
| Working company website | +10 | supporting | **manual human review** | **manual** |
| Verified email (any inbox) | +10 | account access | platform email verification | automated |

Caps and interactions:

- ORG-ID contributes at most 25; POC at most 25 (one live check each).
- `verified_company_email` (+25) and `verified_email` (+10) can coexist; a company-domain verification implies both criteria but only the applicable checks' points count once each.
- The website check is created as a **review task**; +10 only after a human marks pass.

## 2. Hard gates (all required for auto-approval; they override points)

1. Score ≥ 100.
2. **Legal business proof** — at least one live legal-proof check (registry match or business document).
3. **Control proof** — at least one live control-proof check (company email, ORG-ID, or POC).
4. **Broker status** — `clear` or `allowed_broker` (exact-match policy, §5).
5. **No hard conflict** — no live check contradicts another (e.g., registry entity ≠ RIR entity for the same submission).

## 3. Decisions (v1 — exactly four)

| Decision | Condition | Platform enforcement |
|---|---|---|
| `approve` | Score ≥ 100 AND all hard gates pass AND ORG-ID check passed | Approve account, enable buying |
| `approve_buy_locked` | Score ≥ 100 AND all hard gates pass AND no passed ORG-ID | Approve account, **lock buying** until ORG-ID passes |
| `manual_review_insufficient` | Score < 100 AND no hard block | Hold; case auto-clears on a later recalculation that passes §2 |
| `reject` | Exact-match blocked broker OR hard block | Reject / suspend account |

`manual_review_insufficient` is a **holding state, not a human queue**: no reviewer action is required; the next qualifying recalculation resolves it automatically.

## 4. Buy enablement

- Account approval does not require ORG-ID; **buying does**.
- `approve_buy_locked` → when an ORG-ID check later passes, recalculation upgrades the case: platform enables buying.
- If a live ORG-ID check is superseded by a failing one, the platform must be told to re-lock buying (decision recomputes on every event).

## 5. Broker policy (v1: exact match only)

Match on exact identifiers — legal name, known domains, email domains, RIR ORG-IDs, POC handles, ASNs (`machine_readable/broker_policy.json`):

- **Blocked (auto-reject):** Larus, Brander, InterLIR. On exact match: stop adapters, decision `reject`, do not spend further enrichment calls.
- **Allowed (tagged, continues):** Silicon Desert, IP Trading, IPXO. Tag `broker_status = allowed_broker`; still require full score + gates.
- **Everything else:** `broker_status = clear`; proceed normally.

No fuzzy matching, no broker-language heuristics, no "broker-like" scoring in v1.

## 6. Supersession & the dynamic loop

- Every evidence update creates a **new** check that supersedes the previous check of that type. Never mutate.
- If ORG-ID changes: supersede the old ORG-ID check (points removed), and revalidate any verified POC against the new ORG-ID — if no longer associated, supersede the POC check too.
- If the POC handle changes: expire outstanding tokens, supersede the old check, send a new token to the new RIR-listed email.
- Every event ends with: recalculate score → re-evaluate gates → recompute decision → return to platform. The loop runs until the case reaches `approve`/`reject` or sits in a holding state awaiting evidence.

## 7. Future extension (documented, not built): risk-flag review

v1 omits a `manual_review_risk_flag` decision because broker rejection is exact-match only — a case either rejects or passes through scoring. Add this path when the tool computes **non-deterministic risk signals**, which require a human rather than an auto-reject:

- Fuzzy name match to a blocked broker (rename/alias).
- Shared infrastructure with a blocked broker: same ASN, ORG-ID, POC handle, or domain.
- Broker-like language on the website or in Floqer data (IPv4 broker/leasing/marketplace/transfer vocabulary).
- A known broker already active on the platform.

Design consequence for v1: keep the decision engine a pure function returning an enum, so adding a fifth decision value plus its routing is additive (new signals in, new enum value out, one new platform enforcement branch). A risk-flag case must **not** auto-clear — that behavioral difference from `manual_review_insufficient` is the main reason it stays a distinct decision.
