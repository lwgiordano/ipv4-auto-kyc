# PR 7b — Outbox stream separation + platform-authoritative decision ordering (item 8) — design (rev 5)

## Context

PR 7b is reordered **before PR 6b** because it supplies the callback-ordering guarantee 6b's
coordinator callbacks rest on (Codex PR-6b rev-5 F3). The outbox (`src/kyc_tool/outbox/publisher.py`)
has two present defects: (1) `_CLAIM_SQL` (`publisher.py:29-49`) claims the min-id pending row **per
`case_id`**, mixing `decision_callback` + `poc_email`, so a stuck email blocks the case's decision
callbacks; (2) callbacks are ordered only by outbox `id`, so a requeued older decision can be
delivered after a newer one and **revert** the platform (delivery stamps `decisions.published_at` +
run `COMPLETE`, `_record_delivered`, `publisher.py:160-191`; 6b's gate reads this).

**Accepted architecture (Codex-confirmed across rev 2-4):** the platform's **sticky per-case
high-water is the ordering authority**; local state **fails closed** and gives PR 6b a **truthful
convergence witness**. Rev 5 keeps that goal and closes the remaining seven *implementation*
contracts (not architecture): the bootstrap is **four single-purpose CAS CLIs** with a full phase
recovery matrix (F1); the outbox claim carries a **fencing token** so a stale claimant cannot
overwrite the reclaiming publisher's terminal (F2); the bootstrap **response is a signed envelope**
bound to the request (F3); "one callback per automatic run" is a **partial unique index** plus a
runtime fail-closed invariant (F4); bootstrap artifacts are **verbatim `BYTEA`, immutable,
kind-typed** (F5); the emission flag is deployed to **every activation-reading process** before the
CAS (F6); and the **outbox status/lifecycle tuples are DB CHECKs** (F7). This is ROADMAP item 8.

### Revision history

Rev 1 (DB-only guard, rolling migration) → wrong authority + unclaimable NULLs. Rev 2 → platform
high-water authoritative, drained transactional cutover, legacy sequencing, `superseded` lifecycle,
integrity. Rev 3 → emission at the publisher, production bootstrap, two-phase rollback, shipped
recovery reuse, exact 6b convergence, ROADMAP detail renumber. Rev 4 → executable activation
(phase state machine + artifacts + CLIs), claim-lease/retry separation, triple identity,
`integrity_mismatch` terminal. **Rev 5 (this doc)** → the seven contracts above.

## Decisions (rev 5)

1. Stream separation — `outbox.ordering_stream` (`decision`|`email`), NOT NULL; claim FIFO per
   `(case_id, ordering_stream)`.
2. Per-case `decision_sequence` from the locked counter (`cases.last_decision_sequence`, never
   `max()+1`), callback-emitting decisions only; carried in the internal payload.
3. Platform sticky high-water is the ordering authority, seeded by an **authenticated bootstrap**
   (signed request **and** response) before emission.
4. Emission decided at the publisher via a shared **runtime phase reader**, checked before claim and
   before HTTP, fail-closed; the emission flag is uniform across all activation-reading processes.
5. Activation is an **executable phase state machine** driven by **four single-purpose CAS CLIs**
   with a documented recovery matrix; artifacts are verbatim, immutable, kind-typed.
6. The outbox claim carries a **per-claim fencing token**; every terminal transition fences on it.
7. `superseded` (genuine higher-delivered obsolescence) is distinct from `integrity_mismatch`
   (non-retryable corruption); all status/lifecycle tuples are DB CHECKs.
8. Two drained windows (migration cutover; activation cutover); forward-only-after-use downgrade;
   two-phase irreversible rollback.

## Architecture

### 1. Migration 013 — drained cutover (`down_revision='012'`)

**7b owns `013`** (ROADMAP table + detail renumbered this commit: 7b `013` / 6b `014` / 7a `015` /
8 `016` / 10 `017`). Precondition (§Rollout A): platform paused, **zero** app writers attested at the
orchestrator level (not `pg_stat_activity` — no `application_name`, `session.py:16-17`); the shipped
job + new outbox recovery one-shots already run. One ordinary transaction (writers stopped, no
`CONCURRENTLY`).

**Stream / sequence / terminal columns**
- `outbox.ordering_stream TEXT` — nullable → backfill by kind → assert zero NULL/unknown →
  add+VALIDATE `CHECK (ordering_stream IS NOT NULL AND ordering_stream IN ('decision','email'))`.
- `outbox.{decision_sequence BIGINT NULL, resolved_at TIMESTAMPTZ NULL, failure_class TEXT NULL}`,
  `decisions.decision_sequence BIGINT NULL`, `cases.last_decision_sequence BIGINT NOT NULL DEFAULT 0`.

**Claim lease + fencing token (F2/F4-rev4)**
- `outbox.{claim_lease_expires_at TIMESTAMPTZ NULL, claim_token UUID NULL, claimed_by TEXT NULL}`.
  The claim atomically **replaces `claim_token`** (a fresh random UUID, not a process name), sets the
  lease, and returns the token. `next_attempt_at` stays the *retry due time*. CHECK pairs them:
  `(claim_token IS NULL) = (claim_lease_expires_at IS NULL)`.

**Triple identity + one-callback-per-run (F5-rev4 + F4-rev5)**
- `UNIQUE decisions(run_id)` (multiple NULL manual rows legal); `UNIQUE decisions(run_id, case_id,
  decision_sequence)`; triple FK `outbox(run_id, case_id, decision_sequence) → decisions(...)` (no
  NULL member on a decision-callback row → always enforced, closing the NULL-case_id bypass).
- **`CREATE UNIQUE INDEX ON outbox(run_id) WHERE kind='decision_callback'`** — the FK gives "each
  callback references a decision"; this partial index gives **at-most-one callback per run**
  (the FK cannot). *At-least-one* is a **runtime fail-closed invariant** (below), not a commit
  constraint (absence violates no FK).
- CHECK on `decisions`: `(manual=false → run_id NOT NULL AND decision_sequence > 0) AND (manual=true
  → run_id NULL AND decision_sequence NULL)`.

**Status vocabulary + lifecycle-tuple CHECKs (F7)** — `outbox.status` is currently unconstrained text
(`tables.py:272`). Add:
- `CHECK (status IN ('pending','delivered','dead','superseded'))`.
- `CHECK (kind='decision_callback' → ordering_stream='decision' AND case_id NOT NULL AND run_id NOT
  NULL AND decision_sequence > 0) AND (kind='poc_email' → ordering_stream='email' AND run_id NULL AND
  decision_sequence NULL)`.
- Same-row lifecycle tuples: `integrity_mismatch → status='dead' AND resolved_at NOT NULL AND
  delivered_at NULL AND claim_token NULL`; `status='superseded' → resolved_at NOT NULL AND
  delivered_at NULL AND claim_token NULL`; `status='delivered' → delivered_at NOT NULL AND claim_token
  NULL`; `status='pending'` may carry a paired claim token/lease; ordinary exhausted `dead` has
  `failure_class NULL`; `failure_class` is NULL or the governed enum (`'integrity_mismatch'`).
- `DecisionRow.published_at` is **cross-table** and cannot be a plain CHECK: every terminal
  transition is one guarded SQL txn asserting rowcount, and the shared invariant query (below)
  rejects a superseded/integrity row whose linked decision is published.

**Activation state machine + immutable typed artifacts (F1/F3/F5)**
- `outbox_ordering_activation` singleton (`id smallint PK DEFAULT 1 CHECK (id=1)`): `phase TEXT NOT
  NULL DEFAULT 'legacy' CHECK (phase IN ('legacy','bootstrap_in_progress','bootstrapped','active'))`;
  ordered `bootstrap_started_at`/`bootstrapped_at`/`activated_at`; `request_digest`,`response_digest`
  (`CHECK ~ '^[0-9a-f]{64}$'` when present). Phase CHECKs: `bootstrap_in_progress` requires
  `request_digest` + FK to the request artifact; `bootstrapped`/`active` require **both** digests +
  both artifact FKs + ordered timestamps; a trigger forbids any reverse/skip transition.
- `outbox_ordering_bootstrap_artifacts(kind TEXT CHECK (kind IN ('request','response')), digest TEXT
  CHECK (~ '^[0-9a-f]{64}$'), body_bytes BYTEA NOT NULL, created_at, UNIQUE(kind, digest))` — the
  **`BYTEA` is the authority** (verbatim canonical bytes; any JSON view is derived, never hashed). A
  **trigger rejects UPDATE/DELETE** (immutable). The singleton's two FKs are **kind-typed**:
  `(request_kind='request', request_digest) → UNIQUE(kind,digest)` and likewise for response, so a
  request slot cannot be satisfied by a `kind='response'` artifact.

**Historical backfill (rev-2/3/4, kept)** — per case `decision_sequence = row_number() OVER (PARTITION
BY case_id ORDER BY decided_at, id)` for callback decisions (`manual=false AND run_id NOT NULL`);
manual NULL; bind every `decision_callback` outbox row (incl. **dead**) via `run_id`; seed
`last_decision_sequence=max`; backfill the sequence into each existing `decision_callback.payload_json`.
Preflight refusals (raise, roll back): orphan/dup-run callback, sequence mismatch, remaining NULL
decision-stream sequence, sequenced email row, `>1` callback per automatic run, automatic decision
with NULL run / non-positive sequence.

**Downgrade — forward-only-after-use** (raises if any `decision_sequence`, `last_decision_sequence>0`,
or `phase != 'legacy'`); else drops cleanly (`up→down→up` green on a fresh DB). Add
`ix_outbox_stream_claim (case_id, ordering_stream, status, next_attempt_at)`.

### 2. Stream-scoped claim + fenced lease (publisher)

`_CLAIM_SQL` gains `AND o2.ordering_stream = o.ordering_stream` (never UNKNOWN — NOT NULL); requires
`(claim_lease_expires_at IS NULL OR claim_lease_expires_at <= now())`; on claim sets **only** a fresh
`claim_token = gen_random_uuid()`, `claim_lease_expires_at = now()+lease`, `claimed_by`, and
**RETURNs the token** (leaving `next_attempt_at` as the retry due time).

### 3. Decision-sequence allocation (decide transaction)

Seam `pipeline._decide_txn` (`pipeline.py:351-518`) holds the Case `FOR UPDATE` from `_load` (`:201`)
across `DecisionRow` build (`:485-495`) and enqueue (`:506`). A callback-emitting decision increments
`cases.last_decision_sequence` under the lock, stamps `DecisionRow.decision_sequence`, and passes it to
`enqueue_decision_callback(..., decision_sequence=seq)` (writes the outbox row + `payload_json`).
Manual approve (`ingest._handle_manual_approve`, `ingest.py:242-267`, `run_id=None`, no callback)
allocates no sequence. Decision + enqueue stay in the one existing transaction (so the partial unique
index enforces at-most-one atomically).

### 4. Emission at the publisher + shared phase reader + fenced terminals (F2/F6)

The pipeline always writes the sequence to the internal payload; emission lives at
`_deliver_decision_callback` (`publisher.py:82-122`), governed by `read_ordering_phase(session) →
(phase, flag)`:

| phase / flag | behavior |
|---|---|
| `legacy` | strip the field; deliver byte-identical to today |
| `bootstrap_in_progress` / `bootstrapped` | **refuse to claim or send** (publishers are stopped in §Rollout B; this is the fail-closed backstop) |
| `active` AND flag=true | require a non-NULL sequence; emit it |
| any other tuple (e.g. `active`+flag=false) | **fail closed / halt** — never reinterpret `active` as emission-off |

Checked **before claim** and **again immediately before HTTP** (phase can advance between).

**Fenced terminals (F2):** `_record_delivered`, `_record_failure`, `_record_superseded`, and the
integrity terminal update `WHERE id=:id AND status='pending' AND claim_token=:token`, **assert
exactly one row**, and clear `claim_token`/`claim_lease_expires_at`/`claimed_by` atomically. A
zero-row stale completion (the claimant's lease expired and another publisher reclaimed + finished)
is an **audited/metric no-op** that **must not** stamp the run/decision or overwrite the terminal.
This fences the exact bug where publisher A blocks in HTTP, its lease expires, B reclaims + delivers,
and A resumes and would otherwise write `dead` over a delivered row. (This outbox fence belongs in
7b — it introduces the claim lease; PR 7a fences the *jobs* queue.)

**Process role matrix (F6):** the emission flag `callback_include_decision_sequence`
(`config.py:77-79`, default false) is deployed **true to every PR-7b process that reads activation
state — API (`create_app`), outbox worker (`build_publisher`), dev worker (`dev_worker.main`), and
any metrics/ops process — before the CAS to `active`**. Phase (not a publisher-only config rollout)
controls emission; each process's startup validates its own `(phase, flag)`: **any 7b process
whose DB `phase='active'` while its own flag is false fails readiness/startup.** `/readyz`
(`app.py:93-125`) reports phase and applies this rule.

Config/schema: `callback_include_decision_sequence: bool = False`; `DecisionCallback.decision_sequence:
int | None = None` (`schemas.py:144-164`, preserved on validate).

### 5. Platform authority — candidate manifest, signed bootstrap, four CLIs (F1/F3)

Documented in `PLATFORM_INTEGRATION.md` + **ADR-008** (ADR-006 reserved for 6b, ADR-007 for PR 10).

**Receiver contract:** `POST /kyc/decision` (`case_id=c`,`decision_sequence=s`): one txn — `s>h(c)`
apply+advance; `s≤h(c)` no-op+success; once initialized, a **missing** sequence is a sticky no-op.

**Candidate manifest (tool-derivable, F2-rev4):** the tool exports every immutable callback decision
`(case_id, run_id, decision_sequence, callback_body_sha256, local_status)` over an agreed universe;
it never invents `platform_current_run_id`; new/manual-only cases carry `no_prior_callback`.

**Platform reconciliation (F2-rev4):** platform returns (a) its **accepted callback-run ledger** and
(b) its **current effective source** separately (`callback:<run>`|`manual:<event>`). `h(c)` = greatest
**accepted** sequence; a current callback **below** greatest accepted (legacy revert) → **refuse
until reconciled**; manual current stays effective while `h` seeds from the ledger; exact two-sided
coverage or fail-closed. §7 consumes this attested mapping, not local `published_at`.

**Signed response envelope (F3):** the shipped HMAC-v2 (`security.py:52-67`) signs a *request*
(`method`,`path_qs`,…), not a response. Define **one versioned signed response envelope**:
`{version, key_id, issued_at, request_digest, response_digest, response_bytes, signature}`, where the
signature uses the **platform→tool** key/direction (`security.DIRECTION_INBOUND`) over a canonical
value **covering both `request_digest` and `response_digest`**. The recording CLI performs key
lookup, **constant-time** verify, freshness/replay policy, **`request_digest` equality**, and
coverage validation **before any DB write**. A timeout query returns a freshly signed envelope for
the same `request_digest`.

**Canonical codec:** versioned compact UTF-8 JSON (sorted keys+rows, integer sequences, no floats) +
lowercase SHA-256; request and response `body_bytes` stored verbatim in the artifact table.

**Four single-purpose CAS CLIs (F1)** (shaped like `activate_bundle_pinning_epoch.py` +
`policy_store/repo.activate_epoch:73-85`; each locks row 1, uses DB time, write-once, read-back;
idempotent-same-digest / refuse-different):
- `export_outbox_ordering_manifest` — writes the candidate manifest + stores the **request** artifact
  (verbatim bytes) + prints `request_digest`. Phase must be `legacy`.
- `begin_outbox_ordering_bootstrap --expect-request-sha` — verifies the stored request artifact, CAS
  **`legacy → bootstrap_in_progress`**. (This is Rollout B step 1 — **before** the external response
  exists.)
- `record_platform_ordering_bootstrap --expect-request-sha --response-envelope <file>` — legal
  **only** in `bootstrap_in_progress`; verifies the signed envelope (above), stores the **response**
  artifact, CAS **`bootstrap_in_progress → bootstrapped`**.
- `activate_outbox_ordering --expect-request-sha --expect-response-sha` — legal **only** from
  `bootstrapped`; re-checks both digests against the stored artifacts, CAS **`bootstrapped →
  active`**.

**Phase recovery matrix (F1):**
- `legacy` → the documented schema-compatible rollback (§Rollback).
- `bootstrap_in_progress` | `bootstrapped` → **never** resume publishers or reverse phase; may only
  resume the **same-digest** operation forward to `active`, **or** a separately-approved platform
  undo + attested repair. On a lost response, `record_*` re-queries by `request_digest` and proceeds
  same-digest.
- `active` → forward-only (forward-fix or approved reconciliation).

**Shared mapping invariant (F4):** a named query — "every automatic decision has exactly one
`decision_callback` outbox row" — is consumed by `/readyz`, activation, and §7 convergence; a
**missing** (or, defensively, duplicate) mapping makes readiness/activation/convergence **fail
closed**. (At-most-one is also the partial unique index; at-least-one is only this runtime check.)

### 6. `superseded` vs `integrity_mismatch`; the local guard

**Superseded (optimization):** after claiming a `decision`-stream row, before `_deliver`, if a
higher-sequence decision for the case is already delivered (`EXISTS(... published_at NOT NULL)`),
`_record_superseded` (fenced, §4): outbox `superseded` + `resolved_at`; `published_at` NULL; run
through the legal `PUBLISH_DECISION→COMPLETE` edge (no new RunState, `models.py:35-46`); audit.
Retention prunes it with `delivered` (`retention.py:29-35`); metrics separate, excluded from
pending/dead alerts (`routes_metrics.py:60`).

**Integrity mismatch (F7):** the pre-HTTP reader compares **`case_id`, `run_id`, and
`decision_sequence`** across the decision row, outbox columns, and internal JSON. A mismatch →
immediate **non-retryable** fenced terminal: `status='dead'`, `failure_class='integrity_mismatch'`,
`resolved_at`, **zero HTTP**, `published_at` NULL, audit with only identifiers + expected-vs-observed
hashes, metric/alert. **Never `superseded`.** The generic UI requeue (`ui/routes.py:451-476`) returns
**409** for `failure_class='integrity_mismatch'` until a dedicated repair reconstructs/verifies the
tuple.

### 7. PR 6b convergence contract (F2/F4/F7)

Over each case's allocated sequences, keyed to the **attested platform mapping** (§5, not local
`published_at`): let `g` = greatest sequence. Convergence for `g` holds **iff** attested `h(case) ≥
g` **and** `g`'s outbox row is terminal-clean (`delivered`, or `superseded` only when a strictly
higher **delivered** sequence exists). `g` `pending`/**`dead`** ⇒ blocks. A **missing/duplicate**
callback mapping ⇒ blocks. Lower `dead` rows safe only after `phase='active'`. A **superseded 6b
coordinator** needs 6b **rev 6** to prove the higher delivered decision was under the **target
validator pair** (7b proves order, not freshness). The shared invariant query also rejects a
superseded/integrity row whose linked decision is `published`. Ships as the shared query 7b owns, 6b
imports.

## Rollout — two drained windows + rollback

**A. Migration cutover (013):** digest-pinned images. (1) pause submission, edge-block composer,
disable autoscaling/restarts; (2) hard-stop API/pipeline/outbox/`dev_worker`/retention/every writer,
attest zero at the orchestrator; (3) shipped `requeue_interrupted_jobs` (decrements attempts) + the
**outbox-lease-reset** one-shot (clears only non-NULL `claim_lease_expires_at`/`claim_token`,
read-back-asserts zero, leaves retry schedules); (4) `alembic upgrade head`; (5) start API only,
probe `/readyz` (head+phase), then start+attest workers — **no mutating prod smoke** (prove on the
exact digest in staging/isolated DB); (6) resume (phase `legacy`, byte-identical delivery).

**B. Activation cutover (later, drained, F1/F6):** (1) pause platform KYC state changes **incl.
manual approvals**; stop+attest zero outbox publishers **and `dev_worker`**;
`begin_outbox_ordering_bootstrap` CAS → `bootstrap_in_progress`. (2) run the external bootstrap (§5)
+ verify; `record_platform_ordering_bootstrap` CAS → `bootstrapped`; publishers stay at **zero**. (3)
deploy `callback_include_decision_sequence=true` to **every** activation-reading process (API, outbox,
dev worker, metrics/ops) while publishers are stopped; `activate_outbox_ordering` CAS → `active`;
then start+attest publishers; resume. On any timeout/anomaly, stay `bootstrap_in_progress`/
`bootstrapped` with publishers stopped and reconcile same-digest. Only after `phase='active'` may 6b
activate.

**Rollback (two irreversible phases):** before `phase` leaves `legacy` → schema-compatible PR-7b
image only, publisher unstarted (never the pre-7b binary — its enqueue writes no `ordering_stream`,
`publisher.py:52-63`, failing 013's constraints). Intermediate phases → per the recovery matrix
(§5). After `active` → emission-off prohibited; forward-fix or approved reconciliation; **startup
fails if `phase='active'` and any 7b process's flag is false.** Document exact permitted digests +
preflight queries.

## Invariants

- Claim FIFO per `(case_id, ordering_stream)`, NOT NULL; the **fenced claim** (`claim_token` +
  `claim_lease_expires_at`) is separate from the retry schedule; every terminal transition fences on
  the token and a zero-row stale write is a no-op that never stamps run/decision.
- `decision_sequence` allocated only under `Case FOR UPDATE`, unique per case, monotonic, carried in
  the payload; each callback bound by `(run_id, case_id, decision_sequence)` to exactly one automatic
  decision (partial unique index = at-most-one; runtime invariant = at-least-one).
- Ordering authority is the platform sticky high-water, seeded by the **signed** bootstrap
  (request+response) reconciling manual/reverted state. Activation is an executable phase machine
  (four CAS CLIs, kind-typed immutable `BYTEA` artifacts, recovery matrix); the same runtime phase
  reader governs API/publisher/dev-worker/metrics/6b, checked before claim and before HTTP,
  fail-closed. The emission flag is uniform across activation-reading processes before CAS.
- Every outbox status/lifecycle tuple is a DB CHECK; `superseded` (genuine obsolescence) ≠
  `integrity_mismatch` (non-retryable, alert-raising, UI-409). Delivery still stamps `published_at`
  + run `COMPLETE`. At-least-once preserved.
- **No `ENGINE_BUILD_ID`/scoring change** (delivery-layer only; drift guard re-pins with no bump).
  `KYC_Tool_Build_Package/` + **M2** untouched.

## Testing strategy (each finding carries a mutation witness)

- **Migration 013 (F4/F5/F7 + rev-2/3/4):** seed both kinds + delivered/pending/**dead** callbacks +
  manual decisions; upgrade; assert sequences/counter/payload backfill. Direct post-migration
  negatives **fail at commit**: bad/NULL stream; NULL-case/NULL-run callback; zero/negative sequence;
  wrong-case/wrong-run same sequence; **duplicate callback per run** (partial unique index);
  sequenced email; every illegal status/lifecycle tuple (`pending`+`integrity_mismatch`,
  `superseded`+NULL `resolved_at`, `delivered`+claim token, unpaired claim token/lease). `up→down→up`
  clean on empty schema. Mutation-removing any CHECK/index/FK fails.
- **Fencing token (F2):** barrier publisher A after claim; expire its lease; B reclaims + delivers;
  release A into **both** failure and success paths — B's terminal tuple, run state, and
  `published_at` are **unchanged**; A's write affects zero rows and is an audited no-op. Mutation
  removing the `claim_token` predicate must fail.
- **Signed envelope (F3):** body tamper, signature tamper, wrong key/direction, envelope replayed
  from another `request_digest`, stale envelope → all rejected with phase/artifacts unchanged; valid
  same-digest timeout recovery succeeds.
- **Four-CLI phase machine (F1):** every CLI out of order (e.g. `record` in `legacy`, `activate` in
  `bootstrap_in_progress`) refuses byte-stable; lost-response query recovery; same-digest retry
  idempotent; different-digest refuses; direct SQL for every illegal phase tuple / reverse transition
  fails.
- **Artifacts (F5):** non-ASCII bytes and alternate JSON whitespace/key-order round-trip **verbatim**
  via `BYTEA`; wrong content under a valid-looking digest rejected; wrong artifact `kind` for a slot
  rejected (kind-typed FK); conflict-path corruption rejected; direct UPDATE/DELETE refused (trigger).
- **Process role matrix (F6):** real `create_app`, `build_publisher`, `dev_worker` startup/readiness
  at `legacy`, both intermediate phases, and `active` with flag false/true — `active`+false fails
  readiness on every 7b process; the pre-armed uniform deployment passes.
- **Mapping invariant (F4):** duplicate callback per run **fails at commit**; a **missing** callback
  makes `/readyz`/activation/§7 convergence **fail closed** (seed both through real ORM/SQL).
- **Integrity terminal + backlog split (F7):** tuple tamper → exact `integrity_mismatch` terminal
  (zero HTTP, `dead`, `resolved_at`, no `published_at`, metric/alert) + UI **409**; a genuine lower
  row under a higher delivered decision → `superseded` only. Split: pending-off→activate→emit;
  dead-off→**real UI requeue**→pending→emit.
- **Bootstrap reconciliation (F2-rev4):** seq2-then-seq1 revert; manual-current with older accepted
  callback; manual-only; send-before-stamp — each yields one deterministic `h` or a named block.
- **6b convergence:** greatest `dead` ⇒ false; lower superseded by delivered higher ⇒ true;
  missing/duplicate mapping ⇒ blocked; coordinator superseded by a higher **pre-target** ⇒ false.
- **Kept (rev-2/3/4):** stuck email never blocks the case's decision; concurrent decides → unique
  increasing sequences; legacy dead callback requeued via real UI is superseded and can't move the
  fake receiver.
- **Lineage:** `tests/unit/test_migration_lineage.py` green after §C table + detail renumber; `rg`
  shows no contradictory live allocation.
- **Gate:** `./manage.sh lint`; the real lineage test; targeted real-Postgres
  migration/outbox/bootstrap/ops/UI tests; then `./manage.sh test`.

## Docs + governance (in the build)

`PLATFORM_INTEGRATION.md` (receiver contract + candidate manifest + reconciliation + HMAC-v2 request
+ **signed response envelope**), `DEPLOYMENT.md`/`RUNBOOK.md` (two drained windows, orchestrator
attestation, the **four CLIs** + phase recovery matrix, process role matrix, two-phase rollback +
startup-refusal + preflight queries), `OVERVIEW.md`, **ADR-008**, `AUDIT_FINDINGS.md` (backfill =
deterministic reconstruction; activation + reconciliation semantics), `.agents/ROADMAP.md` (table +
detail + ADR-008 + the added 013 columns, this commit).

## ROADMAP renumber (kept; detail updated for rev-5 columns)

§C table + status prose + detail: PR 7b `013`/down `012` (item 8, **pending**; 013 also carries
`outbox.{claim_token, failure_class}`, the partial `outbox(run_id)` callback index, the status/
lifecycle CHECKs, and the kind-typed immutable artifact table), PR 6b `014`, PR 7a `015`; 8=`016`,
10=`017`. Build flips only PR 7b's State to `shipped`.

## Out of scope (YAGNI)

- No email-semantics change beyond stream separation; no per-decision content change; no cross-case
  ordering. No enforcement/scoring/`ENGINE_BUILD_ID` change; M2 untouched.
- `event_sequence` (D1, shipped PR 2) keeps its `_callback_body` gate (an ordering hint); only
  `decision_sequence` moves to publisher-side emission.
- PR 7a fences the **jobs** queue; 7b fences its own **outbox** claim. 6b consumes 7b's ordering
  primitive, platform contract, and activation authority.
