"""Authoring checks for the reviewed public handoff documents."""

import json
import re
import runpy
from pathlib import Path

import pytest
from docs.contracts import receiver_reference
from docs.contracts.operations import BUNDLE_PINNING

from kyc_tool.api.schemas import encode_decision_callback
from kyc_tool.domain.decision import decide, hold_positive_for_manual_review
from kyc_tool.domain.models import BrokerStatus, CheckStatus, CheckView
from kyc_tool.domain.scoring import evaluate_gates, org_id_check_passed, score
from kyc_tool.policy.loader import load_policy

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATOR = REPO_ROOT / "scripts" / "build_techcraft_handoff.py"
PUBLIC = REPO_ROOT / "scripts" / "handoff" / "docs"


def test_checked_in_document_matches_a_fresh_build():
    build = runpy.run_path(str(GENERATOR))["build"]
    doc = REPO_ROOT / "docs" / "TECHCRAFT_HANDOFF.md"
    # pytest.fail, not assert: the documents run to thousands of lines and the assertion diff
    # buries the one instruction that fixes this.
    if doc.read_text(encoding="utf-8") != build(REPO_ROOT):
        pytest.fail(
            "docs/TECHCRAFT_HANDOFF.md is stale: run "
            "`.venv/bin/python scripts/build_techcraft_handoff.py` and commit the result"
        )


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    level = len(heading) - len(heading.lstrip("#"))
    tail = text[start + len(heading) :]
    match = re.search(rf"^#{{1,{level}}} .+$", tail, flags=re.MULTILINE)
    return tail[: match.start()] if match else tail


def _flat(text: str) -> str:
    return " ".join(text.split())


def _table(text: str, header: str) -> dict[str, tuple[str, ...]]:
    lines = text[text.index(header) :].splitlines()
    rows = {}
    for line in lines[2:]:
        if not line.startswith("|"):
            break
        cells = tuple(cell.strip() for cell in line.strip("|").split("|"))
        rows[cells[0]] = cells[1:]
    return rows


def _acceptance_cases(text: str) -> dict[str, dict[str, str]]:
    section = _section(text, "### Receiver acceptance cases")
    starts = list(re.finditer(r"^#### (A\d+) — (.+)$", section, flags=re.MULTILINE))
    cases = {}
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(section)
        fields = dict(
            re.findall(
                r"^- \*\*(Input|Expected record|Expected permissions|Observable result):\*\* (.+)$",
                section[match.end() : end],
                flags=re.MULTILINE,
            )
        )
        cases[match.group(1)] = {"title": match.group(2), **fields}
    return cases


def test_public_callback_example_recomputes_from_the_normative_rubric():
    webhook = _section(
        (PUBLIC / "PLATFORM_INTEGRATION.md").read_text(),
        "## 4. Platform decision webhook",
    )
    example = json.loads(re.search(r"```json\n(.+?)\n```", webhook, flags=re.DOTALL).group(1))
    encoded = encode_decision_callback(example)
    rubric = load_policy(REPO_ROOT / "KYC_Tool_Build_Package" / "machine_readable").rubric
    views = []
    for check in encoded["checks"]:
        item = rubric.item(check["type"])
        assert check["points"] == item.points
        views.append(
            CheckView(
                check_type=check["type"],
                status=CheckStatus(check["status"]),
                points_awarded=check["points"],
                category=item.category,
                source=check["source"],
                reason_codes=tuple(check["reason_codes"]),
            )
        )
    breakdown = score(views)
    gates = evaluate_gates(
        views,
        breakdown.score,
        rubric.threshold,
        BrokerStatus.CLEAR,
        rubric.allowed_broker_statuses,
    )
    computed = decide(
        breakdown.score,
        gates,
        org_id_check_passed(views),
        BrokerStatus.CLEAR,
    )
    held = hold_positive_for_manual_review(computed)
    assert encoded["score"] == breakdown.score == 110
    assert encoded["gates"] == gates.as_dict()
    assert encoded["buy_enablement"] == computed.buy_enablement.value == "enabled"
    assert encoded["enforcement_held"]["computed_decision"] == computed.decision.value == "approve"
    assert encoded["decision"] == held.decision.value == "manual_review_insufficient"


def test_receiver_acceptance_cases_cover_permission_and_acknowledgment_boundaries():
    text = (PUBLIC / "PLATFORM_INTEGRATION.md").read_text()
    cases = _acceptance_cases(text)
    assert set(cases) == {"A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8"}
    assert "no callback or dedupe row" in cases["A1"]["Expected record"].lower()
    assert "non-2xx" in cases["A1"]["Observable result"]
    assert "same transaction" in cases["A2"]["Expected record"].lower()
    assert "commit" in cases["A2"]["Observable result"].lower()
    assert "no repeated effect" in cases["A3"]["Expected permissions"].lower()
    assert "decision=manual_review_insufficient" in cases["A4"]["Input"]
    assert "computed_decision=approve" in cases["A4"]["Input"]
    assert "buy_enablement=enabled" in cases["A4"]["Input"]
    assert "buying stays locked" in cases["A4"]["Expected permissions"].lower()
    assert "freshness" in cases["A5"]["Input"].lower()
    assert "no live permission" in cases["A5"]["Expected permissions"].lower()
    assert "manual approval remains authoritative" in cases["A6"]["Expected permissions"].lower()
    assert "last arrival" in cases["A7"]["Expected permissions"].lower()
    assert "timestamp" in cases["A7"]["Expected permissions"].lower()
    acceptance = _flat(_section(text, "### Receiver acceptance cases")).lower()
    assert "acceptance evidence has not been collected" in acceptance
    assert "does not certify permission enforcement" in acceptance


def test_public_policy_language_matches_control_and_inactive_registry_rules():
    briefing = _flat((PUBLIC / "PLATFORM_BRIEFING.md").read_text()).lower()
    integration = _flat((PUBLIC / "PLATFORM_INTEGRATION.md").read_text()).lower()
    combined = briefing + "\n" + integration
    assert "verified `poc_verified` check" in briefing
    assert "email and org-id checks remain supporting evidence" in briefing
    assert "access to the rir-listed contact channel" in combined
    assert "does not prove unrestricted legal authority" in combined
    assert "`registry_exact_company_inactive`" in briefing
    assert "generic historical `registry_company_inactive`" in briefing
    assert "governed revalidation" in briefing
    assert "historical category labels" in briefing
    assert "`partial: false`" in integration
    assert "does not prove that every live check was fetched in that run" in integration


def test_readiness_matrix_assigns_every_required_area_and_leaves_external_results_open():
    text = (PUBLIC / "PRODUCTION_READINESS.md").read_text()
    matrix = _table(
        _section(text, "## Completion matrix"),
        "| Area | Current implementation | Responsible team | Observable acceptance result |",
    )
    assert set(matrix) == {
        "Provider wiring",
        "Applicant authority",
        "Registry negatives",
        "Callback ordering",
        "Evidence freshness",
        "Reviewer workflows",
        "Sanctions",
        "Salesforce reconciliation",
        "Recovery",
        "Capacity",
    }
    for current, owner, result in matrix.values():
        assert current and owner
        assert "uncollected" in result.lower()

    freshness_current, freshness_owner, freshness_result = matrix["Evidence freshness"]
    assert "review aids only" in freshness_current.lower()
    assert "decision-bound" in freshness_current.lower()
    assert "IPv4.Global service team" in freshness_owner
    assert "TechCraft platform team" in freshness_owner
    assert "joint product approval" in freshness_owner.lower()
    assert "bind its evidence to the decision" in freshness_result.lower()
    assert "existing reads do not meet" in freshness_result.lower()


def test_bundle_epoch_command_uses_eng_2_only_for_a_new_first_activation():
    activate = [
        command.argv
        for block in BUNDLE_PINNING.playbook_ref.body
        for command in getattr(block, "commands", ())
        if command.argv[:3]
        == ("python", "-m", "kyc_tool.ops.activate_bundle_pinning_epoch")
    ]
    assert activate == [
        (
            "python",
            "-m",
            "kyc_tool.ops.activate_bundle_pinning_epoch",
            "--expect-bundle-hash",
            "<sha256>",
            "--expect-engine",
            "eng-2",
        )
    ]
    canonical = _section(
        (REPO_ROOT / "docs" / "DEPLOYMENT.md").read_text(),
        "## 10. PR 6 cutover — bundle-pinning activation",
    ).lower()
    public = _section(
        (PUBLIC / "DEPLOYMENT.md").read_text(),
        "## 10. Bundle-pinning activation",
    ).lower()
    for section in (canonical, public):
        assert "--expect-engine eng-2" in section
        assert "first activation" in section
        assert "do not reset" in section
        assert "existing activation epoch" in section


def test_interim_permission_table_matches_first_and_later_receiver_semantics():
    first = receiver_reference.decide(
        receiver_reference.LedgerState(),
        receiver_reference.Callback("case-1", "run-first"),
        phase=receiver_reference.INTERIM,
    )
    later = receiver_reference.decide(
        receiver_reference.LedgerState(current_source="automatic"),
        receiver_reference.Callback("case-1", "run-later"),
        phase=receiver_reference.INTERIM,
    )
    assert first.record and first.effective
    assert later.record and not later.effective

    section = _section(
        (PUBLIC / "PLATFORM_INTEGRATION.md").read_text(),
        "### Permission precedence",
    )
    rows = [tuple(cell.strip() for cell in line.strip("|").split("|"))
            for line in section.splitlines() if re.match(r"^\| \d+ \|", line)]
    first_row = next((row for row in rows if "First accepted" in row[1]), None)
    later_row = next((row for row in rows if "Later automatic" in row[1]), None)
    assert first_row is not None and "current automatic receiver decision" in first_row[2]
    assert later_row is not None and "hold effectiveness" in later_row[2]
    assert "current automatic receiver decision does not grant live permissions" in _flat(
        section
    ).lower()


def test_freshness_guidance_names_both_reads_and_their_evidence_limits():
    briefing = _flat((PUBLIC / "PLATFORM_BRIEFING.md").read_text()).lower()
    integration = _flat((PUBLIC / "PLATFORM_INTEGRATION.md").read_text()).lower()
    for text in (briefing, integration):
        assert "get /v1/runs/{run_id}" in text
        assert "adapters[].fetched_at" in text
        assert "get /v1/cases/{case_id}/checks" in text
        assert "checks[].created_at" in text
        assert "review aids" in text
        assert "not proof of provider-evidence freshness" in text
        assert "not a callback-time snapshot" in text
        assert "cannot prove source age or bind" in text
        assert "unavailable or unprovable" in text and "hold" in text


def test_same_identity_with_different_content_is_an_integrity_hold_not_a_duplicate():
    text = (PUBLIC / "PLATFORM_INTEGRATION.md").read_text()
    precedence = _section(text, "### Permission precedence")
    mismatch_at = precedence.find("Same `(case_id, run_id)`, different content")
    duplicate_at = precedence.find("Exact duplicate")
    first_effect_at = precedence.find("First accepted")
    assert min(mismatch_at, duplicate_at, first_effect_at) >= 0
    assert mismatch_at < duplicate_at < first_effect_at

    cases = _acceptance_cases(text)
    assert "A8" in cases
    assert "same `(case_id, run_id)`" in cases["A8"]["Input"].lower()
    assert "different content" in cases["A8"]["Input"].lower()
    assert "original durable body" in cases["A8"]["Expected record"].lower()
    assert "original outcome" in cases["A8"]["Expected record"].lower()
    assert "no permission effect" in cases["A8"]["Expected permissions"].lower()
    assert "investigation" in cases["A8"]["Observable result"].lower()
