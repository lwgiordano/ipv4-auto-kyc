# PR 5a — HMAC v2 + per-case idempotency (design)

- **Date:** 2026-07-18
- **Status:** Design approved by human (with three dev-simplicity refinements);
  pending implementation plan (`writing-plans`), then the plan gate.
- **Unit:** ROADMAP §G "PR 5a — HMAC v2 + per-case idempotency (item 6)",
  migration 010, decision D3. This spec is a **thin delta pointer** per the
  adopted workflow design: it restates only what §G left open and how each was
  resolved; everything else is locked by `.agents/ROADMAP.md` §G/§B and is not
  re-litigated here.

## 1. Why (locked — ROADMAP §D, the M2 hazard)

v1 signs `{timestamp}.{body}` with one shared secret. `case_id` is in the
**unsigned path**, so a captured signed clean-company event can be redirected to
another case within the 300s skew and auto-approve it. D3's per-case idempotency
widens that hole (keys become reusable across cases) — so the canonical-path
binding and D3 land in the same PR.

## 2. The contract (locked by §G; concretized)

**Canonical v2 signed value** — newline-joined, HMAC-SHA256, hex:

```
v2 \n key_id \n direction \n method \n raw_path+query \n timestamp \n slot \n sha256(body)
```

- `direction`: `platform->tool` (inbound) or `tool->platform` (callbacks).
- `raw_path+query`: the literal request target as sent (path, plus `?query` if
  present; no re-ordering or re-encoding).
- `slot` (uniform one-sentence rule): the idempotency key if the request has
  one, else the nonce if it has one, else empty. (For eventful POSTs the key is
  also inside the signed body; repeating it in the slot keeps the rule uniform.)
- `sha256(body)`: hex digest of the raw body; empty body hashes normally.

**Headers:** v2 requests carry `X-KYC-Signature-V2`, `X-KYC-Key-Id`, and the
existing `X-KYC-Timestamp` (+ `X-KYC-Nonce` on keyless mutating ops only). v1's
`X-KYC-Signature` is untouched during dual-accept.

**Split secrets + key_id (locked):** separate inbound and outbound secrets.
Config: `hmac_inbound_key_id`/`hmac_inbound_secret` (verify; plus an optional
extra-keys map for future rotation), `hmac_outbound_key_id`/
`hmac_outbound_secret` (sign). Legacy `platform_hmac_secret` remains the v1
secret for both directions until sunset.

**Signature retry semantics (locked):** signatures are NOT blanket single-use.
A same-(case, key, payload) retry returns the stored response snapshot. Nonces
are single-use and exist **only** for keyless state-changing ops.

## 3. Open items → approved resolutions

1. **v1 sunset posture → config-dated, required in production.**
   `hmac_v1_sunset_at` (ISO-8601, optional): before it, dual-accept; at/after
   it, v1 rejected. `production_config_violations` **requires it set in
   production** — that is how §G's "fixed sunset" is enforced (prod cannot run
   dual-accept-forever). Staging default: unset (dual). The date itself is an
   ops decision set with TechCraft at the M3 cutover.
2. **Outbound leg → dual-emit.** During dual-accept, decision callbacks carry
   BOTH the v1 header and the v2 header set, signed with the respective
   secrets; the platform verifies whichever it supports and migrates without a
   coordinated flip. v1 emission stops at sunset.

## 4. Dev-simplicity refinements (approved with the design)

The platform-facing integration delta is one ~8-line signing function; all other
machinery is internal to the tool:

1. **One recipe covers everything TechCraft does** (event POSTs + reads): the
   slot rule above. **Nonces are invisible to the platform** — they apply only
   to `POST /v1/review-tasks/{id}/complete`, which is the tool's own
   ops-console path (the platform posts `website.review_completed` /
   `reviewer.manual_approve` as ordinary keyed events).
2. **`key_id` is a constant from their config** (e.g. `kyc-platform-1`); it
   changes only on an explicit secret rotation.
3. **`docs/PLATFORM_INTEGRATION.md` gains a signing quickstart**: a copy-paste
   reference function + worked test vectors (exact inputs → exact canonical
   string → exact signature). On staging, a failed v2 verification logs the
   server-computed canonical string (server-side only, never in the response)
   so a mismatch is diagnosable in one look.

## 5. Per-case idempotency (D3, locked) + nonces — migration 010

- Drop global `uq_events_idempotency_key`; add
  `uq_events_case_idempotency (case_id, idempotency_key)`; retarget ingest's
  `ON CONFLICT`. Cross-case reuse → two independent runs (never another case's
  run id); same-case same-key different-payload → 409 (behavior unchanged,
  scope narrowed). Record in **ADR-003 and `AUDIT_FINDINGS.md`**.
- New `request_nonces` (unique nonce, `seen_at`, `expires_at` + index): consume
  atomically on use; replay → 401; expiry = 2 × `hmac_max_skew_seconds`;
  opportunistic cleanup on insert.
- **§C migration gate:** `down_revision = 009`; `upgrade head && downgrade -1
  && upgrade head` clean on a fresh DB.

## 6. Telemetry

In-process counters — v1-accepted, v2-accepted, rejected — surfaced in
`/v1/metrics` (documented as since-process-start), so ops can watch TechCraft's
v1 traffic reach zero before the sunset date bites.

## 7. Tests (TDD, red-first per the cycle)

- **Unit:** canonical-string vectors (the doc's published vectors are the test
  fixtures); each binding dimension flipped independently (method, path, query,
  direction, key_id, timestamp, slot, body) must fail verification; skew and
  non-finite timestamps (preserve the v1 `nan` fix); nonce single-use + expiry;
  sunset boundary (before/at/after); dual-emit header presence.
- **Integration (DB, run red locally against the dev stack):** live repro of
  the **cross-case redirect attack** — a captured v1-signed event replayed
  against another case succeeds pre-v2 (documents the hole) and 401s under v2;
  the D3 matrix (cross-case reuse → two runs; same-case conflict → 409; replay
  → stored snapshot); migration 010 up/down; review-complete nonce round-trip
  (valid once, replayed 401).
- Existing suites migrate via a v2 `sign_headers` twin in `tests/conftest.py`;
  v1-signing tests remain until sunset removal (they pin dual-accept).

## 8. Out of scope

PR 5b (review-record binding), deleting v1 code at sunset (a later sweep once
telemetry shows zero v1), key-rotation tooling beyond the extra-keys map, any
platform-side implementation. `KYC_Tool_Build_Package/` untouched; deviation
recorded in `AUDIT_FINDINGS.md` per governance (04 §5 evolves: documented as
D-series decision, not a silent change).
