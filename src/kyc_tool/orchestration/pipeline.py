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

from kyc_tool.adapters.base import Adapter, AdapterOutput
from kyc_tool.checkstore import repo as checkstore
from kyc_tool.config import Settings
from kyc_tool.db.audit import audit
from kyc_tool.db.session import uow
from kyc_tool.db.tables import AdapterResult, Case, DecisionRow, Event, Run
from kyc_tool.domain import scoring
from kyc_tool.domain.decision import decide, hold_positive_for_manual_review
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
from kyc_tool.orchestration.rate_limit import RateLimiter
from kyc_tool.orchestration.side_effects import SideEffects
from kyc_tool.orchestration.triggers import RunPlan, plan_for
from kyc_tool.outbox.publisher import enqueue_decision_callback
from kyc_tool.policy.loader import PolicyBundle
from kyc_tool.queue import jobs
from kyc_tool.queue.jobs import ClaimedJob
from kyc_tool.storage.object_store import ObjectStore
from kyc_tool.validators.base import CheckIntent, ValidationContext
from kyc_tool.validators.build import build_intents

log = structlog.get_logger(__name__)


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

    # ------------------------------------------------------------------ job

    def handle_job(self, job: ClaimedJob) -> None:
        run_id = job.payload["run_id"]
        for _ in range(32):  # hard bound; a run has ≤ ~8 transitions
            state = self._current_state(run_id)
            if state in (RunState.PUBLISH_DECISION, RunState.COMPLETE, RunState.FAILED):
                # idempotent when the decide txn already completed the job
                with uow(self.session_factory) as session:
                    jobs.complete(session, job.id)
                return
            self._advance(run_id, state, job_id=job.id)
        raise RuntimeError(f"run {run_id} did not reach a publishable state (loop bound)")

    def on_dead_letter(self, job: ClaimedJob, error: str) -> None:
        run_id = (job.payload or {}).get("run_id")
        if not run_id:
            return
        with uow(self.session_factory) as session:
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
        log.error("run_dead_letter", run_id=run_id, error=error)

    # ----------------------------------------------------------- transitions

    def _current_state(self, run_id: str) -> RunState:
        with uow(self.session_factory) as session:
            state = session.execute(
                text("SELECT state FROM runs WHERE id=:id"), {"id": run_id}
            ).scalar_one()
        return RunState(state)

    def _advance(self, run_id: str, state: RunState, *, job_id: int) -> None:
        if state is RunState.QUEUED:
            self._simple_hop(run_id, RunState.QUEUED, RunState.RESOLVE_INPUTS)
        elif state is RunState.RESOLVE_INPUTS:
            self._resolve_inputs(run_id)
        elif state is RunState.BROKER_GATE:
            self._broker_gate(run_id)
        elif state is RunState.RUN_ADAPTERS:
            self._run_adapters(run_id)
        elif state in (RunState.VALIDATE, RunState.DECIDE):
            self._decide_txn(run_id, from_state=state, job_id=job_id)
        else:  # WRITE_CHECKS / SCORE can never be persisted; see module docstring
            raise RuntimeError(f"run {run_id} in unexpected persisted state {state}")

    def _hop(self, session: Session, run_id: str, from_state: RunState, to_state: RunState) -> bool:
        """Guarded state move — the idempotency shield for retried transitions."""
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

    def _resolve_inputs(self, run_id: str) -> None:
        with uow(self.session_factory) as session:
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
            recorded = {
                (r.adapter_id, r.input_hash)
                for r in session.execute(
                    text("SELECT adapter_id, input_hash FROM adapter_results WHERE run_id=:r"),
                    {"r": run_id},
                )
            }
            case_id = case.id

        # …fetch OUTSIDE any transaction, recording each result in its own txn.
        for adapter_id in plan.adapters:
            adapter = self.adapters.get(adapter_id)
            if adapter is None:
                continue  # not built yet (phase gating) or intentionally absent
            input_hash = adapter.input_hash(snapshot, event_dict)
            if (adapter_id, input_hash) in recorded:
                continue  # resumability: already fetched on a prior attempt
            self.rate_limiter.acquire(adapter_id)  # per-upstream cap, held outside txns
            started = time.monotonic()
            try:
                output = adapter.run(snapshot, event_dict)
            except Exception as exc:  # noqa: BLE001 — upstream failure ≠ check failure
                output = AdapterOutput(
                    adapter_id=adapter_id,
                    status=AdapterStatus.UPSTREAM_ERROR,
                    error=str(exc),
                )
            latency_ms = int((time.monotonic() - started) * 1000)

            raw_ref = None
            if output.raw is not None:
                key = f"{case_id}/{run_id}/{adapter_id}/{uuid.uuid4().hex}"
                raw_ref = self.object_store.put(key, output.raw)

            with uow(self.session_factory) as session:
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
                    session.execute(
                        text("UPDATE runs SET partial=true WHERE id=:id"), {"id": run_id}
                    )
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
            if adapter_id == "floqer_company_enrichment" and output.normalized.get("discovered"):
                # later adapters in this same run (website review) see discovery context
                snapshot = {**snapshot, "floqer_context": output.normalized}

        with uow(self.session_factory) as session:
            if self._hop(session, run_id, RunState.RUN_ADAPTERS, RunState.VALIDATE):
                audit(session, "run.stage", case_id=case_id, run_id=run_id, stage="VALIDATE")

    # ------------------------------------------------- the fused decide txn

    def _decide_txn(self, run_id: str, from_state: RunState, *, job_id: int) -> None:
        """VALIDATE → WRITE_CHECKS → SCORE → DECIDE → PUBLISH_DECISION in ONE
        commit — a decision always corresponds to an exact set of live checks."""
        with uow(self.session_factory) as session:
            run, case, event = self._load(session, run_id)
            if not self._hop(session, run_id, from_state, RunState.PUBLISH_DECISION):
                return  # another attempt already decided

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
                    extras=self._validation_extras(session, case, event),
                )
                intents = list(self.intent_builder(self.policy, ctx))
            audit(
                session, "run.stage", case_id=case.id, run_id=run_id, stage="VALIDATE",
                intents=len(intents),
            )

            # WRITE_CHECKS (logical stage) — includes the ORG-ID→POC cascade
            checkstore.apply_check_intents(
                session,
                case_id=case.id,
                intents=intents,
                rubric=self.policy.rubric,
                run_id=run_id,
            )
            if self.side_effects is not None:
                self.side_effects.on_event(session, case, event, intents)
            audit(session, "run.stage", case_id=case.id, run_id=run_id, stage="WRITE_CHECKS",
                  written=len(intents))

            # SCORE (logical stage) — from live checks only
            views = [checkstore.as_view(c) for c in checkstore.live_checks(session, case.id)]
            breakdown = scoring.score(views)
            gates = scoring.evaluate_gates(
                views,
                breakdown.score,
                self.policy.rubric.threshold,
                BrokerStatus(case.broker_status),
                self.policy.rubric.allowed_broker_statuses,
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
            decision_row = DecisionRow(
                case_id=case.id,
                run_id=run_id,
                decision=result.decision.value,
                score=result.score,
                gates_json=result.gates.as_dict(),
                buy_enablement=result.buy_enablement.value,
                policy_shas=self.policy.shas,
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
            enqueue_decision_callback(session, case_id=case.id, run_id=run_id, body=body)
            # job completion is atomic with the decision commit
            jobs.complete(session, job_id)
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
                policy_bundle_hash=self.policy.bundle_hash,
                partial=run.partial,
            )

    def _validation_extras(self, session: Session, case: Case, event: Event) -> dict:
        """DB reads the pure validators need, gathered here (they can't fetch)."""
        extras: dict = {}
        if event.event_type == "poc.token_verified":
            rows = session.execute(
                text(
                    "SELECT id, token_hash, expired_at, verified_at FROM poc_tokens "
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
