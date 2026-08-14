# KYC/KYB Remediation Roadmap — CANONICAL

_Single source of truth for both agents. Consolidates the workflow-generated
roadmap, Fable's amendment, and Codex's review (all nine changes accepted). Every
decision below is locked. Migration numbers are illustrative — **rebase
`down_revision` to the live Alembic head at merge** (see §C)._

Status: **PR 1–4 shipped** (`c37c052`, `7a19a9f`, `4c91be6`; PR 4 this commit).
Items 1–6 complete (PR 5a shipped: HMAC v2 + per-case idempotency). Item 11
complete (PR 5b shipped: review-record binding). Item 7A complete (PR 6
shipped: per-run policy bundle pinning + `engine_build_id`) — but M2 stays a
HARD STOP (see §D). **PR 7b (item 8) was split into PR 7b-core (SHIPPED as `013`-`023`:
stream separation + local decision ordering + the witness repair/authority/admission/boundary/
transition-authority/repair revisions — see §C) and PR 7b-activation (`024`, the platform-authoritative cutover —
the next pending unit once 7b-core reaches AUDIT-CLEAN).**
Both are ahead of PR 6b, which consumes the callback-ordering guarantee its
revalidation coordinator rests on (Codex PR-6b rev-5 F3); 7b-core is shipped,
7b-activation and 6b still pending.
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

The **State** column (`shipped`/`pending`/`—`) is machine-checked by
`tests/unit/test_migration_lineage.py`: every authored Alembic revision at or above
the first reservation must be owned by a `shipped` row, every `pending` revision must
be absent from the Alembic chain, the first `pending` revision must be `head+1`, and
no revision may be double-booked. Update a row's State to `shipped` in the same PR
that lands its migration.

| Unit | Item(s) | State | Migration | Content |
|------|---------|-------|-----------|---------|
| PR 1 / 1.1 | 1 | — | — | lockdown, auth, /readyz (shipped + follow-up) |
| PR 2 | 2 | shipped | 008 | `runs.input_snapshot_json` (**nullable**), `cases.event_sequence`, `events.event_sequence` |
| PR 3 | 3, 4 | — | — | fail-closed validators + gate-5 frozenset (JSON reason codes) |
| PR 4 | 5 | shipped | 009 | `poc_tokens.{rir,org_handle,resource,consumed_at}` |
| PR 5a | 6 | shipped | 010 | drop global idem unique, add per-case unique, `request_nonces` |
| PR 5b | 11 | — | — | review-record binding |
| PR 5c | — | — | — | per-key HMAC retirement evidence (reserved by audit fold `4c3015a..cccd5f7` F3, NOT built): durable fleet-wide per-key acceptance witness with a defined zero window, or signed signer-fleet cutover receipt with bounded observation, plus an HMAC signer-target drained-cutover record (target key id, secret digest, exact attested publisher roles). Unblocks the two retirement steps WIRE.SIGN.ROTATION publishes as BLOCKED; shipping it must flip the absence anchors and rewrite the gates in docs/contracts/wire.py in the same change |
| PR 6 | 7A | shipped | 011, 012 | `policy_bundles`, `checks.policy_bundle_hash`, `runs/decisions.engine_build_id` (011); `VALIDATE` those provenance CHECKs (012, audit round 1) |
| PR 7b-core | 8 | shipped | 013 | `outbox.ordering_stream` (NOT NULL) + `case_id` NOT NULL + per-(case,stream) claim; `decisions.decision_sequence` + `cases.last_decision_sequence` + a **best-effort** local `superseded` guard (higher *locally-stamped* delivery only; send-before-stamp/cross-replica reverts remain for 7b-activation); identity + ordering (`UNIQUE decisions(case_id, decision_sequence)` [per-case namespace] + `UNIQUE(run_id)` + triple FK + partial callback index); fenced claim (`claim_token`); exhaustive per-status lifecycle CHECKs; `ordering_stream`/`case_id` real `SET NOT NULL` (drained cutover; 013's OWN downgrade is reversible-before-first-supersession, but the shipped 013–023 chain is forward-only after any witness — see the SHIPPED ROLLBACK CONTRACT note below, as 018+ are forward-only) |
| PR 7b-core repair | 8 | shipped | 014 | `outbox_delivery_attempts` (pre-HTTP attempt authority; created-or-validated because the amended-013 history exists) + insert-only trigger; `outbox.witness_generation` (legacy|attempt_v1, conservative backfill); digest⇒delivered CHECK; `cases.latest_decision_row_id` (trigger-maintained latest-decision authority + composite same-case FK; read API never sorts by `decided_at`); `ix_outbox_live_status` partial index for the exact (pending,dead) alerting predicate; **forward-only-after-any-witness** downgrade |
| PR 7b-core hardening | 8 | shipped | 015 | witness AUTHORITY: attempt admission trigger (live claim only), `witness_generation` immutable, terminal digest write-once (pending→delivered with matching attempt; never cleared/rewritten), exact-definition re-validation of the adopted attempt table (complete constraint/index/trigger sets, schema-bound), child-first downgrade lock order (no 40P01 vs live writers), forward-only-after-any-witness |
| PR 7b-core admission | 8 | shipped | 016 | witness ADMISSION provenance: `outbox_delivery_attempts.admission` (`legacy_unverified`\|`admission_v1`, stamped only by the trigger — pre-authority attempts are never promoted into staged-intent evidence); unwitnessed decision delivery refused at the transition; canonical authority functions DROPPED+RECREATED (name-only validation proved gameable); downgrade also refuses on NEGATIVE evidence (`attempt_v1` rows) |
| PR 7b-core boundary | 8 | shipped | 017 | outbox AUTHORITY BOUNDARY: admission requires an UNEXPIRED lease (`clock_timestamp()`, trigger + publisher fence — an expired claim stages nothing and sends nothing); decision callbacks born pending/unwitnessed/unclaimed (INSERT guard) and undeletable (DELETE guard; negative evidence survives); identity (kind/case/run/stream/sequence) immutable; payload immutable while sendable, governed redaction only once terminal; `delivered` reachable only from `pending`; ONE shared advisory fence for witness writers (shared) and migrations (exclusive) replaces lock-order reasoning; DB-checked live-claim preflight plus operator/orchestrator process-quiescence attestation; full structural validation + `search_path`-pinned functions; REBUTS the provenance-regress prescriptions (see bus) |
| PR 7b-core transition authority | 8 | shipped | 018 | dual-table CANONICAL MANIFEST (ordered columns/types/nullability/defaults, every named constraint's exact definition, every index's exact definition, the complete enabled trigger set with exact `pg_get_triggerdef`, each owned function's normalized-body digest + pinned `search_path`) refusing BEFORE any DDL; ONE exhaustive per-kind transition matrix making `delivered`/`superseded` FINAL (no resurrection to pending/dead; terminal timestamps/claim tuple/witness/identity/attempts/errors frozen, only the governed payload redaction permitted); `cases.latest_manual_decision_row_id` (trigger-maintained sticky manual-attribution pointer replacing `ORDER BY decisions.id DESC` — `decisions.id` is a random UUID, so sorting it was wrong); retention brought inside the shared advisory fence; **forward-only** downgrade |
| PR 7b-core payload repair | 8 | shipped | 019 | the governed payload redaction is permitted on any NON-`pending` row, with one deliberate exception (a `dead` decision callback keeps its body so `dead → pending` requeue can still re-send it). `017`'s rule allowed the redaction only on `delivered`/`superseded` and `018` inherited it, so a POC email that EXHAUSTED its retries could not scrub its raw token: the refusal rolled back the whole terminal transaction, the row never reached `dead`, the token stayed at rest, and the raise escaped the outbox worker's loop. Found by self-review after `018` was released; pins `018`'s witness-guard body digest before replacing it; **forward-only** |
| PR 7b-core redaction uniformity | 8 | shipped | 020 | BOTH ends of "sendable": a body may not be scrubbed while sendable AND a scrubbed body may not become sendable. `019` keyed only on `OLD.status`, so `dead → redact → reopen` produced a `pending` POC email with no recipient — claimed FIRST by `min(id)` and failing on every claim, blocking its whole stream, body unrestorable. The `dead`-callback carve-out is REMOVED rather than dated: the redaction is uniform across kinds, its justification ("a dead callback must stay requeueable") moves to the requeue endpoint, which refuses a redacted row of either kind, and retention widens to dead callbacks aged on `created_at` — closing a reviewer-identifier retention gap `019` had foreclosed at the DDL layer. Validates the complete CODE surface (every owned function's digest + pinned `search_path`, the complete enabled trigger set of both authority tables and `decisions`) and says exactly that, after `019` claimed `018`'s manifest and checked one function; **forward-only** |
| PR 7b-core poison recovery | 8 | shipped | 021 | `020`'s new arm asserted a STATE, not a TRANSITION, so it fired on the CLAIM of an already-unsendable row — and the claim has no exception handler and takes the `min(id)` head, so that exit stopped EVERY case and BOTH streams (`019`'s same seed degraded for ~21 min and self-healed). The arm is now a transition rule: a scrubbed body may not BECOME sendable, but an already-poisoned row stays claimable so it can fail, dead-letter and drain itself. The invariant is maintained at INSERT for BOTH kinds (poc_email had no INSERT guard at all); the upgrade REFUSES with `MIGRATION_021_PREFLIGHT_UNSENDABLE_PENDING_ROWS` naming the ids rather than silently arming the outage; and `kyc_set_latest_decision_row` — created unpinned by `014` and missed by `020`'s "complete code surface" — is recreated with a pinned `search_path`; **forward-only** |
| PR 7b-core authority invariant repair | 8 | shipped | 022 | validates the exact 021 authority surface by trigger definition and origin-enable mode (not name-only or replica-only); repairs and enforces terminal POC-token redaction in the terminal transition; makes `outbox.created_at` immutable in every status; adds DB guards that `cases.latest_manual_decision_row_id` references a same-case manual decision and `decisions.manual` is immutable; **forward-only** |
| PR 7b-core cross-table authority repair | 8 | shipped | 023 | validates the exact cross-table authority constraints and data that 022 did not cover: `fk_outbox_decision_triple`, `fk_cases_latest_decision`, `fk_cases_latest_manual_decision`, their unique targets, and same-case pointer/outbox rows; read surfaces fetch pointer rows by `id+case_id`; validation-only downgrade |
| PR 7b-activation | 8 | pending | 024 | wire `decision_sequence` emission + `integrity_mismatch`; platform high-water bootstrap (candidate manifest + signed response envelope); `outbox_ordering_activation` phase machine + immutable `BYTEA` artifacts; four CAS CLIs + activation cutover (`down_revision='023'`, forward-only-after-use) |
| PR 7b-inputs | — | — | — | platform answer artifacts (reserved by audit fold `4c3015a..cccd5f7` F11, NOT built): versioned, approved/signed answer-artifact schema and its verifying authority for the O1-O4 obligations behind WIRE.ORDERING.PENDING_INPUTS. Until it ships, `resolution_problems` in docs/contracts/wire.py refuses every artifact — the acceptors are content screens, and screening is not resolution. Shipping it must replace ANSWER_ARTIFACT_SCHEMA and rewrite the gate in the same change |
| PR 6b | 7B | pending | 025 | revalidation / rollout staging |
| PR 7a | 9 | pending | 026 | `jobs.lease_token` |
| PR 8 | 10 | pending | 027 | `adapter_results.{source_sha256,source_size,source_content_type,source_version_id,evidence_ref}` |
| PR 9a/b/c | 12 | — | — | contract + adapter-output validation + real providers |
| PR 10 | 13 | pending | 028 | broker full-list snapshots, `runs.{matched_broker_entity_id,matched_identifier_class,broker_snapshot_revision}` |

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
  PR 7b-activation (`decision_sequence` + platform high-water-mark dedupe), PR 8 (`ObjectRef`),
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

### PR 5c — Per-key HMAC retirement evidence — FUTURE, reserved unbuilt (audit fold `4c3015a..cccd5f7` F3)
Not started and not scheduled; exists so the BLOCKED retirement steps in `WIRE.SIGN.ROTATION`
have a named owner. Scope when designed: a durable fleet-wide per-key acceptance witness
(per-key accepted/last-seen with a defined zero window) or a signed signer-fleet cutover
receipt with bounded observation, for the inbound direction; an HMAC signer-target
drained-cutover record naming the target key id, its secret digest, and the exact attested
publisher roles, for the outbound direction. Shipping any part MUST flip the executable
absence anchors (`hmac_witness` closed function inventory, hmac table closed inventory,
`ops.cutover` closed record inventory, `SIGNED_FLEET_RECEIPT_SCHEMA`) and rewrite the
retirement gates and claim state in `docs/contracts/wire.py` in the same change — the
`WIRE.SIGN.ROTATION_RETIREMENT` verifier enforces exactly that. Migration number assigned at
design time; none reserved.

### PR 6 — Policy bundle pinning (item 7A) — ✅ SHIPPED
Delivered via the superpowers cycle (spec rev 7 — Codex AUDIT-CLEAN after 7
rounds — + plan rev 6 — Codex PLAN-REVIEW rounds 1–5 folded). Landed:
migration 011 (`policy_bundles` content-hashed store with insert/conflict
read-back verification, singleton `bundle_pinning_epoch`,
`checks.policy_bundle_hash` + `runs/decisions.engine_build_id` provenance
columns with nonblank `CHECK`s, forward-only-after-use downgrade; migration
**012** then `VALIDATE`s those provenance CHECKs — 011 adds them `NOT VALID`
so the rolling deploy takes only a brief metadata lock, not a table scan
under an exclusive lock, audit round 1);
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
the PR 10 ADR-005 reservation below moves to **ADR-007**, since PR 6b owns
ADR-006). Full suite green.
Original spec below.

Migration 011: `policy_bundles`, `checks.policy_bundle_hash`, **`engine_build_id`
on runs + decisions**. Worker loads the run's bundle by hash, refuses if unloadable;
`score()`/`evaluate_gates()` pinned to rubric args (**preserve PR 3's
`HARD_CONFLICT_REASON_CODES`** — must live in the bundle or be covered by
`engine_build_id`). Behind `enforce_bundle_pinning`.

### PR 7b-core — Outbox stream separation + local decision ordering (item 8, part 1) — reordered FIRST
Migration **013** (`down_revision='012'`): `outbox.{ordering_stream (NOT NULL, decision|email),
decision_sequence, resolved_at, claim_lease_expires_at, claim_token, claimed_by}`,
`decisions.decision_sequence` (D1), `cases.last_decision_sequence`; **identity + per-case ordering** —
**`UNIQUE decisions(case_id, decision_sequence)`** (the per-case namespace; `UNIQUE(run_id)` alone
does **not** give it) + `UNIQUE decisions(run_id)` + `UNIQUE decisions(run_id, case_id,
decision_sequence)` (triple-FK target) + a triple FK `outbox(run_id, case_id, decision_sequence) →
decisions` + a partial `UNIQUE outbox(run_id) WHERE kind='decision_callback'` (one callback per run),
plus manual/automatic, kind/stream, **status vocabulary + lifecycle-tuple**, token/lease-pairing
CHECKs, and real `SET NOT NULL` on `ordering_stream`/`case_id` (not only value-rejecting CHECKs). **Allocate `decision_sequence` via the
locked case counter** (not `max()+1`), callback-emitting decisions only; claim FIFO scoped per
`(case_id, ordering_stream)` with a **fenced claim** (`claim_token` + `claim_lease_expires_at`)
separate from the retry schedule (`next_attempt_at`), so a stuck POC email never blocks the decision
callback, recovery resets only claimed rows, and a stale claimant cannot overwrite the reclaiming
publisher's terminal. The **best-effort local `superseded` guard** (mark an older requeued callback
`superseded` only when a higher-sequence decision was *locally stamped* `published_at`) **reduces the
common** single-replica revert; the **send-before-stamp** and **cross-replica** reverts remain until
7b-activation (documented residual risk). `decision_sequence` is **internal** (not on the wire).
Legacy backfill orders by `outbox.id` (under-lock serialization), **not** `decided_at` (txn-start);
a missing legacy callback is **restore-from-backup or `BLOCKED_NO_AUTHORITATIVE_MAPPING` on 012**
(user-confirmed 2026-07-23; no pre-013 reconciliation unit; 7b-activation/`024` is downstream). A **step-0 pre-window
`verify_pr7b_core_backfill` diagnostic** (schema-012-compatible, `SHARE`-locked) runs with retention
**terminated + zero-running attested** before any outage. Drained migration cutover (shipped
`requeue_interrupted_jobs`; **no** pre-013 outbox reset — the claim columns don't exist yet; a
`reset_interrupted_outbox_claims` CLI is post-013-only; digest-pinned;
no mutating prod smoke); migration 013's OWN downgrade is reversible-before-first-supersession
(refuses once a `superseded` row exists). Cross-replica authority is 7b-activation.

> **SHIPPED ROLLBACK CONTRACT (013–023) — canonical (re-audit `d569a15..4938840` F12).** Do NOT read
> any single migration's downgrade property as the chain's rollback policy. Migrations **018 and 022
> are forward-only**, so once the installed head is at/after 018 the schema **cannot be downgraded** —
> rollback is **forward-only after any witness; recover by restoring a compatible pre-cutover image**,
> exactly as RUNBOOK states. 013's conditional downgrade is reachable only before 018 is applied.
**Ops CLIs + cutover docs (plan Tasks 7-9) SHIPPED 2026-07-30:** `verify_pr7b_core_backfill`,
`reset_interrupted_outbox_claims`, `repair_outbox_sequence` (all subprocess-tested against real
Postgres, incl. the SHARE-lock retention race and the schema-012 restore-acceptance contract),
the byte-identical RUNBOOK/DEPLOYMENT cutover section (pinned by
`tests/unit/test_docs_cutover_parity.py`), and the exact documented rollback command proven
against a real head DB (`tests/integration/test_rollback_command.py`, head/sentinel DERIVED,
never transcribed).

### PR 7b-activation — Platform-authoritative decision ordering (item 8, part 2)
Migration **024** (`down_revision='023'`): `outbox.failure_class`; the `outbox_ordering_activation`
phase singleton (`legacy→bootstrap_in_progress→bootstrapped→active`, ordered timestamps, 64-hex
digests, no reverse transition) with kind-typed FKs to an **immutable `BYTEA`
`outbox_ordering_bootstrap_artifacts`** table (UPDATE/DELETE-refusing trigger). Puts
`decision_sequence` **on the wire** (emission decided at the publisher via a shared runtime phase
reader, checked before claim and before HTTP, fail-closed; flag uniform across all activation-reading
processes before CAS); **platform per-case high-water is the ordering authority**, seeded by an
**authenticated bootstrap** (signed request + **signed response envelope**) from the platform's
accepted-run ledger (reconciling manual/reverted state) and sticky thereafter; a wire-tuple mismatch
is a distinct non-retryable `integrity_mismatch` terminal (never `superseded`). **Four** single-purpose
CAS CLIs (`export_outbox_ordering_manifest`, `begin_outbox_ordering_bootstrap`,
`record_platform_ordering_bootstrap`, `activate_outbox_ordering`) + a phase recovery matrix, modeled on
`activate_bundle_pinning_epoch`; drained activation window (publishers to zero, pre-armed flag=true);
**forward-only-after-use** downgrade; **two-phase irreversible** rollback (startup refuses
`phase=active` with flag off). Supplies the ordering guarantee 6b's coordinator callbacks consume; 6b
may build on 7b-core's primitive but not activate until `phase='active'` (ADR-008).

> **REQUIRED BEFORE ANY 024 PLAN/CODE — activation process-role matrix (re-audit `d569a15..4938840`
> F13).** "publishers to zero" is incomplete: author ONE machine-parsed matrix (revision=024,
> parent=023, owner, and the explicit stopped/running state + order for EVERY writer role —
> API, pipeline, outbox publisher, dev-worker — plus retention, target image and flag state) and have
> the ROADMAP/spec/plan all consume it. A guard must fail on a removed/aliased/duplicated/negated role,
> a role left running, or a wrong revision/owner — token-presence ("keep pipeline and API online") is
> not a contract. Deferred to 024 authoring (024 does not yet exist); recorded here so it gates that
> unit.

### PR 7b-inputs — Platform answer artifacts — FUTURE, reserved unbuilt (audit fold `4c3015a..cccd5f7` F11)
Not started and not scheduled; exists so O1-O4 resolution has a named owner. Scope when
designed: a versioned answer-artifact schema (owner, approval, signature, and per-obligation
typed payloads — principal→key binding verified against a real key registry, negotiated
finite maxima with units, a closed recovery-mechanism vocabulary, a governed release-id
allocator registry, and the one exact canonical process-role matrix the blockquote under
PR 7b-activation defers to 024 authoring) plus the authority that validates and signs it.
Until it ships, the acceptors on `WIRE.ORDERING.PENDING_INPUTS` are content SCREENS only and
`resolution_problems` refuses every artifact. Shipping it MUST replace
`ANSWER_ARTIFACT_SCHEMA` and rewrite `resolution_problems` in the same change — the tripwire
branch and the claim's verifier enforce exactly that. No migration; none reserved.

### PR 6b — Revalidation (item 7B) — PENDING, required for M4
Migration **025** (`down_revision='024'`). Revalidate immutable evidence under the run's pinned
bundle+engine, write **superseding** checks; a tightened pass rule marks prior PASSes stale until
revalidated; block rollout activation until successors exist. Consumes PR 7b-activation's exact
convergence contract (greatest per-case sequence platform-acknowledged, incl. `dead` as a hard
blocker); rev 6 must separately prove a superseded coordinator's higher delivered decision was under
the target validator pair (7b ordering ≠ freshness).

### PR 7a — Queue lease fencing (item 9)
Migration **026** (`down_revision='025'`): `jobs.lease_token`. Every transition gated on
`(id, locked_by, lease_token, status='running')`; heartbeat ≤ lease/3; cadence
reaper fails the run in the **same txn**; fenced complete inside the decide txn
(stale worker rolls back the decision). The interim nonce's transaction-HELD
authority (`assert_live` FOR UPDATE, R9 fold) must be preserved, not merely
renamed. Migration 026 also carries the per-case single-runner DB backstop
(re-audit `750630c..ca85355` R10-F1): partial unique index
`jobs(case_id) WHERE status='running'` (NULL case_id exempt), so no future code
path — recovery included — can put two same-case jobs in `running` again.

### PR 8 — Object-store containment + immutable evidence (item 10)
Migration 027. Structured `ObjectRef{key, sha256, size?, content_type?, version_id?}`
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

**PR 9c acceptance criteria for a real email provider (re-audit `d569a15..4938840` F14 — a real
sender must not ship without these; production currently REFUSES the stub, so this is a future gate,
not a live hole):** (1) an ENFORCEABLY bounded provider attempt (hard per-send timeout, not a
best-effort client default); (2) a stable, outbox-row-derived idempotency key sent to the provider so
a retried send cannot double-deliver; (3) a POC-specific claim lease strictly greater than the
provider budget + DB accounting margin (the existing lease-vs-deadline invariant, applied to the
email path); (4) explicit ambiguous-success / retry semantics (a timeout after the provider may have
accepted must not silently double-send); (5) a two-publisher stale-claim proof (no double delivery
under overlap); (6) bounded resource saturation (connection/handle caps); (7) clean, bounded process
exit with a HUNG provider. **Plus a static gate:** enabling a non-stub `email_provider` is IMPOSSIBLE
(production boot refuses) until the provider interface and the tests above exist — the enablement and
its proof land in the same change.

### PR 10 — Production ops hardening (item 13)
**Split (2026-08-02, `.agents/superpowers/plans/2026-08-02-pr10a-ops-hardening-core.md`): 10a
shipped the non-migration core** — `adapters/retry.py` wired into the httpx adapters, bounded+declared
24h metrics windows, `/v1/metrics.prom` + `docs/ALERTS.md`, RUNBOOK raw-requeue-SQL repair (+
governance test), base-image digest pin + `requirements.lock` constraints, durable `core.hooksPath`.
**10b keeps** the reserved migration 028 (broker snapshots + the audit-promised
`outbox.attempts>=0` / `jobs.attempts>=0` / `jobs.max_attempts>=1` CHECKs + the cutover-attestation
CAS record — re-audit `3db5f13..a7df17b` F6: a DB record of (setting, reviewed target, epoch) with
an orchestrator-inventory receipt that every publisher compares its live value/image against before
claiming; until it lands the env-pair gate is a per-process mitigation and the drained STOP/ATTEST-
ZERO steps remain the fleet control), the recalc broker gate +
ADR-007, `evidence.refresh_requested`, the contracted rollout-observation endpoint, and tree-wide
format adoption. 10b also owns two R10 structural residuals (re-audit `750630c..ca85355`):
(a) the **supervised external-call executor** — an unconditionally-killable wall-clock boundary
(child process or proven async cancellation scope) around governed upstream calls; until it lands,
the governed transport proves the absolute deadline after headers, per body chunk, and at EOF, but
worker OCCUPANCY during a hostile header drip is bounded only by the tightened inactivity phase +
h11 header caps (nothing past the deadline is returned or persisted); and (b) **ExternalCallAuthority
unification** — one transitive gateway type that HTTP, object-store, OCR, and provider-protocol
calls all accept (today the governed helper + `authorize_external_io()` + bounded store reads cover
every known call site, enforced by the static transport guard, but the authority is per-call-site
convention rather than a single injected capability).
Migration 028: broker **full-list immutable snapshots** `(revision, sha256,
json_bytes, author, timestamp)` — NOT per-entity versioning (6 entities; snapshots
reproduce matches AND non-matches, simpler); run records matched entity + snapshot
revision. `adapters/retry.py` (transient classification + Retry-After — job-layer
backoff already exists); `recalculate.requested` runs the broker gate (record
`AUDIT:<id>` + ADR-007 — spec limits recalc to "no adapter calls", but the gate is a
local lookup); new `evidence.refresh_requested` in a **local extension contract**;
RUNBOOK: delete the broken self-select SQL → point to the existing authenticated
requeue endpoint; metrics windowing (index exists) + Prometheus + alerts; pin deps +
base image `@sha256`; `ruff format --check` in CI; wire `core.hooksPath` durably in
`manage.sh`.

**Rollout observation unit (was the shadow `automation_readiness` gauge; removed from
`/v1/metrics` in the `d569a15..4938840` re-audit fold — F2/F3/F4).** Rebuild it as a
CONTRACTED surface, not a raw diagnostic: a dedicated authenticated endpoint with (a)
one versioned population + explicit denominator per metric (`schema_version`,
`non_gating=true`, `as_of`, fixed window, engine/policy cohort, zero-filled enum
categories, an explicit unknown/legacy-provenance bucket); (b) set-based single-statement
aggregation with a purpose-built index and a statement-time budget (the removed version
sequentially scanned full history and ran a correlated manual subplan per case); (c) a
snapshot-consistent read (one SQL statement with shared CTEs, or a `REPEATABLE READ`
read-only transaction) so one response is one database snapshot. This is the
"measure-before-enforce" input to M2; it must not ship until it meets this contract.

**Typed ops `ShapeContract` (re-audit `d569a15..4938840` F9).** `binding.bind(require_columns=…)` is
column/relation PRESENCE only (reset now lists every column it touches, incl. `status`). Build a
per-command typed contract — relation identity + each referenced column's type/nullability/default +
the constraint/trigger/function set + sequence ownership — consumed IDENTICALLY by each command's
prerequisite, diagnostic and mutation paths under its lock, so a diagnostic (e.g.
`verify_pr7b_ops_prerequisites`) cannot report success on a shape (`decisions` dropped, `claim_token`
nullability changed) that the mutation then tracebacks or half-applies on. RED matrix: missing
table/column, wrong type/nullability/default, removed/changed constraint or trigger, wrong sequence
binding — all refused, stable and non-mutating — against every genuinely supported revision.

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
policy bundle pinning)**, ADR-006 (PR 6b: revalidation under the pinned
engine — validator-axis staleness + replay), ADR-007 + `AUDIT:` (PR 10:
broker gate on recalculate — shifted from ADR-006 now that PR 6b owns it),
**ADR-008 (PR 7b: platform-authoritative decision ordering — `decision_sequence`
on the wire + platform per-case high-water dedupe; ADR-006/007 left intact)**,
`AUDIT:` (local `evidence.refresh_requested` extension), `AUDIT:`
(OpenAPI as derived contract).
