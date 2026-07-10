"""Single-source-of-truth drift guards.

The hardcoded routing/vocabulary tables (run plans, adapter order, gate keys,
run states) duplicate the normative policy bundle for readability. Rather than
derive them dynamically (which would lose the per-event routing intent that is
NOT in the catalog), we pin the duplication here: a spec change now surfaces as
a failing test at CI instead of silently drifting in production.

Audit: the "altitude / design depth" cluster.
"""

import re

from kyc_tool.api.schemas import PAYLOAD_MODELS, GatesBody
from kyc_tool.domain.models import Gates, RunState
from kyc_tool.orchestration.triggers import _PLANS, FULL_RUN_ADAPTERS
from kyc_tool.ui.routes import _CONSOLE_HTML


def test_run_plans_cover_the_spec_events(policy):
    # reviewer.manual_approve is record-only (handled inline in ingest, no run),
    # so it is the single event type without a run plan.
    assert set(_PLANS) == set(policy.events.event_types) - {"reviewer.manual_approve"}


def test_payload_models_cover_exactly_the_spec_events(policy):
    assert set(PAYLOAD_MODELS) == set(policy.events.event_types)


def test_full_run_adapters_match_catalog_order(policy):
    # the broker gate is a separate pipeline stage, not a full-run adapter
    catalog = [a for a in policy.adapter_catalog.adapter_ids if a != "broker_policy"]
    assert list(FULL_RUN_ADAPTERS) == catalog


def test_gate_vocabulary_matches_callback_schema():
    gates = Gates(True, True, True, True, True)
    assert set(gates.as_dict()) == set(GatesBody.model_fields)


def test_run_state_enum_covers_spec_states(policy):
    # every state the spec machine defines must have an enum member
    assert set(policy.state_machine.run_states) <= {s.value for s in RunState}


def test_console_pipeline_strip_covers_every_persisted_state():
    # the console run-state strip must not omit a persisted state (e.g. DECIDE on
    # the broker short-circuit path) — that renders a blank/-1 pipeline for ops.
    match = re.search(r"const RUN_STATES=\[([^\]]+)\]", _CONSOLE_HTML)
    assert match, "RUN_STATES not found in console.html"
    listed = {token.strip().strip('"') for token in match.group(1).split(",")}
    non_terminal = {s.value for s in RunState} - {"FAILED"}
    assert non_terminal <= listed
