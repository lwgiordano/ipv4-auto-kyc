# PR 7b-activation — Platform-authoritative decision ordering (item 8, part 2) — design (rev 2)

## Context

PR 7b was split (user decision, 2026-07-22) into **7b-core** (migration `013`, **pending/planned** —
not yet shipped: stream separation, an internal per-case `decision_sequence` + locked counter, a
**best-effort local** `superseded` guard, a **fenced** claim, per-case + triple-identity constraints,
status/lifecycle CHECKs) and **7b-activation** (this doc, migration `014`, `down_revision='013'`).
7b-core closes the mixed-FIFO defect locally and **mitigates** the requeue revert with a best-effort
guard that fires **only when a higher delivery was locally stamped** — the **single-publisher
send-before-stamp revert AND the cross-replica revert both remain open** for this unit's platform
high-water. 7b-core explicitly does **not** make the platform authoritative.
7b-activation does: it puts `decision_sequence` **on the wire** and makes the platform's **sticky
per-case high-water the ordering authority**, closing the cross-replica race (publisher A sends seq 2,
then a requeued seq 1 is sent by B in the claim/HTTP/stamp gap — the tool cannot close this locally,
because the publisher commits its claim `publisher.py:146-148`, HTTPs outside any txn `151-152`, and
stamps `published_at` in a second txn `160-191`).

This carries the contracts hardened across the pre-split Codex reviews (rev 2-5): the platform
high-water authority, a drained authenticated activation cutover, a candidate/accepted-ledger
reconciliation, an executable phase state machine with immutable signed artifacts, and the exact 6b
convergence contract. It is ROADMAP item 8 (part 2) and **ADR-008**. The accepted architecture was
Codex-confirmed three times: **the platform sticky high-water is authoritative; local state fails
closed; PR 6b gets a truthful convergence witness.** PR 6b's *activation* consumes this unit.

## Decisions

1. **Wire emission** — `decision_sequence` (allocated in 7b-core, internal until now) is emitted,
   decided **at the publisher** via a shared **runtime phase reader**, checked before claim and before
   HTTP, fail-closed. The flag is uniform across all activation-reading processes before the CAS.
2. **Platform sticky high-water is the ordering authority**, seeded by an **authenticated bootstrap**
   (signed request **and** signed response envelope) from the platform's **accepted-run ledger**
   before emission, reconciling manual/reverted state; sticky thereafter.
3. **Executable activation authority** — a phase state machine (`legacy → bootstrap_in_progress →
   bootstrapped → active`) driven by **four single-purpose CAS CLIs** with a recovery matrix; the
   bootstrap artifacts are verbatim, immutable, kind-typed.
4. **`integrity_mismatch`** — the pre-HTTP wire-tuple check (`case_id`/`run_id`/`decision_sequence`
   across decision/outbox/JSON) yields a distinct non-retryable terminal, never `superseded`.
5. **Drained activation window** — publishers at zero through bootstrap; forward-only-after-use
   downgrade; two-phase irreversible rollback; startup refuses `phase='active'` with the flag off.

## Architecture

### 1. Migration 014 (`down_revision='013'`)

Adds only what activation needs (7b-core's `013` already carries stream/sequence/claim/identity):

- **`outbox.failure_class TEXT NULL`** — set only on an `integrity_mismatch` terminal; NULL for
  ordinary retryable/exhausted `dead`. CHECK: `failure_class IS NULL OR failure_class =
  'integrity_mismatch'`; `failure_class='integrity_mismatch' ⇒ status='dead' AND resolved_at NOT NULL
  AND delivered_at NULL AND claim_token NULL`.
- **`outbox_ordering_activation` singleton** (`id smallint PK DEFAULT 1 CHECK (id=1)`): `phase TEXT
  NOT NULL DEFAULT 'legacy' CHECK (phase IN ('legacy','bootstrap_in_progress','bootstrapped',
  'active'))`; ordered `bootstrap_started_at`/`bootstrapped_at`/`activated_at`;
  `request_digest`,`response_digest` (`CHECK ~ '^[0-9a-f]{64}$'` when present). Phase CHECKs:
  `bootstrap_in_progress` requires `request_digest` + the request-artifact FK; `bootstrapped`/`active`
  require both digests + both artifact FKs + ordered timestamps; a trigger forbids reverse/skip
  transitions. Seeded `phase='legacy'`.
- **`outbox_ordering_bootstrap_artifacts`** (`kind TEXT CHECK (kind IN ('request','response')),
  digest TEXT CHECK (~ '^[0-9a-f]{64}$'), body_bytes BYTEA NOT NULL, created_at, UNIQUE(kind,
  digest)`) — the **`BYTEA` is the authority** (verbatim canonical bytes; any JSON view is derived,
  never hashed); a trigger **rejects UPDATE/DELETE** (immutable). The singleton's two FKs are
  **kind-typed** (`request_digest → (kind='request')`, `response_digest → (kind='response')`) so a
  request slot cannot be filled by a response artifact.
- **Downgrade — forward-only-after-use:** raises if `phase != 'legacy'` (the sequence namespace +
  activation epoch are externally observed once emission starts); else drops cleanly. `up→down→up`
  green on a fresh (legacy) schema.

### 2. Wire emission at the publisher + shared phase reader (F1-heritage)

**Payload ownership boundary (rev-6 P3):** 013 (7b-core) writes `decision_sequence` **only to the
`decisions`/`outbox` columns** and keeps `payload_json` **and the HTTP body byte-identical** to pre-7b
— it puts nothing on the wire and owns no phase reader. **014 owns the callback-JSON field**: it
(a) **backfills** the internal field into every surviving legacy `decision_callback.payload_json` from
the FK-bound column, and (b) **adds** it to newly-enqueued callback payloads. Neither the pipeline nor
013 flag-gates anything. All emission lives at `_deliver_decision_callback` (`publisher.py:82-122`),
governed by `read_ordering_phase(session) → (phase, flag)`:

| phase / flag | claim + send behavior |
|---|---|
| `legacy` (no bootstrap) | strip the field; deliver byte-identical to 7b-core |
| `bootstrap_in_progress` / `bootstrapped` | **refuse to claim or send** (publishers stopped in §Rollout; fail-closed backstop) |
| `active` AND flag=true | require a non-NULL sequence; emit it |
| any other tuple (`active`+flag=false, …) | **fail closed / halt** — never reinterpret `active` as emission-off |

Checked **before claim** and **again immediately before HTTP** (phase can advance between). Config
(`config.py:77-79`): `callback_include_decision_sequence: bool = False`; schema
(`api/schemas.py:144-164`): `decision_sequence: int | None = None`. (014's payload backfill, defined
in the ownership boundary above, is what lets a pre-flag pending/dead callback emit correctly after
activation and never strand.)

**Process role matrix:** the flag is deployed **true to every activation-reading process — API
(`create_app`), outbox worker (`build_publisher`, `outbox_worker.py:9-20`), dev worker
(`dev_worker.main`), metrics/ops — before the CAS to `active`**; phase (not a publisher-only rollout)
controls emission. Each process's startup validates its own `(phase, flag)`: **any 7b process whose
DB `phase='active'` while its own flag is false fails readiness/startup.** `/readyz`
(`app.py:93-125`) reports phase and applies this rule.

**`integrity_mismatch`:** the pre-HTTP reader compares **`case_id`, `run_id`, and `decision_sequence`**
across the decision row, outbox columns, and internal JSON. A mismatch → immediate **non-retryable**
fenced terminal (7b-core's fencing): `status='dead'`, `failure_class='integrity_mismatch'`,
`resolved_at`, **zero HTTP**, `published_at` NULL, audit with only identifiers + expected-vs-observed
hashes, metric/alert. **Never `superseded`.** The generic UI requeue (`ui/routes.py:451-476`) returns
**409** for this class until a dedicated repair reconstructs/verifies the tuple.

### 3. Platform authority — candidate manifest, reconciliation, signed bootstrap (F2/F3-heritage)

**Receiver contract:** `POST /kyc/decision` (`case_id=c`,`decision_sequence=s`): one txn — `s>h(c)`
apply+advance; `s≤h(c)` no-op+success; once initialized, a **missing** sequence is a sticky no-op.

**Candidate manifest (tool-derivable):** the tool exports every immutable callback decision
`(case_id, run_id, decision_sequence, callback_body_sha256, local_status)` over an agreed universe; it
**never invents `platform_current_run_id`** (it cannot know the platform's effective state; a manual
decision has `run_id=NULL` + no callback, `ingest.py:242-267`). New/manual-only cases carry
`no_prior_callback`.

**Platform reconciliation:** the platform returns (a) its **accepted callback-run ledger** and (b)
its **current effective source** separately (`callback:<run>`|`manual:<event>`). `h(c)` = greatest
**accepted** sequence; a current callback **below** greatest accepted (a legacy revert) → **refuse
until reconciled and re-attested** (do not merely advance the integer); a **manual** current source
stays effective while `h(c)` seeds from the ledger; exact two-sided coverage, else fail closed. §6
convergence consumes this attested mapping — never the tool's local `published_at` alone.

**Manual-current high-water floor (added by 7b-core rev-5 review, 2026-07-24 — acceptance
strengthening):** seeding `h(c)` from the *accepted ledger alone* is insufficient when the current
effective source is `manual:<event>` — an **unaccepted** pending/dead callback whose decision
**predates** the manual approval was never accepted, so the ledger's greatest accepted sequence can
sit below it; after activation that stale callback would deliver with `s > h(c)` and **replace the
manual-current state** (7b-core's third residual, replayed *through* the authority this unit
installs). Therefore the bootstrap MUST, for every case whose current effective source is manual,
seed `h(c)` to at least the **tool's greatest allocated `decision_sequence` for that case at
bootstrap time** (`GREATEST(ledger max accepted, max(decisions.decision_sequence))` — allocation
happens under the case lock and publishers are at zero for the whole window, so this bound is
stable), making every such older unaccepted pending/dead callback a **sticky no-op** on delivery:
the manual source stays effective, and the callback row still terminalizes locally as `delivered`
(receiver no-op success) without becoming the platform's current state. **Acceptance:** an
unaccepted pending/dead callback OLDER than a manual-current platform source can NEVER replace it
after activation; the §Testing "manual-current with an older accepted callback" scenario is
extended with the *unaccepted* pending/dead variant, and a mutation that seeds `h(c)` from the
ledger alone (dropping the allocated-sequence floor) must fail it.

**Signed response envelope:** the shipped HMAC-v2 (`security.py:52-67`) signs a *request*
(`method`,`path_qs`,…), not a response. Define one versioned signed envelope `{version, key_id,
issued_at, request_digest, response_digest, response_bytes, signature}`, signature over the
**platform→tool** direction (`security.DIRECTION_INBOUND`) covering **both** digests. The recording
CLI does key lookup, **constant-time** verify, freshness/replay policy, `request_digest` equality, and
coverage **before any DB write**. A timeout query returns a freshly signed envelope for the same
`request_digest`. **Canonical codec:** versioned compact UTF-8 JSON (sorted keys+rows, integer
sequences, no floats) + lowercase SHA-256; request/response `body_bytes` stored verbatim.

### 4. Four single-purpose CAS CLIs + phase recovery matrix

Modeled on `activate_bundle_pinning_epoch.py` + `policy_store/repo.activate_epoch:73-85` (lock row 1,
DB time, write-once, read-back, idempotent-same-digest / refuse-different):

- `export_outbox_ordering_manifest` — writes the candidate manifest, stores the **request** artifact
  verbatim, prints `request_digest`. Phase must be `legacy`.
- `begin_outbox_ordering_bootstrap --expect-request-sha` — verifies the stored request artifact, CAS
  **`legacy → bootstrap_in_progress`** (Rollout step 1, **before** the platform call).
- `record_platform_ordering_bootstrap --expect-request-sha --response-envelope <file>` — legal **only**
  in `bootstrap_in_progress`; verifies the signed envelope, stores the **response** artifact, CAS
  **`bootstrap_in_progress → bootstrapped`**.
- `activate_outbox_ordering --expect-request-sha --expect-response-sha` — legal **only** from
  `bootstrapped`; re-checks both digests against the stored artifacts, CAS **`bootstrapped → active`**.

**Recovery matrix:** `legacy` → 7b-core's reversible rollback. `bootstrap_in_progress` |
`bootstrapped` → **never** resume publishers or reverse phase; only same-digest forward to `active`,
or an approved platform undo + attested repair; on a lost response, `record_*` re-queries by
`request_digest` and proceeds same-digest. `active` → forward-only (forward-fix or approved
reconciliation).

### 5. Rollout (drained activation window) + rollback

**Activation cutover:** (1) pause platform KYC state changes **incl. manual approvals**; stop +
orchestrator-attest zero outbox publishers **and `dev_worker`**; `begin_outbox_ordering_bootstrap` CAS
→ `bootstrap_in_progress`. (2) run the external bootstrap (§3) + verify;
`record_platform_ordering_bootstrap` CAS → `bootstrapped`; publishers stay at **zero**. (3) deploy
`callback_include_decision_sequence=true` to **every** activation-reading process while publishers are
stopped; `activate_outbox_ordering` CAS → `active`; then start + attest publishers; resume. On any
timeout/anomaly, stay in the intermediate phase with publishers stopped and reconcile same-digest.
Only after `phase='active'` may PR 6b activate.

**Rollback (two irreversible phases):** before `phase` leaves `legacy` → 7b-core's schema-compatible
reversible rollback. Intermediate phases → per the recovery matrix (§4). After `active` → emission-off
is **prohibited** (a sticky-sequenced case rejects a missing sequence); rollback stays on a
sequence-capable image with the flag on; a defect is **forward-fix** or a **separately approved**
reconciliation. **Startup/readiness FAILS if `phase='active'` and any 7b process's flag is false.**

### 6. PR 6b convergence contract (the shared predicate 6b imports)

Over each case's allocated sequences, keyed to the **attested platform mapping** (§3, not local
`published_at`): let `g` = greatest sequence. Convergence for `g` holds **iff** attested `h(case) ≥ g`
**and** `g`'s outbox row is terminal-clean (`delivered`, or `superseded` only when a strictly higher
**delivered** sequence exists). `g` `pending`/**`dead`** ⇒ blocks (a dead higher callback the UI can
requeue is a hard blocker). A **missing/duplicate** callback→decision mapping ⇒ blocks. Lower `dead`
rows are safe only after `phase='active'`. A **superseded 6b coordinator** needs 6b **rev 6** to
separately prove the higher delivered decision was under the **target validator pair** (7b proves
order, not freshness). The shared invariant query also rejects a superseded/integrity row whose
linked decision is `published`. Ships as the shared query 7b-activation owns and 6b imports.

## Invariants

- Emission is a **drained window**, never a live per-producer flag flip; no stripped callback is sent
  into an initialized sticky receiver and then locally stamped delivered. The flag is uniform across
  activation-reading processes before the CAS; each process fails closed on `active`+flag-false.
- Ordering authority is the platform sticky high-water, seeded by the **signed** bootstrap
  (request+response) reconciling manual/reverted state. Activation is an executable phase machine
  (four CAS CLIs, kind-typed immutable `BYTEA` artifacts, recovery matrix); the same runtime phase
  reader governs API/publisher/dev-worker/metrics/6b, checked before claim and before HTTP.
- `integrity_mismatch` (non-retryable, alert-raising, UI-409) is distinct from 7b-core's `superseded`.
  Delivery still stamps `published_at` + run `COMPLETE`. At-least-once preserved.
- Downgrade is **forward-only-after-use** (refuses once `phase != 'legacy'`); rollback is two-phase
  irreversible. **No `ENGINE_BUILD_ID`/scoring change** (delivery-layer only; drift guard re-pins with
  no bump). `KYC_Tool_Build_Package/` + **M2** untouched.

## Testing strategy (real Postgres, each with a mutation witness)

- **Migration 014:** `failure_class` + activation singleton + artifacts; direct SQL for every illegal
  phase tuple (`active`/`bootstrapped` without digests/artifacts, reverse transition, out-of-order
  timestamps) fails; `integrity_mismatch` lifecycle CHECK enforced; `up→down→up` clean on a legacy
  schema; downgrade **refuses** once `phase != 'legacy'`.
- **Payload-ownership boundary (rev-6 P3):** after **013**, the DB `payload_json` **and** the wire body
  contain **no** `decision_sequence`; after the **014** migration, existing pending/dead
  `decision_callback.payload_json` contains the FK-bound value and a newly-enqueued 014 callback
  contains it internally; `legacy` still emits the pre-7b bytes; `active` emits it. Mutations —
  adding the JSON field in 013, or omitting either the 014 legacy-backfill or the 014 new-enqueue
  writer — must fail.
- **Wire emission + silent-loss window:** create pending **and** dead callbacks while `legacy`; enter
  `bootstrap_in_progress`; barrier an old `legacy`/false-flag publisher immediately before HTTP and
  prove **zero HTTP + no delivered/published stamps**; CAS `active` + start only true-flag publishers →
  the queued callback (with its backfilled sequence) applies **once**. Mutations that omit `dev_worker`,
  permit a legacy send in an intermediate phase, or stamp local delivery on a receiver no-op must fail.
- **Signed envelope:** body tamper, signature tamper, wrong key/direction, envelope replayed from
  another `request_digest`, stale envelope → all rejected, phase/artifacts unchanged; valid same-digest
  timeout recovery succeeds.
- **Four-CLI phase machine:** every CLI out of order refuses byte-stable; lost-response query recovery;
  same-digest retry idempotent; different-digest refuses.
- **Artifacts:** non-ASCII bytes and alternate JSON whitespace/key-order round-trip **verbatim** via
  `BYTEA`; wrong content under a valid-looking digest rejected; wrong artifact `kind` for a slot
  rejected (kind-typed FK); direct UPDATE/DELETE refused (trigger).
- **Process role matrix:** real `create_app`/`build_publisher`/`dev_worker` startup/readiness at
  `legacy`, both intermediate phases, and `active` with flag false/true — `active`+false fails
  readiness on every 7b process; the pre-armed uniform deployment passes.
- **Bootstrap reconciliation:** seq2-then-seq1 revert; manual-current with an older accepted callback;
  **manual-current with an older UNACCEPTED pending/dead callback (rev 2): the floor makes it a
  sticky no-op — a mutation seeding `h(c)` from the ledger alone (dropping the allocated-sequence
  floor) must fail**; manual-only; send-before-stamp — each yields one deterministic `h` or a named
  block; a mutation using the tool's local-delivered row as platform truth fails.
- **`integrity_mismatch`:** tuple tamper → exact terminal (zero HTTP, `dead`, `resolved_at`, no
  `published_at`, metric/alert) + UI **409**; a genuine lower row under a higher delivered decision →
  `superseded` only (7b-core).
- **6b convergence:** greatest `dead` ⇒ false; lower superseded by delivered higher ⇒ true;
  missing/duplicate mapping ⇒ blocked; coordinator superseded by a higher **pre-target** ⇒ false.
- **Lineage:** `tests/unit/test_migration_lineage.py` green after the split renumber.
- **Gate:** `./manage.sh lint`; the real lineage test; targeted real-Postgres
  migration/outbox/bootstrap/ops/UI tests; then `./manage.sh test`.

## Docs + governance (in the build)

`PLATFORM_INTEGRATION.md` (receiver contract + candidate manifest + reconciliation + HMAC-v2 request +
**signed response envelope**), `DEPLOYMENT.md`/`RUNBOOK.md` (drained activation window, orchestrator
attestation, the **four CLIs** + recovery matrix, process role matrix, two-phase rollback +
startup-refusal + preflight queries), `OVERVIEW.md`, **ADR-008**, `AUDIT_FINDINGS.md` (activation +
reconciliation semantics), `.agents/ROADMAP.md` (the split + renumber + ADR-008, this commit).

## Out of scope (YAGNI)

- Everything in 7b-core (stream separation, the internal sequence, the best-effort local `superseded`
  guard, the fenced claim, the per-case + identity constraints) is a **prerequisite**, planned in
  `013` (not yet shipped).
- No enforcement/scoring/`ENGINE_BUILD_ID` change; M2 untouched. `event_sequence` (D1, PR 2) keeps its
  `_callback_body` gate. PR 7a fences the **jobs** queue. PR 6b consumes this unit's ordering
  authority; it may build on 7b-core's primitive but not activate until `phase='active'`.

## Revision note — rev 2 (2026-07-24)

Acceptance strengthening from the 7b-core rev-5 complete-unit review (finding 4): §3 gains the
**manual-current high-water floor** — when a case's current effective source is manual, the
bootstrap seeds `h(c)` to at least the tool's greatest allocated `decision_sequence`, so an
**unaccepted** pending/dead callback older than the manual-current source is a sticky no-op after
activation and can never replace it. The §Testing reconciliation scenario is extended with the
unaccepted-callback variant and a ledger-only-seed mutation witness. No other change.
