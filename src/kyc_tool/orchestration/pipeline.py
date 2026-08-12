"""Run orchestrator — walks the spec state machine, one transaction per
persisted transition, network I/O strictly outside transactions.

Persisted states: QUEUED → RESOLVE_INPUTS → BROKER_GATE → RUN_ADAPTERS →
VALIDATE → PUBLISH_DECISION → COMPLETE (broker short-circuit: BROKER_GATE →
DECIDE). WRITE_CHECKS/SCORE/DECIDE are logical stages of the single fused
"decide transaction" — the spec REQUIRES checks+score+decision to commit
atomically, so those states can never be observed as committed rows; each is
still recorded as an audit entry.

Crash safety: any crash between transactions is recovered by the job lease —
the retried job re-reads run.state and resumes; adapter fetches are skipped
via the (run, adapter, input_hash) unique key.
"""

import time
import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from kyc_tool.adapters import retry
from kyc_tool.adapters.base import Adapter, AdapterOutput
from kyc_tool.api.schemas import encode_decision_callback
from kyc_tool.checkstore import repo as checkstore
from kyc_tool.config import Settings
from kyc_tool.db.audit import audit
from kyc_tool.db.session import uow
from kyc_tool.db.tables import AdapterResult, Case, DecisionRow, Event, Run
from kyc_tool.domain import scoring
from kyc_tool.domain.decision import decide, hold_positive_for_manual_review
from kyc_tool.domain.engine import ENGINE_BUILD_ID
from kyc_tool.domain.models import (
    AdapterStatus,
    BrokerStatus,
    BuyStatus,
    CaseStatus,
    CheckStatus,
    Decision,
    DecisionResult,
    RunState,
)
from kyc_tool.events import review_guard
from kyc_tool.orchestration.rate_limit import RateLimiter
from kyc_tool.orchestration.side_effects import SideEffects
from kyc_tool.orchestration.triggers import RunPlan, plan_for
from kyc_tool.outbox.publisher import enqueue_decision_callback
from kyc_tool.policy.loader import PolicyBundle
from kyc_tool.policy_store import repo as policy_store
from kyc_tool.queue import jobs
from kyc_tool.queue.jobs import ClaimedJob
from kyc_tool.storage.object_store import ObjectStore
from kyc_tool.validators.base import CheckIntent, ValidationContext
from kyc_tool.validators.build import build_intents
from kyc_tool.validators.website import website_intent

log = structlog.get_logger(__name__)

# Room between the adapter-plan retry deadline and the job lease for the transition's DB writes
# (results, checks, decision, completion) — the F2 bound is lease MINUS this, never the whole lease.
_ADAPTER_PLAN_MARGIN_SECONDS = 10.0


def plan_budget_seconds(lease_seconds: float) -> float:
    """The adapter plan's wall-clock budget: STRICTLY below the lease at every accepted value
    (re-audit `3db5f13..a7df17b` F5 — the old max(lease-10, 1) collapsed the margin to nothing for
    leases ≤ 10s and returned ≥ the whole lease at lease ≤ 1). Full margin when the lease affords
    it, else half the lease."""
    return max(lease_seconds - _ADAPTER_PLAN_MARGIN_SECONDS, lease_seconds * 0.5)


class BundleUnavailable(Exception):
    """Raised by Pipeline.resolve_bundle when bundle pinning is enforced and
    the run's creation-pin policy_bundle_hash has no matching row in
    policy_bundles. MUST propagate out of handle_job untouched (no
    try/except) — the caller resolves before dispatching any transition, so
    nothing has been written yet; the worker's except-path (queue/worker.py)
    rolls that no-op read transaction back and retries/dead-letters the job
    with zero side effects for the attempt."""


class Pipeline:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        policy: PolicyBundle,
        object_store: ObjectStore,
        settings: Settings,
        *,
        adapters: dict[str, Adapter] | None = None,
        broker_matcher: object | None = None,  # Phase 2: (session, case_snapshot) -> BrokerStatus
        intent_builder: object | None = None,
        side_effects: object | None = None,  # Phase 2: (session, run, case, AdapterOutput) -> None
    ) -> None:
        self.session_factory = session_factory
        self.policy = policy
        self.object_store = object_store
        self.settings = settings
        self.adapters = adapters or {}
        self.broker_matcher = broker_matcher
        self.intent_builder = intent_builder or build_intents
        self.side_effects = side_effects if side_effects is not None else SideEffects(settings)
        self.rate_limiter = RateLimiter(settings.adapter_rate_limits)
        # PR 6 (Task 7): in-process cache of flag-on resolved bundles, keyed by
        # bundle_hash — avoids re-loading/re-parsing the same bundle on every
        # job (see resolve_bundle below). Never populated flag-off.
        self._bundle_cache: dict[str, PolicyBundle] = {}

    # ------------------------------------------------------------------ job

    def resolve_bundle(self, session: Session, run: Run) -> PolicyBundle:
        """Resolve the policy bundle this run's transitions must use.

        Flag-off: always the process-loaded bundle — self.policy — so
        behavior stays byte-identical to pre-PR6. Flag-on: the run's
        creation-pin (run.policy_bundle_hash), loaded from policy_bundles; a
        miss raises BundleUnavailable rather than silently falling back to
        self.policy, so an unresolvable pin refuses instead of scoring under
        the wrong rubric. Flag-on results are cached on self by bundle_hash.
        """
        if not self.settings.enforce_bundle_pinning:
            return self.policy
        bundle_hash = run.policy_bundle_hash
        cached = self._bundle_cache.get(bundle_hash)
        if cached is not None:
            return cached
        bundle = policy_store.load_bundle(session, bundle_hash)
        if bundle is None:
            raise BundleUnavailable(
                f"run {run.id}: policy bundle {bundle_hash!r} is unavailable "
                "(enforce_bundle_pinning=True)"
            )
        self._bundle_cache[bundle_hash] = bundle
        return bundle

    def handle_job(self, job: ClaimedJob) -> None:
        run_id = job.payload["run_id"]
        # Resolve FIRST, before dispatching any transition or side effect:
        # flag-on + an absent bundle raises BundleUnavailable here and it
        # PROPAGATES unmodified (no try/except) — the worker's except-path
        # then rolls this no-op read transaction back and retries/dead-letters
        # the job with zero side effects (no adapter call, check, decision,
        # token, email, or outbox row) for this attempt.
        with uow(self.session_factory) as session:
            run = session.get(Run, run_id)
            bundle = self.resolve_bundle(session, run)
        for _ in range(32):  # hard bound; a run has ≤ ~8 transitions
            # Revocation boundary (re-audit `3db5f13..a7df17b` F2): a claim the heartbeat marked
            # lost stops HERE — before the next transition's reads, writes, or external calls.
            jobs.check_claim_live()
            state = self._current_state(run_id)
            if state in (RunState.PUBLISH_DECISION, RunState.COMPLETE, RunState.FAILED):
                # idempotent when the decide txn already completed the job
                with uow(self.session_factory) as session:
                    jobs.complete(session, job)  # stale fence-miss is fine here: the run is already terminal
                return
            self._advance(run_id, state, job=job, bundle=bundle)
        raise RuntimeError(f"run {run_id} did not reach a publishable state (loop bound)")

    def on_dead_letter(self, job: ClaimedJob, error: str, *, session: Session | None = None) -> None:
        run_id = (job.payload or {}).get("run_id")
        if not run_id:
            return
        if session is not None:
            # SAME-TXN path (re-audit `3db5f13..a7df17b` F3): the worker passes its fail/reap txn so
            # the job terminal and the run-FAILED transition commit or roll back together.
            self._fail_run(session, job, run_id, error)
            return
        with uow(self.session_factory) as session:
            self._fail_run(session, job, run_id, error)
        log.error("run_dead_letter", run_id=run_id, error=error)

    def _fail_run(self, session: Session, job: ClaimedJob, run_id: str, error: str) -> None:
        session.execute(
            text(
                """
                UPDATE runs SET state='FAILED', error=:err, finished_at=now()
                WHERE id=:id AND state NOT IN ('COMPLETE', 'FAILED')
                """
            ),
            {"id": run_id, "err": error[:2000]},
        )
        audit(session, "run.failed", case_id=job.case_id, run_id=run_id, error=error[:500])

    # ----------------------------------------------------------- transitions

    def _current_state(self, run_id: str) -> RunState:
        with uow(self.session_factory) as session:
            state = session.execute(
                text("SELECT state FROM runs WHERE id=:id"), {"id": run_id}
            ).scalar_one()
        return RunState(state)

    def _advance(self, run_id: str, state: RunState, *, job: ClaimedJob, bundle: PolicyBundle) -> None:
        if state is RunState.QUEUED:
            self._simple_hop(run_id, RunState.QUEUED, RunState.RESOLVE_INPUTS)
        elif state is RunState.RESOLVE_INPUTS:
            self._resolve_inputs(run_id)
        elif state is RunState.BROKER_GATE:
            self._broker_gate(run_id)
        elif state is RunState.RUN_ADAPTERS:
            self._run_adapters(run_id)
        elif state in (RunState.VALIDATE, RunState.DECIDE):
            self._decide_txn(run_id, from_state=state, job=job, bundle=bundle)
        else:  # WRITE_CHECKS / SCORE can never be persisted; see module docstring
            raise RuntimeError(f"run {run_id} in unexpected persisted state {state}")

    def _hop(self, session: Session, run_id: str, from_state: RunState, to_state: RunState) -> bool:
        """Guarded state move — the idempotency shield for retried transitions. Every hop first
        PROVES the ambient claim nonce is still live INSIDE this transaction (F2): a stale claimant
        raises StaleJobClaim here and the whole transaction (hop + everything committed with it,
        including the fused decide writes) rolls back instead of clobbering the new owner's run."""
        jobs.assert_live(session)
        moved = session.execute(
            text("UPDATE runs SET state=:to WHERE id=:id AND state=:from"),
            {"id": run_id, "to": to_state.value, "from": from_state.value},
        )
        return moved.rowcount == 1

    def _simple_hop(self, run_id: str, from_state: RunState, to_state: RunState) -> None:
        with uow(self.session_factory) as session:
            if self._hop(session, run_id, from_state, to_state):
                run = session.get(Run, run_id)
                audit(session, "run.stage", case_id=run.case_id, run_id=run_id, stage=to_state.value)

    def _load(self, session: Session, run_id: str) -> tuple[Run, Case, Event]:
        run = session.get(Run, run_id)
        case = session.get(Case, run.case_id, with_for_update=True)
        event = session.get(Event, run.triggering_event_id)
        return run, case, event

    @staticmethod
    def _run_snapshot(run: Run, case: Case) -> dict:
        """The run's FROZEN inputs — what broker matching, adapters and
        validators evaluate. Never the case's evolving `submitted_json`, so a
        later event or another run can't change what this run sees. Falls back
        to the case snapshot only for pre-008 runs that never recorded one."""
        snapshot = run.input_snapshot_json
        return snapshot if snapshot is not None else (case.submitted_json or {})

    @staticmethod
    def _seed_in_run_context(snapshot: dict, adapter_id: str, normalized: dict) -> dict:
        """Discovery context that LATER adapters in the same run read (the website
        review task). Applied at the producing adapter's slot whether it ran fresh
        or was skipped as already-recorded on a resume — otherwise a crash between
        Floqer and the website adapter drops the context (neither adapter's
        input_hash depends on it, so seeding here is resume-safe)."""
        if adapter_id == "floqer_company_enrichment" and normalized.get("discovered"):
            return {**snapshot, "floqer_context": normalized}
        return snapshot

    def _resolve_inputs(self, run_id: str) -> None:
        with uow(self.session_factory) as session:
            jobs.assert_live(session)  # HELD fence FIRST — lock order job → case → run (R9-F1)
            run, case, event = self._load(session, run_id)
            if not self._hop(session, run_id, RunState.RESOLVE_INPUTS, RunState.BROKER_GATE):
                return
            audit(
                session,
                "run.inputs_resolved",
                case_id=case.id,
                run_id=run_id,
                event_type=event.event_type,
                snapshot_keys=sorted(self._run_snapshot(run, case).keys()),
            )

    def _broker_gate(self, run_id: str) -> None:
        with uow(self.session_factory) as session:
            jobs.assert_live(session)  # HELD fence FIRST — lock order job → case → run (R9-F1)
            run, case, event = self._load(session, run_id)
            plan = plan_for(event.event_type)
            status = BrokerStatus(case.broker_status)
            if plan.run_broker_gate and self.broker_matcher is not None:
                status = self.broker_matcher(session, self._run_snapshot(run, case))
                case.broker_status = status.value
            blocked = status is BrokerStatus.BLOCKED
            target = RunState.DECIDE if blocked else RunState.RUN_ADAPTERS
            if not self._hop(session, run_id, RunState.BROKER_GATE, target):
                return
            audit(
                session,
                "run.broker_gate",
                case_id=case.id,
                run_id=run_id,
                broker_status=status.value,
                short_circuit=blocked,
            )

    # ---------------------------------------------------------- RUN_ADAPTERS

    def _run_adapters(self, run_id: str) -> None:
        # Read the plan and inputs in a short txn…
        with uow(self.session_factory) as session:
            run, case, event = self._load(session, run_id)
            plan: RunPlan = plan_for(event.event_type)
            snapshot = dict(self._run_snapshot(run, case))
            event_dict = {"event_type": event.event_type, "payload": event.payload_json or {}}
            recorded_rows = session.execute(
                text(
                    "SELECT adapter_id, input_hash, normalized_json "
                    "FROM adapter_results WHERE run_id=:r"
                ),
                {"r": run_id},
            ).fetchall()
            recorded = {(r.adapter_id, r.input_hash) for r in recorded_rows}
            # in-run context produced on a PRIOR attempt, keyed by adapter, so a
            # resumed loop can reseed it at the skipped adapter's slot below
            recorded_normalized = {r.adapter_id: (r.normalized_json or {}) for r in recorded_rows}
            case_id = case.id

        # …fetch OUTSIDE any transaction, recording each result in its own txn.
        # ONE monotonic deadline for the WHOLE adapter plan, STRICTLY below the job lease minus a DB
        # margin (re-audit `f2929f8..6a4cd87` F2; formula per `3db5f13..a7df17b` F5 — the old
        # max(lease-10, 1) collapsed the margin to ~nothing for leases ≤ 10s): in-adapter retry
        # sleeps and rate waits must never outlive the claim.
        plan_deadline = time.monotonic() + plan_budget_seconds(self.settings.job_lease_seconds)
        for adapter_id in plan.adapters:
            # Revocation boundary (F2): a lost claim stops BEFORE the next permit/wire call.
            jobs.check_claim_live()
            adapter = self.adapters.get(adapter_id)
            if adapter is None:
                continue  # not built yet (phase gating) or intentionally absent
            input_hash = adapter.input_hash(snapshot, event_dict)
            if (adapter_id, input_hash) in recorded:
                # already fetched on a prior attempt — still reseed the in-run
                # context it produced so later adapters aren't starved on resume
                snapshot = self._seed_in_run_context(
                    snapshot, adapter_id, recorded_normalized.get(adapter_id, {})
                )
                continue
            started = time.monotonic()
            try:
                # The budget exists BEFORE the first permit and EVERY send — the first included —
                # passes through the helper's single authority: deadline-aware permit → DB claim
                # re-proof → remaining-deadline proof (re-audit `7d1c435..827bc0f` F5), then a
                # streamed, byte-capped, absolute-deadline wire call (F3/F6). The budget travels
                # via contextvar so adapter signatures stay unchanged.
                with retry.budget_scope(
                    retry.RetryBudget(
                        deadline_monotonic=plan_deadline,
                        acquire=lambda a=adapter_id: self.rate_limiter.acquire(
                            a, deadline_monotonic=plan_deadline
                        ),
                        prove_live=lambda: jobs.prove_live_for_send(self.session_factory),
                        max_response_bytes=self.settings.adapter_max_response_bytes,
                        hard_kill=self.settings.adapter_hard_kill_boundary,
                    )
                ):
                    output = adapter.run(snapshot, event_dict)
            except jobs.StaleJobClaim:
                raise  # revocation is not an upstream error — abort the plan, commit nothing
            except Exception as exc:  # noqa: BLE001 — upstream failure ≠ check failure
                output = AdapterOutput(
                    adapter_id=adapter_id,
                    status=AdapterStatus.UPSTREAM_ERROR,
                    error=str(exc),
                )
            latency_ms = int((time.monotonic() - started) * 1000)
            # Post-external-call boundary (F2): a claim lost DURING the fetch must not stage object
            # bytes or open the recording transaction.
            jobs.check_claim_live()

            # Stage the raw bytes OUTSIDE the fenced transaction (re-audit `7d1c435..827bc0f` F1:
            # never hold the job row lock over object-store I/O); the reference is attached only
            # under the held in-txn fence below, and a stale fence orphan-cleans the staged object.
            raw_ref = None
            if output.raw is not None:
                # adapter-raw/ NAMESPACE (R10-F8): staged-by-pipeline objects live under one
                # prefix so the crash-window sweeper can enumerate ONLY them — platform-uploaded
                # documents (uploads/…, arbitrary keys) are never sweep candidates.
                key = f"adapter-raw/{case_id}/{run_id}/{adapter_id}/{uuid.uuid4().hex}"
                raw_ref = self.object_store.put(key, output.raw)
            try:
                with uow(self.session_factory) as session:
                    # HELD in-txn fence (F1): assert_live locks the job row FIRST (lock order
                    # job → case → run/task) and PostgreSQL holds it through commit — the
                    # AdapterResult, partial flag, and every side effect (tasks, POC tokens/emails)
                    # commit only under an authority no reaper/claimant/recovery can overtake.
                    jobs.assert_live(session)
                    self._record_adapter_result(
                        session, run_id, case_id, adapter_id, output, input_hash, raw_ref, latency_ms
                    )
            except Exception:
                # EVERY pre-commit failure orphan-cleans the staged bytes, not just a stale fence
                # (re-audit `750630c..ca85355` F8): the DB rolled back, so no reference exists —
                # bytes surviving a side-effect/commit error would be untracked evidence. The
                # crash window (process death between put and commit) is covered by the
                # staged-evidence sweeper (ops/sweep_staged_evidence.py).
                if raw_ref is not None:
                    try:
                        self.object_store.delete(raw_ref)  # ref never committed
                    except Exception:  # noqa: BLE001 — cleanup is best-effort, the error is not
                        log.warning("orphan_object_cleanup_failed", ref=raw_ref, run_id=run_id)
                raise
            snapshot = self._seed_in_run_context(snapshot, adapter_id, output.normalized)

        with uow(self.session_factory) as session:
            if self._hop(session, run_id, RunState.RUN_ADAPTERS, RunState.VALIDATE):
                audit(session, "run.stage", case_id=case_id, run_id=run_id, stage="VALIDATE")

    def _record_adapter_result(
        self, session, run_id, case_id, adapter_id, output, input_hash, raw_ref, latency_ms
    ) -> None:
        """The adapter recording writes — called ONLY under the held assert_live fence above."""
        session.add(
            AdapterResult(
                run_id=run_id,
                adapter_id=adapter_id,
                status=output.status.value,
                raw_ref=raw_ref,
                normalized_json=output.normalized,
                input_hash=input_hash,
                latency_ms=latency_ms,
            )
        )
        if output.status.value == "upstream_error":
            session.execute(text("UPDATE runs SET partial=true WHERE id=:id"), {"id": run_id})
        if self.side_effects is not None:
            run_row = session.get(Run, run_id)
            case_row = session.get(Case, case_id, with_for_update=True)
            self.side_effects(session, run_row, case_row, output)
        audit(
            session,
            "adapter.recorded",
            case_id=case_id,
            run_id=run_id,
            adapter_id=adapter_id,
            status=output.status.value,
            latency_ms=latency_ms,
            raw_ref=raw_ref,
            error=(output.error or None),
        )

    # ------------------------------------------------- the fused decide txn

    def _decide_txn(
        self, run_id: str, from_state: RunState, *, job: ClaimedJob, bundle: PolicyBundle
    ) -> None:
        """VALIDATE → WRITE_CHECKS → SCORE → DECIDE → PUBLISH_DECISION in ONE
        commit — a decision always corresponds to an exact set of live checks.

        `bundle` is this run's resolved policy bundle (Pipeline.resolve_bundle,
        called once at job entry — see handle_job). Flag-off it IS self.policy
        (same object), so scoring and provenance below stay byte-identical to
        pre-PR6."""
        with uow(self.session_factory) as session:
            # HELD fence FIRST (R9-F1): the job row lock is taken before the Case FOR UPDATE in
            # _load — ONE lock order (job → case → run/task) across worker, reaper, and recovery —
            # and PostgreSQL holds it through this whole fused commit; the fenced complete() at the
            # end is then the second, terminal proof on the same locked row.
            jobs.assert_live(session)
            run, case, event = self._load(session, run_id)
            if not self._hop(session, run_id, from_state, RunState.PUBLISH_DECISION):
                return  # another attempt already decided

            # PR 6 (Task 9): which engine build resolved/scored this run — paired
            # with the run's IMMUTABLE creation-pin policy_bundle_hash (never
            # written here). Stamped on every decide, flag on or off.
            run.engine_build_id = ENGINE_BUILD_ID

            # Authoritative website-completion guard (PR 5b): the case is
            # already FOR UPDATE from _load above, so locking the referenced
            # ReviewTask here preserves a consistent case→task lock order.
            # Evaluated once, unconditionally on from_state (VALIDATE or the
            # broker-blocked short-circuit DECIDE), from the PERSISTED event —
            # re-validated even though ingest already checked it, because an
            # event queued before this deploy never saw that floor.
            website_guard = None
            website_task = None
            if event.event_type == "website.review_completed":
                website_guard, website_task = review_guard.evaluate_website_completion(
                    session, case.id, event
                )

            # VALIDATE (logical stage)
            intents: list[CheckIntent] = []
            if from_state is RunState.VALIDATE:  # short-circuit path skips validators
                adapter_outputs = {
                    r.adapter_id: r.normalized_json
                    for r in session.execute(
                        text(
                            "SELECT adapter_id, normalized_json FROM adapter_results "
                            "WHERE run_id=:r AND status='ok'"
                        ),
                        {"r": run_id},
                    )
                }
                live_views = tuple(
                    checkstore.as_view(c) for c in checkstore.live_checks(session, case.id)
                )
                ctx = ValidationContext(
                    case_snapshot=dict(self._run_snapshot(run, case)),
                    event_type=event.event_type,
                    event_payload=dict(event.payload_json or {}),
                    adapter_outputs=adapter_outputs,
                    live_checks=live_views,
                    extras=self._validation_extras(session, case, event, website_guard=website_guard),
                )
                intents = list(self.intent_builder(bundle, ctx))
            # website.review_completed carries the reviewer verdict in its payload (no
            # adapters), so its check must be produced even on the broker-blocked DECIDE
            # short-circuit — otherwise an eligible completion closes the task with no +10.
            if (
                event.event_type == "website.review_completed"
                and from_state is RunState.DECIDE
                and website_guard is not None
                and website_guard.eligible
            ):
                intents.append(website_intent(event.payload_json or {}, website_guard.reviewer_id))
            audit(
                session, "run.stage", case_id=case.id, run_id=run_id, stage="VALIDATE",
                intents=len(intents),
            )

            # identity invalidation (item 5): stale identity-bound proof is
            # superseded on an ORG-ID/POC change BEFORE new checks are written,
            # independent of whether the revalidation adapter succeeded
            checkstore.supersede_stale_identity_proof(
                session,
                case_id=case.id,
                event_type=event.event_type,
                payload=event.payload_json or {},
                run_id=run_id,
                policy_bundle_hash=bundle.bundle_hash,
                rubric=bundle.rubric,
            )

            # WRITE_CHECKS (logical stage) — includes the ORG-ID→POC cascade
            checkstore.apply_check_intents(
                session,
                case_id=case.id,
                intents=intents,
                rubric=bundle.rubric,
                run_id=run_id,
                policy_bundle_hash=bundle.bundle_hash,
            )
            if self.side_effects is not None:
                self.side_effects.on_event(
                    session, case, event, intents,
                    website_guard=website_guard, website_task=website_task,
                )
            audit(session, "run.stage", case_id=case.id, run_id=run_id, stage="WRITE_CHECKS",
                  written=len(intents))

            # SCORE (logical stage) — from live checks only
            views = [checkstore.as_view(c) for c in checkstore.live_checks(session, case.id)]
            if self.settings.enforce_bundle_pinning:
                # PR 6 (Task 8): re-price every live check from the pinned bundle's
                # rubric so score, gates, and the callback all reflect it — not
                # each check's stamped points/category from whatever era wrote it.
                views = scoring.rubric_scoring_views(views, bundle.rubric)
            breakdown = scoring.score(views)
            gates = scoring.evaluate_gates(
                views,
                breakdown.score,
                bundle.rubric.threshold,
                BrokerStatus(case.broker_status),
                bundle.rubric.allowed_broker_statuses,
            )
            org_passed = scoring.org_id_check_passed(views)
            audit(session, "run.stage", case_id=case.id, run_id=run_id, stage="SCORE",
                  score=breakdown.score, by_check=breakdown.by_check, gates=gates.as_dict())

            # DECIDE (logical stage)
            computed = decide(breakdown.score, gates, org_passed, BrokerStatus(case.broker_status))
            # Emergency enforcement overlay (temporary): while approval-grade
            # validators are known-permissive, hold auto-enforceable positives
            # for manual review. The computed decision stays in the audit trail.
            result = (
                computed
                if self.settings.enforce_positive_decisions
                else hold_positive_for_manual_review(computed)
            )
            enforcement_held = result.decision is not computed.decision
            # PR 7b-core: allocate this callback-emitting decision's per-case ordinal from
            # the LOCKED counter (never max()+1). The Case is FOR UPDATE from _load above,
            # so this single writer of the counter is race-free.
            case.last_decision_sequence = (case.last_decision_sequence or 0) + 1
            decision_sequence = case.last_decision_sequence
            decision_row = DecisionRow(
                case_id=case.id,
                run_id=run_id,
                decision=result.decision.value,
                score=result.score,
                gates_json=result.gates.as_dict(),
                buy_enablement=result.buy_enablement.value,
                policy_shas=bundle.shas,
                engine_build_id=ENGINE_BUILD_ID,
                decision_sequence=decision_sequence,
            )
            session.add(decision_row)

            self._project_case(case, result, org_passed)
            event.processed_at = datetime.now(UTC)

            body = self._callback_body(case, run, event, result, views)
            if enforcement_held:
                body["enforcement_held"] = {
                    "computed_decision": computed.decision.value,
                    "reason": "positive_enforcement_disabled",
                }
            # Encode LAST, after every optional field is in place, so validation covers the whole
            # body rather than a prefix of it (Wave 0 gate finding 6). This is the only path to the
            # wire: an unmodelled key is dropped here, so the emitter cannot publish a field the
            # contract does not declare.
            body = encode_decision_callback(body)
            enqueue_decision_callback(
                session, case_id=case.id, run_id=run_id, body=body, decision_sequence=decision_sequence
            )
            # job completion is atomic with the decision commit — and FENCED (PR 7a slice): a
            # stale worker (reaped + reclaimed) must roll this whole decision back, not commit a
            # duplicate the platform then has to reconcile.
            if not jobs.complete(session, job):
                raise jobs.StaleJobClaim(
                    f"job {job.id} claim nonce is no longer live — "
                    "rolling back this decide transaction"
                )
            audit(
                session,
                "run.decided",
                case_id=case.id,
                run_id=run_id,
                decision=result.decision.value,
                computed_decision=computed.decision.value,
                enforcement_held=enforcement_held,
                score=result.score,
                gates=result.gates.as_dict(),
                buy_enablement=result.buy_enablement.value,
                policy_bundle_hash=bundle.bundle_hash,
                resolved_policy_bundle_hash=bundle.bundle_hash,
                partial=run.partial,
            )

    def _validation_extras(
        self, session: Session, case: Case, event: Event, *, website_guard=None
    ) -> dict:
        """DB reads the pure validators need, gathered here (they can't fetch)."""
        extras: dict = {}
        if website_guard is not None:
            extras["website_guard"] = website_guard
        if event.event_type == "poc.token_verified":
            rows = session.execute(
                text(
                    "SELECT id, token_hash, expired_at, verified_at, consumed_at, "
                    "rir, org_handle, resource, poc_handle FROM poc_tokens "
                    "WHERE case_id=:c ORDER BY sent_at DESC"
                ),
                {"c": case.id},
            ).fetchall()
            extras["poc_tokens"] = [dict(r._mapping) for r in rows]
        return extras

    def _project_case(self, case: Case, result: DecisionResult, org_passed: bool) -> None:
        case.current_score = result.score
        case.latest_decision = result.decision.value
        sticky_manual = case.status == CaseStatus.APPROVED_MANUAL.value
        if result.decision is Decision.APPROVE:
            if not sticky_manual:
                case.status = CaseStatus.ACCOUNT_APPROVED.value
            case.buy_status = BuyStatus.BUY_ENABLED.value
        elif result.decision is Decision.APPROVE_BUY_LOCKED:
            if not sticky_manual:
                case.status = CaseStatus.ACCOUNT_APPROVED.value
            case.buy_status = BuyStatus.BUY_LOCKED_ORG_ID_REQUIRED.value
        elif result.decision is Decision.REJECT:
            case.status = CaseStatus.REJECTED.value
            case.buy_status = BuyStatus.BUY_SUSPENDED.value
        else:  # manual_review_insufficient — holding state
            if sticky_manual:
                # manual approval is platform-enforced; only buy state tracks ORG-ID
                case.buy_status = (
                    BuyStatus.BUY_ENABLED.value
                    if org_passed
                    else BuyStatus.BUY_LOCKED_ORG_ID_REQUIRED.value
                )
            else:
                case.status = CaseStatus.MANUAL_REVIEW_INSUFFICIENT.value
                case.buy_status = BuyStatus.NOT_APPLICABLE.value

    def _callback_body(
        self, case: Case, run: Run, event: Event, result: DecisionResult, views: list
    ) -> dict:
        body = {
            "case_id": case.id,
            "run_id": run.id,
            "event_id": event.id,
            "decision": result.decision.value,
            "score": result.score,
            "gates": result.gates.as_dict(),
            "buy_enablement": result.buy_enablement.value,
            "checks": [
                {
                    "type": v.check_type,
                    "status": v.status.value,
                    "points": v.points_awarded if v.status is CheckStatus.PASS else 0,
                    "source": v.source,
                    "reason_codes": list(v.reason_codes),
                }
                for v in views
            ],
            "decided_at": datetime.now(UTC).isoformat(),
        }
        # D1: the per-case ordinal of the triggering event, so the platform can
        # order callbacks. Gated behind the M3 cutover flag until accepted.
        if self.settings.callback_include_event_sequence:
            body["event_sequence"] = event.event_sequence
        return body
