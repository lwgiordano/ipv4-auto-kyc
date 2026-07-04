"""Golden cases at the logic level (Phase 1 acceptance): each step's check
intents run through the real check store (supersession + cascade), then the
pure scoring/gates/decision chain. Expected scores are DERIVED from the rubric
via `live_pass_types` — the case files contain no point values."""

import json
import uuid
from pathlib import Path

import pytest

from kyc_tool.checkstore import repo as checkstore
from kyc_tool.db.session import uow
from kyc_tool.db.tables import Case
from kyc_tool.domain import scoring
from kyc_tool.domain.decision import decide
from kyc_tool.domain.models import BrokerStatus, CheckStatus
from kyc_tool.validators.base import CheckIntent

pytestmark = pytest.mark.postgres

CASES_DIR = Path(__file__).parent / "cases"
CASE_FILES = sorted(CASES_DIR.glob("*.json"))


def _intent(entry: dict) -> CheckIntent:
    return CheckIntent(
        check_type=entry["type"],
        status=CheckStatus(entry["status"]),
        reason_codes=tuple(entry.get("reasons", ())),
        source="golden-fixture",
        source_detail=entry.get("detail", {}),
    )


@pytest.mark.parametrize("case_file", CASE_FILES, ids=[f.stem for f in CASE_FILES])
def test_golden_case(case_file: Path, session_factory, policy, clean_db):
    spec = json.loads(case_file.read_text())
    case_id = f"golden-{spec['id']}-{uuid.uuid4().hex[:6]}"

    with uow(session_factory) as session:
        session.add(Case(id=case_id))

    for step_index, step in enumerate(spec["steps"]):
        with uow(session_factory) as session:
            case = session.get(Case, case_id, with_for_update=True)
            if "broker_status" in step:
                case.broker_status = step["broker_status"]
            checkstore.apply_check_intents(
                session,
                case_id=case_id,
                intents=[_intent(c) for c in step.get("checks", [])],
                rubric=policy.rubric,
                run_id=None,
            )
            views = [checkstore.as_view(c) for c in checkstore.live_checks(session, case_id)]
            broker_status = BrokerStatus(case.broker_status)

        breakdown = scoring.score(views)
        gates = scoring.evaluate_gates(
            views,
            breakdown.score,
            policy.rubric.threshold,
            broker_status,
            policy.rubric.allowed_broker_statuses,
        )
        result = decide(
            breakdown.score, gates, scoring.org_id_check_passed(views), broker_status
        )

        expect = step.get("expect", {})
        where = f"{spec['id']} step {step_index}"

        if "decision" in expect:
            assert result.decision.value == expect["decision"], where
        if "buy_enablement" in expect:
            assert result.buy_enablement.value == expect["buy_enablement"], where
        if "gates_all_pass" in expect:
            assert gates.all_pass == expect["gates_all_pass"], where
        for gate_name, wanted in expect.get("gates", {}).items():
            assert gates.as_dict()[gate_name] == wanted, f"{where} gate {gate_name}"
        if "live_pass_types" in expect:
            live_pass = {
                v.check_type for v in views if v.status is CheckStatus.PASS
            }
            assert live_pass == set(expect["live_pass_types"]), where
            expected_score = sum(
                policy.rubric.item(t).points for t in expect["live_pass_types"]
            )
            assert breakdown.score == expected_score, where
        for check_type in expect.get("not_live_pass", []):
            live_pass = {v.check_type for v in views if v.status is CheckStatus.PASS}
            assert check_type not in live_pass, f"{where}: {check_type} should not be live+pass"
