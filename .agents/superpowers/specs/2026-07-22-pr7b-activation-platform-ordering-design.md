# PR 7b-activation — Platform-authoritative decision ordering (item 8, part 2) — design (rev 6)

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
- **`outbox_manual_release`** (re-review `6a408a3` F2) — the tool-owned half of the release saga:
  `(case_id, release_id)` PK; `requested_manual_event_id` NOT NULL; `request_event_id` FK to the
  signed `manual.release_requested` event; `principal` NOT NULL non-blank; `deadline` TIMESTAMPTZ
  NOT NULL; `outcome TEXT NOT NULL CHECK (outcome IN ('pending','completed','expired','cancelled'))`;
  `bound_run_id`/`bound_decision_sequence` NULL until the fresh run exists. Partial unique index
  `WHERE outcome='pending'` on `case_id` — **one open release per case, enforced by the database**,
  not by application check-then-act. Terminal outcomes are immutable (trigger rejects
  `completed|expired|cancelled → anything`), so a replay cannot resurrect a closed release.
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

**`integrity_mismatch`:** the pre-HTTP reader compares **`case_id`, `run_id`, `decision_sequence`,
and `release_id`** across the decision row, outbox columns, and internal JSON. `release_id` is in the
tuple because a release callback's authority comes from that binding (§Manual-release state machine):
ordinary callbacks must have it NULL on every surface, release callbacks non-NULL and equal on every
surface, and any disagreement is a mismatch rather than a silent send. A mismatch → immediate **non-retryable**
fenced terminal (7b-core's fencing): `status='dead'`, `failure_class='integrity_mismatch'`,
`resolved_at`, **zero HTTP**, `published_at` NULL, audit with only identifiers + expected-vs-observed
hashes, metric/alert. **Never `superseded`.** The generic UI requeue (`ui/routes.py:451-476`) returns
**409** for this class until a dedicated repair reconstructs/verifies the tuple.

### 3. Platform authority — candidate manifest, reconciliation, signed bootstrap (F2/F3-heritage)

**Receiver contract:** `POST /kyc/decision` (`case_id=c`,`decision_sequence=s`): one txn — `s>h(c)`
**and** the case's effective source is not manual ⇒ apply+advance; `s≤h(c)` no-op+success; once
initialized, a **missing** sequence is a sticky no-op. The manual clause is **not** an optimization —
see the runtime manual-authority rule below; `s>h(c)` alone is **not** sufficient authority to apply.

**Runtime manual authority (rev 3, 7b-core rev-5 re-review `0ca264b` P1) — supersedes the
bootstrap-only floor as the governing rule.** The rev-2 "manual-current high-water floor" (below) is
a *bootstrap-time* seeding rule: it protects only those cases whose effective source was already
`manual:<event>` **at bootstrap**, and it never runs again. The identical reversion therefore remains
reachable **after `phase='active'`**, because manual approval is enforced inline by the platform and
allocates nothing locally: `_handle_manual_approve` (`src/kyc_tool/events/ingest.py:242-267`) writes a
`DecisionRow` with `run_id=NULL`, `manual=True`, **no `decision_sequence`**, and enqueues **no**
callback, so it cannot advance `h(c)`. Trigger: `h(c)=5`; automatic callback seq 6 already pending;
the platform manually approves; callback 6 arrives; `6>5` ⇒ under the bare rule it **replaces the
manual-current source**. The rev-2 acceptance claim is thus false outside the activation window.

The governing rule is therefore about **authority**, not about seeding an integer: **while a case's
effective source is `manual:<event>`, a sequenced automatic callback is acknowledged (2xx), recorded
for dedupe, and allowed to advance `h(c)` — but it MUST NOT become the effective source.** Only the
explicit, authenticated, platform-owned **manual-release state machine specified below** returns
automatic callbacks to effectiveness. Consequences: the tool still terminalizes such a callback locally as
`delivered` (receiver no-op success — no dead row, no retry storm); `h(c)` still moves so later
callbacks dedupe correctly; and the platform's current-source field, not the sequence integer, is
what the convergence query (§6) and the recovery matrix (§4) read. **This is deliberately the
manual-wins direction.** The inverse — a later automatic decision overriding a manual approval — is a
product decision that must be made explicitly and is **never** to be inferred from `s>h(c)`.
**Acceptance:** after `phase='active'`, a pending automatic callback (sequence higher than the manual
approval's arrival point) delivered **after** a manual approval leaves the manual source effective;
likewise for a *future* sequence created after the manual approval. A **mutation** restoring the bare
`s>h(c)` receiver must fail both. The bootstrap floor below is **retained** — it remains necessary for
cases already manual when the window opens — but it is no longer the whole control.

### Manual-release state machine (rev 5, 7b-core re-review `6a408a3` P1/F2)

Rev 3 said release was "carried in the signed response envelope below". **That was a dangling
reference and is withdrawn**: that envelope is the one-time bootstrap response, accepted only while
`phase='bootstrap_in_progress'`. There was no post-activation endpoint, actor authority, payload,
idempotency rule, persisted state, CAS, timeout, or recovery rule — so manual-wins depended on a
transition that did not exist, and simply clearing the source could not have promoted anything
(the suppressed callbacks are already `s ≤ h(c)`, so replaying them is a no-op).

**Product decision (made explicitly, not inferred): release EXISTS, and suppressed callbacks are
DISCARDED, never promoted.** An operator must be able to hand a manually-approved case back to
automatic scoring. But every callback suppressed while manual was effective was computed against
pre-manual state; promoting one would silently resurrect the decision the operator overrode. Release
therefore completes **only** on a *fresh* recalculation bound to that release — never on history.

**It is a two-system saga, and saying "persisted on the case" was the rev-4 mistake.** There is no
shared transaction between the platform and the tool, so ownership must be split explicitly:

| Owner | Owns |
|---|---|
| **Platform** | effective source, current `manual_event_id`, pending release id + deadline + operator, `h(c)`, and the **final atomic source swap** |
| **Tool (014)** | a durable `outbox_manual_release` record keyed `(case_id, release_id)`: the signed request/event id, the requested `manual_event_id`, the authorized principal, the deadline, an immutable outcome `pending|completed|expired|cancelled`, and the bound `run_id`/`decision_sequence` |

Neither side may infer the other's state. The tool's record is what makes the saga recoverable after
a lost response; the platform's swap is what makes it authoritative.

**Entry — a documented local-extension event.** Release arrives as `manual.release_requested` on the
ordinary signed event path, so the release row, the fresh run, and its queue job all commit in **one
tool transaction** (this repo's rule is that events start work; inventing a side-channel endpoint
would be a deviation, and is recorded as one in `AUDIT_FINDINGS.md` if it is ever taken instead).
Admission requires **all** of: HMAC-v2 over that exact path; `Idempotency-Key == release_id`;
`actor.type == "system"`; and an actor id equal to a configured, non-blank platform principal.
User, reviewer, and other-system actors are rejected — a valid signature is not authority.

**`release_id` is bound end-to-end**: release record → run → decision → outbox row → callback JSON,
and the pre-HTTP integrity tuple (§2) is extended to include it. Ordinary callbacks carry every
release field NULL; release callbacks carry them all non-NULL and equal. A mismatch anywhere is an
`integrity_mismatch` terminal, not a silent send.

**States** (platform-side effective source; the tool mirrors outcome only):

| State | Effective source | Meaning |
|---|---|---|
| `manual` | manual | a manual approval is in force |
| `manual_release_pending` | **still manual** | a release is open; manual has NOT been given up |
| `automatic` | automatic | release completed, or the case was never manual |

**Transitions — all fail-closed; manual stays effective until the final atomic swap:**

1. **Open.** CAS on `manual_event_id`: the transition applies only if the case's currently-effective
   manual event equals the supplied one. A stale id — the case was re-approved since the operator
   read it — is rejected with no state change.
2. `manual` → `manual_release_pending`, recording `release_id`, principal, and a DB-time
   `release_deadline`. **Manual remains effective.**
3. **Completion re-CASes everything, not just the release id.** The rev-4 machine checked only
   `release_id` and `s > h(c)`, which let this sequence through: M1 is effective; R1 opens against
   M1; a reviewer records **M2**; R1's bound callback arrives and replaces M2 — an approval nobody
   ever released. The completion transaction must therefore re-assert **all** of:
   pending release id matches; the release's requested `manual_event_id` matches; **the case's
   current manual event is still that same event**; the deadline is unexpired **by database time**;
   and `s > h(c)`. Any one failing leaves manual effective and the release un-completed.
4. **A new manual approval takes the same case lock and CANCELS the pending release** (outcome
   `cancelled`). That is the other half of (3): the race is resolved by whichever transaction takes
   the lock first, and both orders end with the newer manual approval effective.
5. **Expiry is driven by stored time, not by traffic.** A pending release past its deadline is
   expired by a concrete DB-time reaper (or a lock-protected lazy transition on next touch,
   whichever the implementation picks — but one of them must exist and be named). A case must not
   sit in `manual_release_pending` forever because no further callback ever arrives.
6. **Idempotency + concurrency.** `release_id` is the key: replaying it returns the original
   outcome and changes nothing, including after `completed` or `expired`. A *different* `release_id`
   while one is pending is rejected — one release at a time per case. The same `release_id` used on
   a different case is rejected (the key is the pair).
7. **Recovery.** All release state is persisted on both sides, so a lost response after the platform
   commits `pending`, or a tool crash between the request and the run, both resume: the tool's record
   is authoritative for "was this release ever admitted", and the deadline bounds it either way.

**Required proof (each an executable test, not prose):** wrong signature; valid signature but
non-platform actor (user / reviewer / other system); `Idempotency-Key != release_id`; replayed
`release_id` after `completed` and after `expired`; the same `release_id` on another case; stale
`manual_event_id` at open; **M1 → R1 → M2 → R1-callback in BOTH lock orders, each leaving M2
effective**; response loss after the platform's pending commit; tool crash between request and run;
restart with no further traffic followed by deadline expiry; a late pre-release callback (seq 7)
during `manual_release_pending` that must not complete the release; the fresh bound callback (seq 8)
that must; field-by-field `release_id` tampering across record/run/decision/outbox/JSON; and `h(c)`
monotonic across every path. **Mutations that must fail:** dropping the current-manual-event re-CAS
at completion (this is finding F2 itself), dropping the open-time CAS, dropping the deadline, and
dropping the `release_id` binding so any high-sequence callback completes the release.

**Updated together with this machine**, because a release-aware state only one of them knows about
is the defect this finding was: the platform contract (§Platform integration), the convergence query
(§6), the recovery matrix (§4), `.agents/ROADMAP.md`, `docs/PLATFORM_INTEGRATION.md`, and
`AUDIT_FINDINGS.md`.

**Candidate manifest (tool-derivable):** the tool exports every immutable callback decision
`(case_id, run_id, decision_sequence, callback_wire_sha256, local_status)`. **Its universe and its
source are now defined, not "agreed" (rev 3, `0ca264b` P1/F4):** the manifest is built from 7b-core's
**durable ordering authority — the `outbox` row itself**, which 013 makes non-prunable (retention's
outbox prune is narrowed to `kind='poc_email'`, so a delivered `decision_callback` row keeps its
body indefinitely). `local_status` is `outbox.status`; neither is re-derived from the immutable
`decisions` row, which cannot carry them. The universe is therefore **every** `decision_callback`
row, with no exclusion window — the pre-rev-3 formulation could not survive a restored row being
re-pruned, which would let bootstrap block or silently omit platform history. Exact two-sided
coverage still applies and still fails closed. It
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

**Open manual releases across incidents (rev 4, F2).** A release is **per-case** state and is legal
only in `phase='active'`; the phase CLIs neither read nor clear it. An open
`manual_release_pending` resolves **only** by its own persisted deadline or its bound fresh callback
— never by operator memory and never as a side effect of a phase operation — so an incident during
a release leaves manual effective and the case self-heals at expiry. No recovery path may clear
release state to "unstick" a case: that would discard a manual approval without the CAS that exists
to prevent exactly that.

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

**Effective source gates convergence (rev 4, F2).** The predicate above answers "is the highest
automatic decision in order and terminal-clean" — it does **not** answer "is an automatic decision in
force". While a case's effective source is `manual` or `manual_release_pending`, its suppressed
callbacks are terminal-clean and `h(c)` has advanced, so a sequence-only reading would report the
case **converged on a decision that is not in effect** — exactly the confusion this finding is about.
The shared query therefore returns a **third state**, not a boolean: `converged_automatic`,
`blocked`, or **`not_applicable_manual`** (carrying the effective source and, when pending, the open
`release_id` and deadline). 6b must handle `not_applicable_manual` explicitly — a manually-approved
case is outside automatic convergence, never silently counted as converged and never counted as a
blocker. When a release completes, the case re-enters the ordinary predicate against the **fresh**
bound sequence; the discarded pre-release callbacks never satisfy it.

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
- **Runtime manual authority, POST-activation (rev 3):** with `phase='active'` — (a) create a pending
  automatic callback at seq 6 with `h(c)=5`, apply a manual approval, then deliver 6: the **manual
  source remains effective**, 6 is acknowledged and terminalizes locally as `delivered`, and `h(c)`
  advances; (b) the same with a **future** sequence 7 created *after* the manual approval — manual
  still effective; (c) an explicit authenticated platform release/override transition returns
  automatic callbacks to effectiveness. **Mutation:** the bare `s>h(c)` receiver (no manual clause)
  must **fail** (a) and (b). The bootstrap-time pending/dead case above is retained, not replaced.
- **Manifest durability (rev 3, `0ca264b` P1/F4):** a `decision_callback` row older than the retention
  period, with its **original** `delivered_at`, survives a real retention run intact; its manifest
  entry `(case_id, run_id, decision_sequence, callback_wire_sha256, local_status)` is still exactly
  derivable, and two-sided coverage holds. **Mutation:** widening retention's `DELETE` back over
  `decision_callback` rows must make bootstrap fail closed rather than silently omit history.
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

## Revision note — rev 3 (2026-07-24)

Folds the one activation-owned finding from Codex's 7b-core rev-5 complete-unit re-review
(`0ca264b`), plus the activation-side consequence of the core spec's rev-10 durable-authority
contract. Rev 2's controls are retained, not replaced.

- **P1 — The manual-current floor protected only the activation window (§3).** The rev-2 floor seeds
  `h(c)` at **bootstrap** for cases already manual at that instant. Manual approval is enforced inline
  by the platform and allocates nothing locally — `_handle_manual_approve`
  (`src/kyc_tool/events/ingest.py:242-267`) writes `run_id=NULL`, `manual=True`, no
  `decision_sequence`, and no outbox row — so it never advances `h(c)`. After `phase='active'` the
  bare `s>h(c)` receiver therefore still lets a stale pending automatic callback replace a manual
  approval, and the floor never runs again. **Resolution:** a **runtime authority rule** replaces
  `s>h(c)` as the governing condition — while a case's effective source is `manual:<event>`, sequenced
  automatic callbacks are acknowledged, terminalized locally as `delivered`, and allowed to advance
  `h(c)`, but **cannot become the effective source** until an explicit authenticated platform-owned
  release/override transition. The bootstrap floor is retained for cases already manual when the
  window opens. This is the **manual-wins** direction; the inverse is a product decision that must be
  made explicitly and is never inferred from `s>h(c)`. Receiver contract, platform integration docs,
  convergence query, and recovery semantics are updated together.
- **Manifest universe is now defined (§3), following core rev 10.** The candidate manifest is built
  from 7b-core's durable ordering authority — the `outbox` row, which 013 makes non-prunable
  (retention's outbox prune narrows to `kind='poc_email'`) and which therefore keeps both its body
  and its `status`. The pre-rev-3 "agreed universe" was undefined and could not survive a
  restored row being re-pruned; the universe is now every `decision_callback` row, with two-sided
  coverage still failing closed.

**`callback_wire_sha256` is the digest of the bytes actually sent — one versioned codec, two callers
(rev 4, `0ca264b` P1/F1).** The pre-rev-4 manifest hashed PostgreSQL `payload_json::text` while the
sender transmits `json.dumps(payload).encode()` (`publisher.py:114`). Those encoders disagree —
Python's default emits `\u00e9` where JSONB text emits UTF-8 `é`, and `checks[].source` is reachable
as `reviewer:<reviewer_id>` (`validators/website.py:13-20`) — so an honest platform hashing the bytes
it accepted would have disagreed with us, while a platform echoing our SQL value would have made the
witness circular and proved nothing about what it received. Therefore:

- 014 defines **one codec with an explicit wire schema**,
  `encode_decision_callback(payload, wire_version) -> bytes`, where `wire_version` is `"legacy"` or
  `"sequenced"`, and **both** `_deliver_decision_callback` and the manifest exporter use its exact
  output. Neither may encode independently. Changing either encoding is a versioned change, because
  it changes the signed bytes.
- **`wire_version` is not decoration — without it the historical manifest is simply wrong
  (re-review `6a408a3` P1/F1).** 014 backfills `decision_sequence` into every surviving legacy
  `decision_callback.payload_json` (§2), but pre-activation HTTP sent a body **without** that field.
  Hash the backfilled payload and the exporter produces a digest the platform never saw. The trigger
  is reachable, not theoretical: a legacy callback gets a 2xx, the local delivered stamp faults
  (send-before-stamp, the residual 7b-core documents), the row stays `pending`, and after 014 the
  platform ledger holds the legacy-body digest while the exporter hashes the sequenced body —
  bootstrap fails on honest history with neither side corrupt.
  Therefore: **the bootstrap manifest labels and hashes every pre-`active` callback as `legacy`**,
  even after the internal backfill, and the `legacy` encoding **deterministically omits
  `decision_sequence`** rather than relying on it being absent. `wire_version` travels in the
  manifest entry and is persisted in the **signed, immutable bootstrap response artifact**, so the
  accepted version is evidence rather than an assumption on either side.
- **No sequenced HTTP emission may occur before `phase='active'`** — that is what makes the blanket
  `legacy` label sound. §2's phase matrix already enforces it (`legacy` strips the field;
  `bootstrap_in_progress`/`bootstrapped` refuse to claim or send; only `active`+flag emits), and it
  is now stated as a contract, with a test that a pre-`active` send is byte-identical to 7b-core.
  **If any future phase can emit both encodings, that blanket label becomes unsound** and the design
  must add an immutable pre-HTTP delivery-attempt record
  `(outbox_id, attempt_id, wire_version, request_sha256)`, reconciling the platform's accepted digest
  against those attempts — local terminal status is not sufficient evidence of what was sent, because
  send-before-stamp is reachable.
- `callback_wire_sha256` is the SHA-256 of those bytes.
- The **platform ledger must retain the SHA-256 of the raw request bytes it actually accepted**, and
  reconciliation compares that against `callback_wire_sha256`. Echoing the tool's supplied value is
  explicitly not an acceptable implementation of the platform side.
- 7b-core's `stored_payload_jsonb_digest` is a **different value for a different job** — backup→restore
  semantic equality, both sides in Postgres. It is never exported in the manifest and never described
  as a body digest.

**Required proof:** a legacy-delivered callback still matches its legacy digest **after** 014's
backfill has added `decision_sequence` to its stored payload; a legacy HTTP-success whose local
stamp faulted still reconciles; the first post-`active` delivery uses only sequenced bytes; and a
pre-`active` send is byte-identical to 7b-core. **Mutation:** make the exporter always use the
sequenced encoding — the backfilled-legacy case must fail. Then: capture the real
`httpx.Request.content` and assert its SHA-256 equals the manifest's `callback_wire_sha256` for
nested objects, alternate key-insertion order, and non-ASCII
reviewer/source text; assert the SQL semantic digest **differs** for at least the non-ASCII case and
is used only on the restore path. **Mutation:** switch either the sender or the exporter to an
independent encoder — the equality test must fail.

## Revision note — rev 4 (2026-07-25)

Folds the two 014-owned findings from Codex's 7b-core rev-6 re-review (`99df3d2`). Rev 3's accepted
controls are unchanged; core (`013`) gains nothing from this revision, per that review's explicit
instruction not to re-merge activation into core.

- **P1/F1 — the manifest digest did not hash what is sent.** `callback_body_sha256` was defined over
  PostgreSQL `payload_json::text` while `_deliver_decision_callback` transmits
  `json.dumps(payload).encode()` — `ensure_ascii=True` versus raw UTF-8. `checks[].source` is
  reachable as `reviewer:<reviewer_id>` (`validators/website.py:13-20`), so a non-ASCII reviewer id
  makes the two disagree; a platform hashing the bytes it accepted would dispute the manifest, and
  one echoing our SQL digest would prove nothing. **Resolution:** two separately-named values that
  cannot be conflated — `stored_payload_jsonb_digest` (restore-time semantic equality, both sides
  Postgres) and `callback_wire_sha256`, one versioned `encode_decision_callback(payload) -> bytes`
  codec used by **both** the sender and the manifest exporter, with the platform ledger required to
  retain the SHA-256 of the raw request bytes it actually accepted.
- **P1/F2 — manual-wins depended on a release transition that did not exist.** Rev 3 pointed at "the
  signed response envelope below", which is the one-time *bootstrap* response, accepted only in
  `bootstrap_in_progress`. **Resolution (user product decision, 2026-07-25): release EXISTS, as a
  fail-closed state machine, and suppressed callbacks are DISCARDED rather than promoted** — they
  were computed against pre-manual state, so promoting one would resurrect the decision the operator
  overrode. Manual remains effective through `manual_release_pending`; only a *fresh* recalculation
  bound to `release_id`, with `s > h(c)`, atomically completes the release. CAS on
  `manual_event_id`, `release_id` as idempotency key, one release per case at a time, persisted
  deadline so a case can never wedge, and full restart recovery. The convergence query now returns
  `not_applicable_manual` as a third state rather than reporting a suppressed decision as converged,
  and the recovery matrix forbids clearing release state to "unstick" a case.

## Revision note — rev 5 (2026-07-26)

Folds the 014-owned finding F2 from Codex's post-rev-6 re-review (`6a408a3`). Rev 4's release
machine was directionally right and mechanically unsafe; this replaces the mechanism, not the
product decision (release exists; suppressed callbacks are discarded — unchanged, human-decided).

- **The completion CAS could discard a newer manual approval.** Rev 4 checked only `release_id` and
  `s > h(c)` at completion, so: M1 effective → R1 opens against M1 → a reviewer records **M2** →
  R1's bound callback arrives and replaces M2, an approval nobody released. Completion now re-CASes
  **all** of: pending release id, the release's requested `manual_event_id`, **the case's current
  manual event still being that same event**, an unexpired **DB-time** deadline, and `s > h(c)`. A
  new manual approval takes the same case lock and *cancels* the pending release, so both lock
  orders end with M2 effective. The required proof runs that interleaving in both orders.
- **"Persisted on the case" conflated two systems.** There is no shared transaction between platform
  and tool, so ownership is now split explicitly: the platform owns effective source, current
  `manual_event_id`, pending release/deadline/operator, `h(c)`, and the final atomic swap; 014 owns a
  durable `outbox_manual_release` record keyed `(case_id, release_id)` with an immutable
  `pending|completed|expired|cancelled` outcome. A partial unique index enforces **one open release
  per case in the database** rather than by check-then-act, and terminal outcomes are trigger-immutable
  so a replay cannot resurrect a closed release.
- **Entry is a documented local-extension event** (`manual.release_requested`) so the release row,
  the fresh run and its queue job commit in one tool transaction — preserving this repo's "events
  start work" rule instead of inventing a side-channel endpoint. Admission requires HMAC-v2 on that
  exact path, `Idempotency-Key == release_id`, `actor.type == "system"`, and a configured non-blank
  platform principal; a valid signature alone is not authority.
- **`release_id` is bound end-to-end** (record → run → decision → outbox → callback JSON) and joins
  the pre-HTTP integrity tuple, so a tampered or absent binding is an `integrity_mismatch` terminal
  rather than a silent send. Ordinary callbacks carry the release fields NULL on every surface.
- **Expiry is driven by stored DB time**, via a named reaper or lock-protected lazy transition — a
  stored deadline does not expire itself, and a case must not wedge because no further traffic arrives.

## Revision note — rev 6 (2026-07-26)

Folds the remaining 014-owned finding F1 from Codex's post-rev-6 re-review (`6a408a3`).

- **The historical manifest could not reproduce the bytes the platform accepted.** Rev 5 defined one
  codec, `encode_decision_callback(payload)`, and pointed both the sender and the exporter at it —
  which fixed the *encoder* disagreement but not the *payload* disagreement. 014 backfills
  `decision_sequence` into every surviving legacy `decision_callback.payload_json`, while
  pre-activation HTTP sent a body without that field, so the exporter hashed a body that was never
  sent. The trigger is reachable: a legacy callback takes a 2xx, its local delivered stamp faults
  (the send-before-stamp residual 7b-core documents), the row stays `pending`, and after 014 the
  platform ledger holds the legacy digest while the exporter produces the sequenced one — bootstrap
  fails on honest history with neither side corrupt.
  **Resolution:** the codec takes an explicit wire schema,
  `encode_decision_callback(payload, wire_version)` with `"legacy"`/`"sequenced"`; the `legacy`
  encoding deterministically **omits** `decision_sequence` rather than assuming it is absent; the
  bootstrap manifest labels and hashes every pre-`active` callback as `legacy` even after the
  backfill; and `wire_version` is persisted in the signed immutable bootstrap response, so the
  accepted encoding is evidence rather than an assumption. "No sequenced emission before
  `phase='active'`" is now a stated contract with a test, since that is what makes the blanket
  `legacy` label sound — and the spec names the delivery-attempt record that would become mandatory
  if any future phase could emit both encodings.
- **The core spec's stale cross-reference is removed.** It still described
  `stored_payload_jsonb_digest` as the expression "014's manifest" uses, contradicting its own later
  correction that the SQL digest is restore-only and never a received-body witness.
