## 10. PR 6 cutover — bundle-pinning activation

PR 6 pins the policy bundle (and records the engine build) a run is actually
scored and decided under, instead of trusting whatever the worker process
happened to have loaded. It ships in two parts: a **rolling** part (safe to
deploy like any other release) and a **drained** part (the flag flip, not
safe to roll).

**Rolling — migration + provenance, flag stays off.** Migration 011
(`policy_bundles`, `bundle_pinning_epoch`, and the new nullable provenance
columns) is additive and hot-compatible — deploy it through the normal §4
flow. It is hot-compatible precisely because its three provenance-column
CHECKs land `NOT VALID` (a brief, metadata-only lock, no table scan) and are
validated by the follow-on migration 012 via `VALIDATE CONSTRAINT` under a
non-blocking lock, so the rolling `alembic` upgrade-to-head (the §4 one-shot) does not stall
ingest/decide writers on large `checks`/`decisions` audit tables. On the PR6
image, the API and pipeline worker seed and read back the
on-disk policy bundle at startup (failing closed on a corrupt persisted row)
and every automatic/manual decision starts recording bundle **and** engine
provenance immediately — `Settings.enforce_bundle_pinning`
(`KYC_ENFORCE_BUNDLE_PINNING`) stays `false`, so scoring itself is
byte-identical to pre-PR6 (a strict no-op; see ADR-005). A `GET` on `/readyz`
unconditionally confirms the process's loaded bundle is durably resolvable
from the store — watch it like any other readiness check during this
rollout.

**Seed the bundle.** Before relying on the store for anything beyond a
process's own startup seeding (in particular, before the preflight below),
confirm the release's bundle is in `policy_bundles`:

```operator
python -m kyc_tool.ops.seed_policy_bundle --expect-hash <sha256>
```

`--expect-hash` is the hash an operator names from a reviewed source (the
release notes / deploy manifest) — the command computes the hash of the
bundle at `KYC_POLICY_DIR`, compares it to `--expect-hash`, and **only on a
match** stores it; a mismatch raises and writes nothing, so a wrong policy
directory can never land a row silently.

**Drained cutover — flip the flag.** This is not a rolling deploy: every
worker that claims a `run_transition` job while `enforce_bundle_pinning` is
inconsistent across the pool risks resolving a bundle differently from its
peers. Flip it with the pool fully drained, the same shape PR 5a/5b used:

1. **Preflight.** ``python -m kyc_tool.ops.verify_pinnable_backlog`` — checks
   that every queued/running/dead `run_transition` job's run has a
   creation-pin bundle that actually loads from the store. **A nonzero exit
   blocks the cutover** — seed the missing bundle(s) (§ above, or historical
   recovery via the same command with the older policy directory) and re-run
   until it exits 0.
2. **Disable autoscaling/restarts; confirm zero old workers.** At the
   orchestrator (not by row count — a job-table count doesn't prove process
   quiescence), confirm every pipeline-worker replica currently running
   predates this cutover is gone.
3. **Recover interrupted jobs.** With workers confirmed at zero, run
   ``python -m kyc_tool.ops.requeue_interrupted_jobs`` once — every
   `status='running'` job at this point is by definition interrupted; it
   requeues the whole set without consuming the forced-stop attempt and
   asserts zero `running` rows remain. Its precondition is "all workers
   confirmed stopped" (step 2) — do not run it while any worker is live.
4. **Start flag-on workers.** Set `KYC_ENFORCE_BUNDLE_PINNING=true` and start
   the pipeline-worker pool. Confirm the startup **attestation** log line on
   every replica — a structured `bundle_pinning_ready` event carrying
   `flag=true`, `bundle_hash`, and `engine_build_id` — emitted only after the
   worker's own seed-and-verify passes, so a replica that never logs it never
   started claiming jobs.
5. **Resume.** Unpause whatever was paused for the drain (the API itself
   never stopped — only the worker pool is drained here).

**Activate the epoch.** Once the cutover is verified stable, write the
durable activation record:

```operator
python -m kyc_tool.ops.activate_bundle_pinning_epoch \
    --expect-bundle-hash <sha256> --expect-engine eng-1
```

This compares the **locally loaded** policy bundle and this process's
`ENGINE_BUILD_ID` against the `--expect-*` arguments before touching the
database at all (a valid-but-wrong bundle is refused here, not merely by
store-absence later), writes the singleton `bundle_pinning_epoch` row with
database time, and read-back-fails if a concurrent activation already wrote
different values — so a skewed operator clock or a mismatched second
activation can never silently move the boundary. From `activated_at`
onward, `docs/RUNBOOK.md`'s post-epoch alert treats any check/decision
missing its provenance stamp as an anomaly, not an expected state.

**Rollback — flag-only, no data migration.** Normal rollback for PR 6 never
means resuming on a pre-PR6 image (that would silently stop writing
provenance and, after the epoch, mint permanent NULLs — a defect, not a
safe fallback). It means disabling the flag **on the PR6 image**, via the
same drained shape: stop the worker pool → confirm zero running →
`ops.requeue_interrupted_jobs` → start workers with
`KYC_ENFORCE_BUNDLE_PINNING=false` → resume. The creation pin and
provenance writing are preserved throughout; scoring simply returns to the
process-loaded bundle. Migration 011 is **retained** — never downgrade it
once any bundle row, provenance column, or the epoch row is populated (its
downgrade deliberately refuses, the same forward-only-after-use contract
migration 010 established in ADR-003).
