## 9. PR 5b cutover — brief full maintenance window

PR 5b adds the reviewer-actor trust floor that closes the "review completed /
approved by anyone holding the shared secret" forgery: it requires the signed
envelope's `actor` to identify the reviewer (`docs/PLATFORM_INTEGRATION.md`
§3), not just the payload. **This is not a rolling deploy.** During any
old/new overlap, an old replica still honors the exact forgery this release
closes — an old API applies `reviewer.manual_approve` inline with no actor
floor, and an old pipeline worker (which claims a job purely by kind, with no
event-type filter) can still close a queued `system`-actor website completion
under the old actorless semantics. There is no way to keep an old replica
serving *any* traffic while guaranteeing it never touches a sensitive event,
so this release ships as a **brief full maintenance window** — the same
non-hot **stop → deploy → start** pattern PR 5a used (§2), extended to workers
as well as the API, which removes old/new overlap entirely. No migration
ships with this change.

The window is a real interruption, not a seamless roll: `POST
/v1/cases/{case_id}/events` is unavailable for its duration, for every event
type, and the pipeline is stopped. The "no loss" guarantee for that
interruption is a **platform prerequisite**, not something the current API
contract provides on its own — today the contract only directs retry on a
*network failure* (`docs/PLATFORM_INTEGRATION.md` §3), but a load balancer
with every API target down instead returns 502/503/504, and a delayed retry
that reuses the original signature can blow the 300-second HMAC skew. Before
scheduling the window, confirm with the platform team that they will
pause/buffer **all** event submission for its duration and drain it
afterward — re-signing each retried body with a **fresh timestamp/signature**
against the **same idempotency key** — or, if they prefer to keep sending,
that they treat 502/503/504 the same as a transport failure: retryable with
the same body/key and a fresh signature.

Steps:

0. **Before the window:** build and publish the reviewed image; record its
   **digest**. Every step below that runs code — the recovery one-shot, the
   new API, the new workers — is pinned to that one digest. The recovery
   module (`kyc_tool.ops.requeue_interrupted_jobs`) exists only in the new
   image, so running it on the still-current old task definition fails with
   `No module named …`.
1. **Pause all platform event submission** (every event type, not only the
   two sensitive ones) and block the ops composer: the platform buffers
   outbound events, and the composer route (a `POST` to `/ui/api/send-event`) is
   edge-blocked — or old replicas are flipped to `KYC_UI_ENABLED=false` — so
   an operator on a still-live old replica can't post an inline forged
   approval. The whole window is a maintenance pause; a partial pause cannot
   guarantee no-loss.
2. **Stop all old processes together** — the API pool and the pipeline-worker
   pool, as one coordinated action, **no graceful drain**, without awaiting
   either pool before signaling the other. Confirm both pools are at **zero**
   before continuing. A sequenced stop leaves the not-yet-stopped pool live
   and able to commit a forgery in the gap; the edge block cannot revoke a
   request already inside an old API's threadpool, so old APIs must be
   *stopped*, not drained. Hard termination is safe here: every transition
   (and every ingest) commits in one transaction, so interrupted work simply
   rolls back.
3. **Recover interrupted jobs.** With both pools confirmed at zero, run
   ``python -m kyc_tool.ops.requeue_interrupted_jobs`` as a one-shot task
   **pinned to the §0 digest**, before any new worker starts. With every
   worker stopped and none yet restarted, every `status='running'` job row is
   by definition interrupted — regardless of lease expiry, and with no need
   for a worker-liveness registry. The command requeues that whole set
   transactionally **without** consuming the forced-stop attempt (this is an
   operator-initiated interruption, not a handler failure — consuming the
   attempt could let the passive reaper dead-letter an expired final-attempt
   job before it ever reaches the new actor guard), and it asserts zero
   `running` rows remain on completion. It must not run while any worker is
   live — confirming both pools at zero (step 2) is its precondition.
4. **Deploy the new API and worker services, both pinned to the §0 digest**
   (attest it). Bring the **API** up first and hold **workers at zero** until
   step 5 passes — workers serve no HTTP, so the API probes below cannot
   vouch for a stale or mismatched worker image, and starting workers early
   would let one honor a recovered pre-upgrade completion.
5. **Direct-probe each new API replica** on a trusted path that bypasses the
   edge rule (internal target-group address or port-forward) — probing
   through the edge would let the load balancer's own 403 falsely certify a
   broken app. Use only side-effect-free probes (each is rejected at the
   ingest floor and rolls back, writing no rows) and assert the response
   **body**, not just the status code:
   - a signed `system`-actor `website.review_completed` against a valid open
     task → the app's **422** (a 404/409 would mask a broken actor floor);
   - a mismatched-actor `reviewer.manual_approve` → the app's **422**;
   - the composer → the app's **403** for both sensitive event types.

   Do not probe the decide-txn guard's live behavior in production this way —
   a real pipeline run there writes a decision and enqueues a callback
   unconditionally, and the outbox publisher (a separate process, not stopped
   in step 2) would deliver it to the platform. That behavior is proven
   **before the window, in staging**, against the exact §0 digest; attest the
   same digest here.
6. **Start the new workers** (they now claim the recovered queue under the
   new decide-txn actor guard), then **resume** — unpause platform event
   submission and unblock the composer route.

### Rollback

Rollback mirrors the same window and pins the recovery one-shot to the **last
image that still contains it**: pause all submission, stop all new processes
together (same coordinated hard stop; same recovery command, run against a
digest that has the module), redeploy the prior image for API **and**
workers, verify, start workers, resume — accepting that the prior image
restores pre-PR-5b behavior.

**Rollback verification is non-mutating only:** `GET` probes of `/readyz`
and `/healthz`, and prior-image digest attestation. Do **not** run the step-5
sensitive-mutation probes against the prior image — that image is the current
vulnerable code with no actor floor, so a mismatched-actor `manual_approve`
probe would actually `approve` the case inline, and a `system`-actor
completion probe would queue a run the restored old worker can honor: the
probe would *perform* the forgery it is meant to detect, not find it.
Exercise that behavior only in staging or an isolated DB. Keep all submission
and the composer blocked until the safe, non-mutating checks pass.
