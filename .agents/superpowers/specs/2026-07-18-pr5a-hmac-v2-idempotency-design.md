# PR 5a — HMAC v2 + per-case idempotency (design)

- **Date:** 2026-07-18
- **Status:** Design **rev 5** — rev 4 made PR 5a a non-hot cutover; rev 5 folds
  Codex's two rev-4 findings (`6ed76e8`, both accepted): an explicit
  **v1/v2 dual-accept precedence rule** (v2 is sticky, no downgrade — finding 1,
  P1), and a concrete witness-activation command + named observation-window
  setting (finding 2, P2). Pending human spec re-review, then `writing-plans`,
  then the plan gate.
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

**Dual-accept precedence — v2 is sticky, no downgrade (rev5, finding 1).** The
verifier matrix, evaluated per inbound request:

- **Any v2 header present** (`X-KYC-Signature-V2` OR `X-KYC-Key-Id`) → the
  request is evaluated **v2-only**: require the complete v2 set + valid v2
  (known `key_id`, timestamp in skew, signature matches the canonical value) or
  **reject**. **No fallback to v1**, even if a valid `X-KYC-Signature` is also
  attached. A valid v2 request is **counted as v2** for telemetry.
- **No v2 header at all** → v1 may authenticate, but only before
  `hmac_v1_inbound_sunset_at`, with its fail-closed witness write (§6); counted
  as v1-accepted. After the inbound sunset, a v1-only request is rejected.

This closes two failures: (A) a dual-sent valid-v1+valid-v2 request counted as v1
would keep the witness above zero and stall cutover — v2 wins and is counted v2;
(B) a request that asserts v2 but is malformed must never fall through to the
path-unbound legacy scheme. Tests: both-valid → v2; valid-v1 + bad/unknown-key
v2 → 401; partial v2 (missing `key_id` or signature) → 401; unknown `key_id` →
401; v1-only → v1 until sunset, 401 after.

**Signature retry semantics (locked):** signatures are NOT blanket single-use;
a same-(case, key, payload) retry returns the stored response snapshot.

## 3. Open items → approved resolutions (unchanged from rev 1)

1. **v1 sunset → config-dated, required in production, and BIDIRECTIONAL
   (rev3, finding 2).** Two independent ISO-8601 dates, because the two
   directions have independent readiness:
   - `hmac_v1_inbound_sunset_at` — stop *accepting* inbound v1. Gated by the
     inbound DB witness (§6: TechCraft's v1 *sending* reached zero).
   - `hmac_v1_outbound_sunset_at` — stop *emitting* v1 on callbacks. Gated by a
     **v2-only staging callback E2E + TechCraft sign-off** that their webhook
     receiver verifies v2 — NOT inferable from inbound telemetry (callbacks
     return only 2xx; the publisher sees only `raise_for_status()`, so the tool
     cannot tell which signature the platform accepted).

   Both required in production (`production_config_violations`); staging default
   unset. Set with TechCraft at the M3 cutover. A single shared date would fail
   this way: TechCraft flips inbound to v2 while its receiver still verifies only
   v1 → inbound v1 hits zero → shared sunset stops v1 callback emission → every
   callback fails.
2. **Outbound leg → dual-emit.** Until `hmac_v1_outbound_sunset_at`, callbacks
   carry BOTH the v1 header and the v2 header set (each signed with its secret);
   the platform verifies whichever it supports and migrates without a coordinated
   flip.

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

**Validation boundary (rev3, finding 1 — explicit, not deferred).** Today the
keyed `website.review_completed` path validates only payload *shape*
(`WebsiteReviewCompletedPayload`: `task_id`/`result`/`reviewer_id`); the pipeline
then creates the `website_verified` check largely unconditionally and closes a
matching open task only if one happens to exist. A direct repro — `task_id`
that does not exist, `result="pass"` — produces a `website_verified pass` check.
The **retired endpoint** did more: `session.get(ReviewTask, task_id)` → 404 if
missing. So retiring it MUST NOT regress below that. Explicit split:

- **PR 5a minimum (this PR) — semantic validation on the event path.** After
  idempotency-replay resolution but **before** creating a new run/check, require:
  (a) the task exists; (b) `task_type == "website"`; (c) the task's `case_id`
  equals the **signed path** `case_id`; (d) `status == "open"`. Otherwise reject
  (404 unknown task / 409 wrong-case or closed / 422 wrong-type) and create no
  check. This closes the invalid-task PASS hole created by retiring the endpoint.
- **PR 5b (later) — trust binding + concurrency.** `SELECT … FOR UPDATE` on the
  task, platform-asserted actor/`reviewer_id` binding, and the atomic
  close-task + write-check transaction. 5a does the safety floor; 5b does the
  hardening.

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

## 6. Sunset telemetry — durable, cross-replica, fail-closed (rev3, finding 3)

The API scales horizontally (`DEPLOYMENT.md`), so in-process counters reset and
`/v1/metrics` reaches only one replica — they cannot witness platform-wide v1=0.
The v1 **acceptance witness** is durable and fail-closed:

- A fixed inbound-v1 witness row holds `accepted_count`, `last_accepted_at`, and
  `observation_started_at`, updated **atomically across replicas** on every
  accepted inbound v1 request. It is seeded **inactive** (`observation_started_at`
  NULL) — the clock is NOT started at migration time (§6a).
- **Fail-closed:** if that durable update cannot be written, the v1 request is
  **rejected** — so real v1 traffic is never silently invisible.
- The **zero predicate** (safe to reach `hmac_v1_inbound_sunset_at`) requires
  BOTH: observation age (`now − observation_started_at`) ≥
  **`hmac_v1_observation_window_days`** (positive integer, production-required)
  AND `last_accepted_at` absent or older than that window. An absent/unseeded row
  means "observation never started," not "zero traffic," and does NOT satisfy the
  predicate.
- v2-accepted and rejected counters are **diagnostic** and may use weaker
  best-effort availability; only the v1-acceptance witness is fail-closed.
  Surfaced in `/v1/metrics`.

This witness gates only the **inbound** date; the **outbound** date is gated by
the staging callback E2E + TechCraft sign-off (§3), never by this telemetry.

## 6a. Deploy — non-hot cutover + witness activation (rev4, finding 1)

Migration 010 is **not hot-compatible**: the old image's ingest does
`ON CONFLICT (idempotency_key)` (`ingest.py:133`), which needs the global unique
that 010 drops — an old replica still serving after the migration would fail
event inserts. So PR 5a ships as an explicit **stop → migrate → start** cutover,
not a rolling upgrade. Consequences:

- Migration 010 seeds the sunset witness **inactive** (`observation_started_at`
  NULL) and does **not** start the observation clock.
- After every old API replica is drained AND every new instance is
  readiness-verified, an explicit **activation** — the non-network management
  command **`python -m kyc_tool.ops.activate_hmac_v1_observation`**, a single
  compare-and-set `UPDATE … SET observation_started_at = now() WHERE
  observation_started_at IS NULL RETURNING …` (idempotent: a rerun reports
  "already active" and never resets a live window; it serializes naturally with
  concurrent witness updates) — sets `observation_started_at`. The inbound zero
  window begins only there — never from migration time — so a straggler old
  replica accepting un-witnessed v1 cannot turn the predicate green prematurely.
- An **inactive** witness (NULL `observation_started_at`) never satisfies the
  zero predicate, regardless of elapsed wall-clock (test in §7).
- Runbook documents the stop/migrate/start ordering + the activation step, and
  corrects the blanket "all revisions downgrade cleanly" line for 010 (§9).

## 7. Tests (TDD, red-first per the cycle)

- **Unit:** canonical-string vectors (the published doc vectors ARE the
  fixtures); each binding dimension flipped independently (method, path, query,
  direction, key_id, timestamp, slot, body) must fail verification; skew +
  non-finite timestamp (preserve the v1 `nan` fix); **both** sunset boundaries
  (inbound-accept and outbound-emit, independently before/at/after); dual-emit
  header presence on callbacks before the outbound date.
- **Integration (DB, run red locally against the dev stack):**
  - **Cross-case redirect attack repro** — a captured v1-signed event replayed
    against another case succeeds pre-v2 (documents the hole) and 401s under v2.
  - D3 matrix: cross-case reuse → two runs; same-case conflict → 409; replay →
    stored snapshot.
  - **Review completion via the keyed event** (`website.review_completed` posted
    to `/v1/cases/{id}/events`) drives the transition; the retired route returns
    404. **Validation matrix (finding 1):** nonexistent task → 404; wrong-case →
    409; wrong-type → 422; closed task → 409; valid → `website_verified pass`,
    and a valid replay returns the stored snapshot. **No check is written on any
    reject.**
  - Migration 010 fresh-DB up/down; **seeded cross-case-duplicate downgrade
    refusal**.
  - **Sunset witness (finding 3):** two sessions/factories increment the durable
    v1 witness and a single `/v1/metrics` read reflects both; an unseeded/absent
    witness row does NOT satisfy the zero predicate; a forced durable-write
    failure **rejects** the v1 request (fail-closed).
  - **Witness activation (rev4, finding 1):** an **inactive** witness (NULL
    `observation_started_at`) never satisfies the zero predicate regardless of
    elapsed wall-clock; only the explicit post-cutover activation starts the clock.
  - **Activation command (rev5, finding 2):** `activate_hmac_v1_observation` on a
    NULL witness sets `observation_started_at` (compare-and-set RETURNING); a
    **rerun reports already-active and does NOT reset** the live window.
  - **v1/v2 precedence (rev5, finding 1):** both-valid → counted v2; valid-v1 +
    bad/unknown-key v2 → 401 (no v1 fallback); partial v2 → 401; unknown
    `key_id` → 401; v1-only → accepted before inbound sunset, 401 after.
  - **Bidirectional sunset (finding 2):** past `hmac_v1_inbound_sunset_at`,
    inbound v1 is rejected while outbound callbacks still dual-emit v1 until
    `hmac_v1_outbound_sunset_at` — the two dates move independently.
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

## 9. Operator-contract surfaces this PR must update (rev4, finding 2)

The contract change makes several operator/doc/test surfaces stale; the plan
updates each (and the claim is extended to cover them):

- `docs/RUNBOOK.md` — the blanket "all revisions downgrade cleanly" line
  (`:11`) is corrected: **010 refuses downgrade after cross-case reuse**; add the
  stop/migrate/start + witness-activation ordering.
- `.env.example`, `docs/OVERVIEW.md`, `docs/PLATFORM_BRIEFING.md` — the single
  shared `KYC_PLATFORM_HMAC_SECRET` story is superseded by **split inbound/
  outbound secrets + key_id, the two sunset dates, and the observation window**;
  document the new settings.
- `docs/DEPLOYMENT.md` — the non-hot cutover + activation step (already claimed).
- **New command module (rev5, finding 2):** `src/kyc_tool/ops/activate_hmac_v1_observation.py`
  (+ `src/kyc_tool/ops/__init__.py`) — the idempotent compare-and-set activation
  command; a unit/integration test for first-activation + rerun-no-reset.
  `.env.example`/`docs/RUNBOOK.md` also gain `hmac_v1_observation_window_days`
  (production-required) alongside the two sunset dates and split secrets.
- Tests already on disk that this PR must touch: `tests/integration/test_migrations.py`
  (010 up/down + downgrade-refusal), `tests/unit/test_production_config.py`
  (both sunset dates required in production), `tests/unit/test_ops_auth.py`
  (v2 verification paths). These are added to the claim before plan/code.
