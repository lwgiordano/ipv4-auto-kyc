# KYC/KYB Remediation Roadmap — CANONICAL

_Single source of truth for both agents. Consolidates the workflow-generated
roadmap, Fable's amendment, and Codex's review (all nine changes accepted). Every
decision below is locked. Migration numbers are illustrative — **rebase
`down_revision` to the live Alembic head at merge** (see §C)._

Status: **PR 1–4 shipped** (`c37c052`, `7a19a9f`, `4c91be6`; PR 4 this commit).
Items 1–6 complete (PR 5a shipped: HMAC v2 + per-case idempotency). Item 11
complete (PR 5b shipped: review-record binding). Item 7A complete (PR 6
shipped: per-run policy bundle pinning + `engine_build_id`) — but M2 stays a
HARD STOP (see §D). PR 6b (item 7B, revalidation) next; still pending.
Auto-enforcement of positive decisions (M2) is a **hard stop** far downstream (§D).

---

## A. Verification status

All 13 backlog items verified against the real code (14-agent workflow) and the
amended plan adversarially reviewed by Codex. Confirmed anchors: per-case FIFO
serialization (`jobs.py:37-41`); gate-5 fail-open (`scoring.py:31` keys only on
`HARD_CONFLICT`); postal-token shortcut (`org_id.py:30,47`); reaper dead-letter in
a *separate* txn (`jobs.py:142-153`); global unique idempotency (`tables.py:59`);
runs already record `policy_bundle_hash` (`ingest.py:137`); Floqer write-back
(`side_effects.py:63-71`); HMAC signs `{timestamp}.{body}` only, `case_id`
unsigned (`security.py:14`); spec-package-unmodified governance (`AGENTS.md:8`).

---

## B. Decisions locked

- **D1 — callback sequence naming.** PR 2 → **`event_sequence`**; PR 7b →
  **`decision_sequence`**. No bare `case_sequence` on the wire.
- **D2 — no fabricated provenance.** `runs.input_snapshot_json` is **nullable**
  (NULL = pre-migration, unrecorded); never backfilled from today's
  `cases.submitted_json`. NOT-NULL enforced in code for new runs + guard test.
  Ordinal backfills (`event_sequence`) use `row_number()` but are **reconstructions,
  not proof of arrival order** (received_at ties are ambiguous) — mark them
  backfilled.
- **D3 — per-case idempotency.** Drop global `uq_events_idempotency_key`; add
  `uq_events_case_idempotency (case_id, idempotency_key)`. Cross-case reuse → two
  independent runs (never returns another case's run id). Within-case same-key +
  different-payload → **409**. Recorded in **ADR-003 AND `AUDIT_FINDINGS.md`**.

---

## C. THE load-bearing hazard: the Alembic chain

All migration specs were written as `008/down_revision 007` in isolation; Alembic
is linear. Rebase each to the live head at merge. **CI gate on every migration PR:**
rebased `down_revision` + `upgrade head && downgrade -1 && upgrade head` on a fresh
DB. `/readyz` resolves the head **dynamically** (shipped in PR 1 — do not regress).

| Unit | Item(s) | Migration | Content |
|------|---------|-----------|---------|
| PR 1 / 1.1 | 1 | — | lockdown, auth, /readyz (shipped + follow-up) |
| PR 2 | 2 | 008 | `runs.input_snapshot_json` (**nullable**), `cases.event_sequence`, `events.event_sequence` |
| PR 3 | 3, 4 | — | fail-closed validators + gate-5 frozenset (JSON reason codes) |
| PR 4 | 5 | 009 | `poc_tokens.{rir,org_handle,resource,consumed_at}` |
| PR 5a | 6 | 010 | drop global idem unique, add per-case unique, `request_nonces` |
| PR 5b | 11 | — | review-record binding |
| PR 6 | 7A | 011 | `policy_bundles`, `checks.policy_bundle_hash`, `runs/decisions.engine_build_id` |
| PR 6b | 7B | 012 | revalidation / rollout staging |
| PR 7a | 9 | 013 | `jobs.lease_token` |
| PR 7b | 8 | 014 | `outbox.ordering_stream`, `decisions.decision_sequence`, `cases.last_decision_sequence`, `UNIQUE(case_id, decision_sequence)` |
| PR 8 | 10 | 015 | `adapter_results.{source_sha256,source_size,source_content_type,source_version_id,evidence_ref}` |
| PR 9a/b/c | 12 | — | contract + adapter-output validation + real providers |
| PR 10 | 13 | 016 | broker full-list snapshots, `runs.{matched_broker_entity_id,matched_identifier_class,broker_snapshot_revision}` |

---

## D. Milestones

- **M1 — PR 1 merged, CI green.** ✅ `c37c052`.
- **M2 — Enable auto-enforcement. HARD STOP.** Flipping
  `KYC_ENFORCE_POSITIVE_DECISIONS` requires **M4 complete + a staging E2E on real
  adapters + platform cutover complete**. Gating on M4 (the whole backlog —
  including queue/outbox ordering, evidence containment, policy revalidation,
  broker provenance, and RDAP #6 resolved) rather than a hand-picked subset
  prevents a prerequisite being accidentally omitted. The flag is **PERMANENT** —
  default `false` forever, enabled explicitly per production environment, wiring
  **never removed**. _Why it can't be earlier: HMAC signs `{timestamp}.{body}`
  only; `case_id` is unsigned in the path, and D3 makes a key reusable across
  cases, so a captured signed clean-company event could be redirected to another
  case within the 300s skew and auto-approve it — until HMAC v2 (PR 5a) binds the
  path, and until the rest of the durability/evidence/provenance work lands._
- **M3 — Platform contract cutovers** (schedule with platform, ship flagged): PR 2
  (`event_sequence`), PR 5a/5b-HMAC (canonical v2 dual-accept + split secrets),
  PR 7b (`decision_sequence` + platform high-water-mark dedupe), PR 8 (`ObjectRef`),
  PR 9 (`evidence.refresh_requested` local extension + discriminated contract).
- **M4 — Backlog closed** requires: Phase B revalidation shipped (PR 6b) and
  RDAP #6 resolved. (`engine_build_id` stamping shipped in PR 6.) Until then
  §E items are OPEN.

---

## E. Open until explicitly closed (do NOT report "all done")

1. **RDAP fail-open (#6).** The four relationship `needs_review` flags +
   `conflicting_entity` are consumed (`org_id.py:32-38,101`) but produced by no
   real RIR strategy. PR 3 shrinks exposure; live relationship detection stays
   `TODO(integration)`.
2. **Policy/engine revalidation of pre-existing checks.** Until Phase B (PR 6b),
   a `PASS` written under a since-tightened rule survives — closed only when
   revalidation writes superseding checks.

---

## F. Spec governance (repo rule — Codex #6)

`KYC_Tool_Build_Package/` stays **unmodified** (`AGENTS.md:8`). Do **not** edit
`platform_events.json`; new events (`evidence.refresh_requested`) live in a
versioned **local extension contract**. OpenAPI is a **derived transport contract**,
not the business-policy source of truth (the machine-readable JSON is). Every
divergence — per-case idempotency, recalc broker gate, local event extension —
gets an `AUDIT:<id>` entry in `AUDIT_FINDINGS.md`, not only an ADR.

---

## G. Per-unit plan

### PR 1 — Production lockdown + auth + /readyz (item 1) — SHIPPED
Delivered `c37c052`: `environment` + `validate_for_production()`; `ui_enabled` off;
`enforce_positive_decisions` hold; read + `/ui`-mutation auth; `/readyz`
(dynamic head); provider knobs fail-closed.

### PR 1.1 — lockdown follow-up
Gate `/v1/metrics` (split `_metrics_payload` so `/ui` reuse stays ungated);
admin-gate the `/ui` GET endpoints (PII); move to router-level
`dependencies=[Depends(...)]`; `/readyz` 503-path tests (CI); `environment="test"`
in the conftest fixture.

### PR 2 — Immutable run snapshot + event sequence (item 2) — MERGED + CI-GREEN (`7a19a9f`)
_Follow-up (Codex PR 2 review): resume-safe Floqer context reseed + `event_sequence`
added to the `DecisionCallback` model. See git log._
Migration 008 (D2, nullable `input_snapshot_json`). ingest: lock case `FOR UPDATE`,
allocate `event_sequence`, pin the frozen snapshot on case+run; `_apply_submission`
split into a pure `_apply_event_to_snapshot` (returns a new dict) + `_update_case_metadata`;
**Floqer write-back deleted** (`side_effects.py`). Pipeline reads `run.input_snapshot_json`
(`_run_snapshot` helper, pre-008 fallback). Accepted acceptance details, all folded in:
- **`UNIQUE(case_id, event_sequence)`** backstop on events.
- **`cases.event_sequence` backfilled** to each case's max reconstructed sequence.
- **`events.sequence_backfilled`** persists whether a sequence was assigned live
  or reconstructed by the migration (D2 honesty).
- Concurrency test: **simultaneous same-case ingest + `reviewer.manual_approve`**
  (FOR UPDATE serializes; distinct gap-free sequences; UNIQUE holds).
- Callback `event_sequence` (D1) **behind the M3 flag** `callback_include_event_sequence`
  (default off — not shipped until the platform accepts it).
Offline unit tests green here; DB tests run in CI.

### PR 3 — Fail-closed validators + gate-5 (items 3, 4) — IMPLEMENTED (CI pending)
_Registry/org_id/documents/email/rir_poc/poc fail closed; ORG-ID handle equality +
no postal-token shortcut; email domain from address; POC association target;
`HARD_CONFLICT_REASON_CODES` allow-list; document/registry conflict stamps gate 5.
New reason codes; adversarial unit tests; doc fixtures gain `address`;
`AUDIT_FINDINGS.md §D6`. Offline suite green (67 tests); DB tests in CI._
Make registry/org_id/documents/email/rir_poc/poc fail-closed; add handle-equality,
**delete `_POSTAL_TOKEN` shortcut**; email domain from address; real POC association
target. `HARD_CONFLICT_REASON_CODES = frozenset({HARD_CONFLICT, DOCUMENT_REGISTRY_CONFLICT})`
in `scoring.py`; stamp `HARD_CONFLICT` in `documents.py`. **Rollback = git revert +
redeploy** (no module-constant switch — purity-illegal and needs redeploy anyway).
Tightening validators creates stale PASSes → sweep in PR 6b. Tests rewrite
`test_address_material_match_via_postcode` (PASS→FAIL); E2E ≥105-pt conflicting doc
never approves.

### PR 4 — Identity invalidation + POC token binding (item 5) — SHIPPED
Migration 009: `poc_tokens.{rir,org_handle,resource,consumed_at}`. Token bound to
**(case, rir, poc, org, resource, token_id, digest)** — handles collide across
registries; matched by `id + digest` (case-scoped, no extra index). The validator
re-checks the binding against the **current** snapshot, so a POC/ORG change makes a
stale token fail (binding mismatch) and `consumed_at` makes it single-use.
Identity-invalidation (`supersede_stale_identity_proof`) runs in `_decide_txn`
**before** `apply_check_intents` and **independent of adapter success** — a new
ORG-ID/POC drops the stale `org_id_match`/`poc_verified` even with RDAP down. Raw
token scrubbed → digest at ingestion (idempotency hash still from the original
envelope); dead `poc_email` outbox rows redacted like delivered ones. Token_id is
carried in the verification email for the platform to echo back (closes AUDIT:C2).
**Completes items 1–5** but does NOT trigger M2 (see §D).

### PR 5a — HMAC v2 + per-case idempotency (item 6) — ✅ SHIPPED
Delivered via the superpowers cycle (spec rev 6 + plan, both Codex-clean).
Landed: migration 010 (per-case idempotency + fail-closed v1 witness,
forward-only downgrade), sticky-v2 verifier + bidirectional sunset + activation
command, retired `/complete` with an ingest validation floor, outbound dual-emit,
`/v1/metrics` witness, and the three-phase attack proof. Two deviations recorded
in `AUDIT_FINDINGS.md` D8 + ADR-003: endpoint retirement and `request_nonces`
omission (no keyless op remains). Full suite green. Original spec below.

Migration 010 (D3). Canonical signed value (versioned):
`v2 \n key_id \n direction \n method \n raw_path+query \n timestamp \n
idempotency_key_or_nonce \n sha256(body)`. Signatures are **not** blanket
single-use: same (case, key, payload) retry returns the **stored response**; nonces
are single-use and only for keyless state-changing ops. Split inbound/outbound
secrets + `key_id`; dual-accept window with **fixed v1 sunset + telemetry**.
Record D3 in `AUDIT_FINDINGS.md`.

### PR 5b — Review-record binding (item 11) — ✅ SHIPPED
Delivered via the superpowers cycle (spec rev 8, Codex AUDIT-CLEAN, + plan).
Landed: the actor-trust floor at ingest (advisory, fast-fail) plus the
authoritative decide-txn guard (`ReviewTask` locked `FOR UPDATE` after the
case lock, one immutable guard decision feeding a pipeline-internal ORM view
and a separate immutable scalar view across the validator boundary); the
broker-blocked DECIDE short-circuit proven to still close the task and write
the check atomically; actor-derived persistence (`ReviewTask.reviewer_id` /
check `source` / audit actor from `event.actor_json`, `DecisionRow.reviewer_id`
untouched for automatic runs); the production composer 403 on both sensitive
event types (dev/staging composer now sends a real reviewer actor); and the
`kyc_tool.ops.requeue_interrupted_jobs` cutover-recovery one-shot. Recorded in
the architecture decision log — this PR takes the next ADR number, and the PR
10 reservation below moves one slot out accordingly. Shipped as a **brief
full maintenance window**, not a rolling deploy (`docs/DEPLOYMENT.md` §9).
Full suite green. Original spec below.

Reviewer identity trusted only as **platform-asserted** in the signed payload (or
tool-side OIDC) — `actor.id == payload.reviewer_id` is a consistency check, **not**
the security boundary. Production `/ui` composer **barred** from
`website.review_completed` and `reviewer.manual_approve`. Lock the task `FOR UPDATE`,
enforce type/status/case/actor, close + write check atomically.

### PR 6 — Policy bundle pinning (item 7A) — ✅ SHIPPED
Delivered via the superpowers cycle (spec rev 7 — Codex AUDIT-CLEAN after 7
rounds — + plan rev 6 — Codex PLAN-REVIEW rounds 1–5 folded). Landed:
migration 011 (`policy_bundles` content-hashed store with insert/conflict
read-back verification, singleton `bundle_pinning_epoch`,
`checks.policy_bundle_hash` + `runs/decisions.engine_build_id` provenance
columns with nonblank `CHECK`s, forward-only-after-use downgrade);
`domain/engine.py::ENGINE_BUILD_ID` plus the framed whole-source-tree drift
guard (this task); `Pipeline.resolve_bundle` resolving the run's creation-pin
bundle (or refusing via `BundleUnavailable`) **at job entry, before any
adapter call or side effect**; flag-on decision-time re-pricing of every
live check's points/category from the resolved rubric across
`score()`/`evaluate_gates()`/the callback checks-summary (immutable check
rows and validator PASS/FAIL untouched — re-judging evidence stays PR 6b);
atomic bundle **and** engine provenance on every automatic and manual
decision, with `runs.policy_bundle_hash` (the creation pin) never rewritten;
startup seed-and-verify + `bundle_pinning_ready` attestation, scoped to the
API and pipeline/dev workers only (`outbox`/`retention` stay
policy-independent); an unconditional `/readyz` bundle-store check;
`ops.verify_pinnable_backlog` / `ops.seed_policy_bundle` /
`ops.activate_bundle_pinning_epoch` plus the per-surface post-epoch
NULL-provenance check. Preserves PR 3's `HARD_CONFLICT_REASON_CODES`
(engine code, covered by `engine_build_id`, not the rubric). Behind
`enforce_bundle_pinning` (default off); rollout is the drained cutover in
`docs/DEPLOYMENT.md` §10. Recorded in **ADR-005** (prepended above ADR-004 —
the PR 10 ADR-005 reservation below moves to **ADR-006**). Full suite green.
Original spec below.

Migration 011: `policy_bundles`, `checks.policy_bundle_hash`, **`engine_build_id`
on runs + decisions**. Worker loads the run's bundle by hash, refuses if unloadable;
`score()`/`evaluate_gates()` pinned to rubric args (**preserve PR 3's
`HARD_CONFLICT_REASON_CODES`** — must live in the bundle or be covered by
`engine_build_id`). Behind `enforce_bundle_pinning`.

### PR 6b — Revalidation (item 7B) — PENDING, required for M4
Migration 012. Revalidate immutable evidence under the run's pinned bundle+engine,
write **superseding** checks; a tightened pass rule marks prior PASSes stale until
revalidated; block rollout activation until successors exist.

### PR 7a — Queue lease fencing (item 9)
Migration 013: `jobs.lease_token`. Every transition gated on
`(id, locked_by, lease_token, status='running')`; heartbeat ≤ lease/3; cadence
reaper fails the run in the **same txn**; fenced complete inside the decide txn
(stale worker rolls back the decision).

### PR 7b — Outbox stream separation (item 8)
Migration 014: `outbox.ordering_stream` (decision|email), `decisions.decision_sequence`
(D1), `cases.last_decision_sequence`, `UNIQUE(case_id, decision_sequence)`.
**Allocate `decision_sequence` via the locked case counter** (not `max()+1`), only
for callback-emitting decisions. Claim predicate scopes FIFO per
`(case_id, ordering_stream)` so POC-email failure never blocks the decision callback.
Requeue marks `superseded`, never reverts a newer decision.

### PR 8 — Object-store containment + immutable evidence (item 10)
Migration 015. Structured `ObjectRef{key, sha256, size?, content_type?, version_id?}`
(**no bucket field**); separate input vs evidence buckets; `_safe_path` containment
(reject traversal/absolute/symlink/foreign-bucket); streamed byte cap; verify digest;
copy source doc to a content-addressed immutable evidence key **before** OCR is
trusted. Adapter *outputs* are already archived (`pipeline.py:226-229`) — this adds
inbound containment + source-document immutability.

### PR 9a/9b/9c — Executable contract + output validation + real providers (item 12)
9a: discriminated-union payloads + `schema_version`; type the `/events` body →
OpenAPI request schema (**derived transport contract, not normative**); committed
golden + drift test; HMAC test vectors. 9b: per-adapter Pydantic output models;
`AdapterStatus.INVALID_RESPONSE` → partial run (malformed-but-non-raising output is
currently silently accepted as `ok` — the real gap). 9c: rewrite stale
`OVERVIEW.md §7` (tool sends the POC email — code already does); wire `SesEmailSender`;
close the OCR ownership decision (recommend `TextractOcrEngine`, keep
`document.uploaded`).

### PR 10 — Production ops hardening (item 13)
Migration 016: broker **full-list immutable snapshots** `(revision, sha256,
json_bytes, author, timestamp)` — NOT per-entity versioning (6 entities; snapshots
reproduce matches AND non-matches, simpler); run records matched entity + snapshot
revision. `adapters/retry.py` (transient classification + Retry-After — job-layer
backoff already exists); `recalculate.requested` runs the broker gate (record
`AUDIT:<id>` + ADR-006 — spec limits recalc to "no adapter calls", but the gate is a
local lookup); new `evidence.refresh_requested` in a **local extension contract**;
RUNBOOK: delete the broken self-select SQL → point to the existing authenticated
requeue endpoint; metrics windowing (index exists) + Prometheus + alerts; pin deps +
base image `@sha256`; `ruff format --check` in CI; wire `core.hooksPath` durably in
`manage.sh`.

---

## H. Program-level sequencing risks

1. **Migration renumbering (highest):** rebase every migration PR to the live head;
   CI gate as in §C.
2. **`ingest.ingest_event`** edited by PR 2, PR 4, PR 5a — PR 2 lands first; others
   rebase onto its reorder.
3. **`scoring.py`** edited by PR 3 (frozenset) + PR 6 (rubric-pinned signatures) —
   PR 6 preserves the frozenset.
4. **conftest churn:** PR 1 (ui/environment), PR 3 (org_id PASS→FAIL rewrites),
   PR 5a (canonical sign_headers) — bundle fixture updates into the same PR.
5. **Platform coordination (M3):** PR 2, PR 5a/5b, PR 7b, PR 8, PR 9.
6. **M2 is frozen** behind PR 1.1 + PR 5a/5b + PR 9 + staging E2E + RDAP. The kill
   switch is permanent.

## I. ADRs / AUDIT_FINDINGS to write
ADR-003 + `AUDIT:` (per-case idempotency), **ADR-005 — written (PR 6, per-run
policy bundle pinning)**, ADR-006 + `AUDIT:` (PR 10: broker gate on
recalculate — bumped from the ADR-005 reservation now that PR 6 owns it),
`AUDIT:` (local `evidence.refresh_requested` extension), `AUDIT:`
(OpenAPI as derived contract).
