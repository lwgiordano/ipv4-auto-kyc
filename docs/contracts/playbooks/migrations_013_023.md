## 11. PR 7b-core cutover — drained maintenance window (migration 013)

**Step 0 — pre-window diagnostic (BEFORE any outage):**
0.1 Suspend the retention schedule.
0.2 Terminate and wait for every active retention task.
0.3 Capture target-orchestrator zero-running evidence. `TODO(integration)`: the exact
    zero-running listing — the `aws` CLI's `ecs list-tasks` scoped to the cluster and the
    retention family (or the EC2 equivalent) — and its expected zero-task output MUST be
    recorded here as a typed operator command once the production substrate is chosen. A
    pytest does NOT prove this — it is a deployment acceptance. Do not invent a substrate.
0.4 With the schedule still suspended, run the digest-pinned
    ``python -m kyc_tool.ops.verify_pr7b_core_backfill``. The result is valid ONLY while retention stays
    suspended AND the 0.3 attestation holds.
0.5 On failure, ABORT here — before stopping service (no outage begun). Recovery is restore-or-block:
    restore from authoritative backup the EXACT callback row, OR remain on 012 in
    `BLOCKED_NO_AUTHORITATIVE_MAPPING`. Backup availability is an operator prerequisite. Activation (`024`) is
    downstream and cannot repair this. Never fabricate a callback, delete a decision, or fall back to
    `decided_at`. On EVERY abort path, explicitly re-enable OR deliberately keep-frozen retention.
    THE RESTORE PATH IS A SHIPPED CLI, reachable from HERE — a pre-window maintenance stop, not the
    cutover (which 0.4 still gates): FIRST run the prerequisites check (read-only, takes NO
    lock) and confirm it is GREEN — exact schema phase, correct role, `outbox_id_seq`
    ownership, and timeout budgets:

```operator
python -m kyc_tool.ops.verify_pr7b_ops_prerequisites --expect-revision 012
```

    A wrong maintenance credential OR wrong phase is caught HERE, not at `ALTER SEQUENCE`
    inside the stop; then pause submissions,
    hard-stop and attest EVERY writer (API,
    pipeline, outbox, `dev_worker`, retention), then run
    the restore CLI (dry-run first; add `--apply` to perform):

```operator
python -m kyc_tool.ops.restore_pr7b_core_callback --evidence <file.json> \
    --expect-original-id <id> --expect-manifest-digest <sha256>
```

    `--expect-manifest-digest` is MANDATORY and is an INTEGRITY check: the tool recomputes
    the sha256 of the evidence file (``sha256sum <file.json>``) and refuses unless it matches, so a
    tampered or wrong file is rejected before any DB work — the file cannot self-certify by carrying
    its own digest. The tool does NOT verify a cryptographic signature; the digest's authenticity is
    yours to establish out of band, from a trusted/signed backup manifest (machine-verified signing
    is a future option).
    It validates the whole
    contract below, inserts the exact original row, floors the sequence past the restored id
    (`GREATEST(max(id), original_id) + 1`) in the SAME transaction, and fail-closed read-backs both
    the acceptance predicate and the sequence before committing — any mismatch rolls back row and
    sequence together. Then rerun 0.4 (the gate that reopens cutover) and either RESUME service or
    proceed to the window. Pasting the SQL below by hand is NOT a sanctioned path — the earlier
    revision of this section prescribed exactly that and was circular: the diagnostic stayed red
    until the restore, while the sequence repair was documented as reachable only after cutover
    step 2 and knew nothing of the id being restored (re-audit `f495de8` F1).
0.6 RESTORE ACCEPTANCE CONTRACT (the restore in 0.5 is an executable identity requirement, not
    advice — the backfill ranks by `outbox.id`, so a wrong id silently reverses the legacy order):
    (a) BEFORE restoring, record from the backup the authoritative evidence tuple per missing
        callback: `decision_id` plus **every schema-012 `outbox` column** —
        `(id, kind, case_id, run_id, payload_json, status, attempts, next_attempt_at,
        delivered_at, last_error, created_at)` — with `body_digest` computed ON THE BACKUP ROW as
        `encode(sha256(convert_to(payload_json::text,'UTF8')),'hex')`. `md5(...)` is prohibited.
        This procedure runs BEFORE 013, so it must name NO 013-only column: `resolved_at`,
        `ordering_stream`, `decision_sequence` and the claim tuple do not exist yet. Omitting the
        retry/audit columns is what makes "exact" false — a previously retried callback restored
        with a reset `attempts`/`next_attempt_at`/`last_error` is NOT the row that was pruned.
    (b) The restore MUST re-insert the ORIGINAL primary key AND every other recorded column:
        `INSERT INTO outbox (id, kind, case_id, run_id, payload_json, status, attempts,
         next_attempt_at, delivered_at, last_error, created_at) VALUES (<original_outbox_id>, ...)`
         — every value from the evidence tuple, none defaulted. A
        default-id INSERT is prohibited (it allocates a fresh id and re-ranks the restored older
        callback as newer), and substituting `now()` for `delivered_at` is prohibited (it falsifies
        the audit record). If the original id is unavailable, do NOT restore: remain
        `BLOCKED_NO_AUTHORITATIVE_MAPPING` on 012. The evidence tuple is captured into the JSON
        file the restore CLI's `--evidence` input consumes; `--expect-original-id` must repeat
        the id (double entry). The id, body digest and decision linkage are machine-refused on
        mismatch; the lifecycle fields are ATTESTED inputs from the backup — but the MANDATORY
        `--expect-manifest-digest` (sha256 of the whole evidence file, from the signed manifest)
        binds every one of them, so a falsified backup value cannot pass without also breaking the
        signed digest. The signed manifest and the documented capture query are the sanctioned source.
    (c) ACCEPTANCE PREDICATE — POSITIVE and fail-closed. Run per restored callback; it MUST return
        EXACTLY ONE row before proceeding. ZERO rows = still blocked. Do NOT invert it into a
        "select the mismatches, expect zero rows" form: an absent row (or one restored under the
        wrong `run_id`) matches nothing and would read as accepted.
        `SELECT 1 AS accepted FROM outbox o JOIN decisions d ON d.id = :decision_id
         WHERE o.id = :original_outbox_id AND o.kind = :original_kind
           AND o.case_id = :case_id AND o.run_id = :run_id
           AND d.case_id = o.case_id AND d.run_id = o.run_id
           AND encode(sha256(convert_to(o.payload_json::text,'UTF8')),'hex') = :body_digest
           AND o.status = :original_status
           AND o.delivered_at IS NOT DISTINCT FROM :original_delivered_at
           AND o.attempts = :original_attempts
           AND o.next_attempt_at IS NOT DISTINCT FROM :original_next_attempt_at
           AND o.last_error IS NOT DISTINCT FROM :original_last_error
           AND o.created_at IS NOT DISTINCT FROM :original_created_at;`
        Every schema-012 column is compared, so dropping any one of them from the restore fails
        the predicate. `IS NOT DISTINCT FROM` is used for nullables so NULL matches NULL.
    (d) THE SEQUENCE IS THE RESTORE CLI'S JOB — there is NO separate precondition to satisfy
        first (re-audit `8377440` F3: the old text made the restore reachable only after a
        `next_id > original_outbox_id` check that the documented `max=5`/`missing-id=100` case fails,
        which is exactly the case the restore exists for). `restore_pr7b_core_callback` floors the
        sequence to `GREATEST(max(id), original_id) + 1` in the SAME transaction as the row
        insert, under `ACCESS EXCLUSIVE`, with a fail-closed read-back — whether the missing id is
        below OR above the current high-water. It never `setval`s (a read-modify-write on a
        non-transactional object that can rewind under concurrent `nextval`); `ALTER SEQUENCE …
        RESTART WITH` takes a literal and excludes `nextval` for the transaction. Run it (dry-run,
        then `--apply`) as step 0.5 above — the restore and the sequence floor are ONE action, not
        a check-then-repair sequence.
    (e) ``python -m kyc_tool.ops.repair_outbox_sequence`` is the SEPARATE DRAINED action for the
        ONLY case the restore does not cover: a divergent sequence high-water with NO row to
        restore (nothing missing, the counter itself is wrong). Same maintenance-stop
        preconditions and owner privilege; pass `--floor` with the id when an id above max must stay cleared.
        It is never a prerequisite the restore waits on.
    (f) Only then rerun 0.4 (it must be clean — it also proves existence/1:1 of every mapping).

**Cutover (only after 0.4 is green):**
1. Pause submission, edge-block the composer, disable autoscaling/restarts.
2. Hard-stop API, pipeline, outbox, `dev_worker` (queue AND outbox), retention, and every writer;
   attest zero at the orchestrator.
3. Run the shipped ``python -m kyc_tool.ops.requeue_interrupted_jobs``. NO outbox reset here — the
   pre-013 schema has no claim columns; an interrupted old claim simply waits until its already-
   recorded `next_attempt_at`. Preserve every pending row's `next_attempt_at`.
4. Run ``python -m alembic -c alembic.ini upgrade head`` (the chain `013`→`014`→…→`022`→`023`) — the deployment image runs its exact
   equivalent. This repeats the §0 parity preflights under the zero-writer boundary and is the
   authoritative fail-closed check (the pre-window diagnostic is an early detector, not a substitute).
   `017` additionally machine-checks the drain: it refuses with `MIGRATION_017_PREFLIGHT_LIVE_CLAIMS`
   while any live (unexpired) outbox claim exists — leases must expire or be reset first.
5. Start API only, probe `/readyz`, then start + attest the fenced workers. No mutating prod smoke.
6. RESUME (forward completion): re-enable retention, autoscaling/restarts, and submissions, and
   remove the composer edge block. The window is NOT closed until all five paused controls
   (retention, autoscaling, restarts, submissions, composer edge block) are restored or removed.

**Rollback — a two-branch maintenance state machine (as drained as the forward cutover). BOTH branches
end in a full resume — never leave the system stopped or retention frozen:**
R1. Pause submissions, edge-block the composer, disable autoscaling/restarts.
R2. Hard-stop and orchestrator-attest zero API, pipeline, outbox, `dev_worker`, retention, every writer.
R3. While 013 still exists, run ``python -m kyc_tool.ops.reset_interrupted_outbox_claims`` (post-013-only;
    clears complete claim tuples, preserves `next_attempt_at`, atomically read-back-asserts zero) and
    verify zero claim tuples.
R4. **With `018` or anything above it installed there is no schema-downgrade path**: `018` through
    `022` refuse unconditionally — a walk from the head prints
    `MIGRATION_022_DOWNGRADE_REFUSED_FORWARD_ONLY` (`023`'s downgrade is a validation-only
    no-op the walk passes through first; the whole command is ONE transaction, so on refusal
    even that step rolls back and the schema does not move) — because walking below them would restore
    search-path-vulnerable authority functions, so rollback goes straight to R5 (image-only on
    the schema already installed). The walk below is the HISTORICAL path, reachable only on a
    schema that never reached `018`: run ``python -m alembic -c alembic.ini downgrade 012`` (the revision is a
    REQUIRED positional argument — a bare `alembic` downgrade invocation without it exits with a usage error
    mid-outage). That walk is `017 → 016 → 015 → 014 → 013 → 012`, and EACH revision preflights
    under
    `LOCK TABLE ... ACCESS EXCLUSIVE` (child-first from `015` on; `017` first takes the shared
    maintenance/writer advisory fence EXCLUSIVE, so it queues behind live witness writers instead
    of reasoning about their lock order). Sentinels in execution order:
    - `017` refuses — `MIGRATION_017_DOWNGRADE_REFUSED_WITNESS_IN_USE` — when ANY attempt row,
      terminal wire digest, or `attempt_v1` decision callback exists. NEGATIVE evidence counts:
      an attempt-regime row with no attempt is the durable proof nothing was staged.
    - `016` refuses — `MIGRATION_016_DOWNGRADE_REFUSED_WITNESS_IN_USE` — same rule one revision
      down (defense in depth below `017`), including the `attempt_v1` negative-evidence case.
    - `015` refuses — `MIGRATION_015_DOWNGRADE_REFUSED_WITNESS_IN_USE` — on any attempt row or
      terminal digest (child-first lock order; cannot deadlock a live writer).
    - `014` refuses — `MIGRATION_014_DOWNGRADE_REFUSED_WITNESS_IN_USE` — same witness rule.
    - `013` refuses on a `superseded` row, a surviving terminal digest
      (`MIGRATION_013_DOWNGRADE_REFUSED_WITNESS_IN_USE`), or the attempt table under a bare `013`
      stamp (`MIGRATION_013_DOWNGRADE_REFUSED_AMENDED_HISTORY`).
R5. ROLLBACK OUTCOME A — downgrade REFUSED (any sentinel above): the DB stays on the
    witness-authority schema, so KEEP or redeploy the reviewed **`023`-COMPATIBLE image** digest —
    an older publisher lacks the receipt/terminal contract and MUST NOT run against preserved
    evidence; PROHIBIT the pre-7b image outright. Rollback after first witness use is a
    FLAG/IMAGE rollback on the compatible schema, never a schema downgrade. A pre-7b image is
    permitted ONLY after the entire walk reaches `012` (outcome B). Verify `/readyz`, start + attest its fenced workers, then
    re-enable retention, autoscaling/restarts, and submissions and remove the composer edge block —
    OR remain in a DELIBERATELY DECLARED maintenance incident while the forward fix is applied. Do
    not end stopped.
R6. ROLLBACK OUTCOME B — downgrade SUCCEEDED: deploy the recorded prior-image digest; start API, probe
    `/readyz`, then start + attest its workers; attest image digest + running processes; then re-enable
    retention, autoscaling/restarts, and submissions and remove the composer edge block. Redeploying
    the pre-7b image BEFORE 013 is applied is also safe.
