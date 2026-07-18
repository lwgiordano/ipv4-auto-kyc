# PR 5a — HMAC v2 + per-case idempotency (design)

- **Date:** 2026-07-18
- **Status:** Design **rev 2** — revised after Codex's spec review (`b1e0ea5`,
  4 findings, all accepted) and the human's decision on finding 1 (retire the
  duplicate review-complete endpoint). Pending human spec re-review, then
  `writing-plans`, then the plan gate.
- **Unit:** ROADMAP §G "PR 5a — HMAC v2 + per-case idempotency (item 6)",
  migration 010, decision D3. Thin-delta pointer: restates only what §G left
  open + the review-driven revisions; everything else is locked by
  `.agents/ROADMAP.md` §G/§B.

## 1. Why (locked — ROADMAP §D, the M2 hazard)

v1 signs `{timestamp}.{body}` with one shared secret. `case_id` is in the
**unsigned path**, so a captured signed clean-company event can be redirected to
another case within the 300s skew and auto-approve it. D3's per-case idempotency
widens that hole (keys become reusable across cases) — so the canonical-path
binding and D3 land in the same PR.

## 2. The v2 contract (locked by §G; concretized)

**Canonical v2 signed value** — newline-joined, HMAC-SHA256, hex:

```
v2 \n key_id \n direction \n method \n raw_path+query \n timestamp \n slot \n sha256(body)
```

- `direction`: `platform->tool` (inbound) or `tool->platform` (callbacks) — a
  captured inbound signature can't be replayed outbound.
- `raw_path+query`: the literal request target as sent (path + `?query` if
  present; no re-ordering/re-encoding). **This binds `case_id`, closing the
  hazard.**
- `slot`: the idempotency key for event POSTs; **empty** for reads and
  callbacks. (No nonce slot — see §4/§5: the only keyless mutating op is retired.)
- `sha256(body)`: hex digest of the raw body; empty body hashes normally.

**Endpoint matrix (rev2, replaces the generic "key-else-nonce" rule — Codex
finding 1):**

| Surface | `slot` | Rule |
|---|---|---|
| `POST /v1/cases/{id}/events` | idempotency key | key **required** |
| `GET` reads, outbound callbacks | empty | — |

There is no nonce row: the review-complete endpoint is retired (§4).

**Headers:** v2 = `X-KYC-Signature-V2` + `X-KYC-Key-Id` + existing
`X-KYC-Timestamp`. v1's `X-KYC-Signature` is untouched during dual-accept.

**Split secrets + key_id (locked):** separate inbound/outbound secrets;
config `hmac_inbound_key_id`/`_secret` (+ optional extra-keys map for future
rotation) and `hmac_outbound_key_id`/`_secret`. Legacy `platform_hmac_secret`
stays the v1 secret until sunset.

**Signature retry semantics (locked):** signatures are NOT blanket single-use;
a same-(case, key, payload) retry returns the stored response snapshot.

## 3. Open items → approved resolutions (unchanged from rev 1)

1. **v1 sunset → config-dated, required in production.** `hmac_v1_sunset_at`
   (ISO-8601): before it, dual-accept; at/after, v1 rejected.
   `production_config_violations` **requires it set in production** (that is how
   §G's "fixed sunset" is enforced). Staging default: unset. Date set with
   TechCraft at the M3 cutover.
2. **Outbound leg → dual-emit.** During dual-accept, callbacks carry BOTH the v1
   header and the v2 header set (each signed with its secret); the platform
   verifies whichever it supports and migrates without a coordinated flip. v1
   emission stops at sunset.

## 4. Retire the duplicate review-complete endpoint (rev2 — finding 1)

**Decision (human):** retire `POST /v1/review-tasks/{id}/complete`. Confirmed
TechCraft has not integrated or committed to it. Rationale:

- The event API is the canonical platform mutation surface; the ops console uses
  `/ui/api/send-event` (not this endpoint); the endpoint duplicates a transition
  that already exists as the keyed event `website.review_completed` (defined in
  `platform_events.json`, in the envelope union `api/schemas.py:20,110`, with a
  run plan `orchestration/triggers.py:39` — the endpoint only ever synthesized
  that same event).
- Retiring it removes the **only** keyless state-changing op, so **migration 010
  omits `request_nonces`** — a deliberate, recorded deviation from ROADMAP §G
  ("add request_nonces"): shipping an unused security mechanism is YAGNI. Record
  in `AUDIT_FINDINGS.md`.
- Keeping it via `Idempotency-Key` was explicitly rejected: it preserves the
  duplicate surface and gives the key ambiguous semantics (the op is naturally
  deduped by task id).

**Now the dev-facing rule is genuinely uniform:** every platform mutation is a
keyed event POST; reads/callbacks carry an empty slot; the platform never deals
with nonces. `docs/PLATFORM_INTEGRATION.md §7` drops the endpoint and documents
review completion as the `website.review_completed` keyed event; the signing
quickstart (copy-paste function + worked vectors; staging logs the server-side
canonical string on a mismatch) covers the one recipe.

**Parity (plan-time verification):** routing completion through the keyed-event
path must preserve the endpoint's current validation (result ∈ {pass, fail},
reviewer_id present, task/case/status resolution). The plan confirms
`WebsiteReviewCompletedPayload` + the pipeline enforce equivalent checks before
the route is removed; **full task/actor/status/result binding is PR 5b**, which
now operates on the shared event pipeline.

## 5. Per-case idempotency (D3, locked) — migration 010

- Drop global `uq_events_idempotency_key`; add
  `uq_events_case_idempotency (case_id, idempotency_key)`; retarget ingest's
  `ON CONFLICT`. Cross-case reuse → two independent runs (never another case's
  run id); same-case same-key different-payload → 409 (behavior unchanged, scope
  narrowed). Record in **ADR-003 and `AUDIT_FINDINGS.md`**.
- **No `request_nonces`** (§4).
- **Downgrade is forward-only after cross-case reuse (finding 4):** once 010 has
  admitted `(case-A, key-X)` and `(case-B, key-X)`, the old global-unique cannot
  be recreated. `downgrade()` runs a **duplicate preflight** and, if cross-case
  duplicates exist, **refuses with an actionable error** (release-note warning)
  rather than deleting/renaming immutable audit events to force success. The
  fresh-DB `upgrade→downgrade→upgrade` still passes; a seeded-duplicate test
  asserts the refusal.
- **§C migration gate:** `down_revision = 009`; rebased onto the live head at
  merge; fresh-DB round trip clean.

## 6. Sunset telemetry — DB-backed, cross-replica (finding 3)

The API scales horizontally (`DEPLOYMENT.md`), so in-process counters reset and
`/v1/metrics` reaches only one replica — they cannot witness platform-wide v1=0.
Count v1-vs-v2 accepted signatures in a **durable store aggregated across
replicas** (a small DB counter table, consistent with the existing DB-backed
metrics), surfaced in `/v1/metrics`. The **sunset witness** is a defined
zero-observation window: **no v1-accepted requests across all replicas** for a
configured number of days before `hmac_v1_sunset_at`. In-process counters may
remain as diagnostic only, never the witness.

## 7. Tests (TDD, red-first per the cycle)

- **Unit:** canonical-string vectors (the published doc vectors ARE the
  fixtures); each binding dimension flipped independently (method, path, query,
  direction, key_id, timestamp, slot, body) must fail verification; skew +
  non-finite timestamp (preserve the v1 `nan` fix); sunset boundary
  (before/at/after); dual-emit header presence on callbacks.
- **Integration (DB, run red locally against the dev stack):**
  - **Cross-case redirect attack repro** — a captured v1-signed event replayed
    against another case succeeds pre-v2 (documents the hole) and 401s under v2.
  - D3 matrix: cross-case reuse → two runs; same-case conflict → 409; replay →
    stored snapshot.
  - **Review completion via the keyed event** (`website.review_completed` posted
    to `/v1/cases/{id}/events`) drives the same transition the retired endpoint
    did; the retired route returns 404; parity of validation asserted.
  - Migration 010 fresh-DB up/down; **seeded cross-case-duplicate downgrade
    refusal**.
  - Cross-replica counter aggregation (two sessions/factories increment; a
    single `/v1/metrics` read reflects both).
- Existing suites migrate to a v2 `sign_headers` twin in `tests/conftest.py`;
  v1-signing tests remain until sunset removal (they pin dual-accept);
  `test_phase4_platform.py` / `test_phase2_adapters.py` move off the endpoint.

## 8. Out of scope

PR 5b (review-record binding on the shared event pipeline), deleting v1 code +
the retired route's residue at sunset (a later versioned cutover once telemetry
shows zero v1), key-rotation tooling beyond the extra-keys map,
`request_nonces` (dropped — revisit only if a future keyless HMAC op appears),
any platform-side implementation. `KYC_Tool_Build_Package/` untouched; the
review-complete retirement and the nonce omission are recorded in
`AUDIT_FINDINGS.md` per governance (04 §5 evolves via documented D-series
decisions, never silent change).
