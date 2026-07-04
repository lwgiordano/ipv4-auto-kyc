"""Event → run plan routing (machine_readable/platform_events.json semantics).

AUDIT:D1 — the broker gate evaluates on every run type except
recalculate.requested, which the spec explicitly limits to "no adapter calls"
(the gate then reuses the case's stored broker status).
"""

from dataclasses import dataclass

# Full-run adapter order from adapter_catalog.json (broker gate is a separate stage).
FULL_RUN_ADAPTERS: tuple[str, ...] = (
    "email_verification",
    "companies_house",
    "gleif",
    "floqer_company_enrichment",
    "rir_rdap",
    "rir_poc",
    "document_ocr",
    "website_manual_review",
)


@dataclass(frozen=True, slots=True)
class RunPlan:
    adapters: tuple[str, ...]
    run_broker_gate: bool
    full: bool


_PLANS: dict[str, RunPlan] = {
    "kyb.run_requested": RunPlan(FULL_RUN_ADAPTERS, run_broker_gate=True, full=True),
    "email.verified": RunPlan(("email_verification",), run_broker_gate=True, full=False),
    "org_id.submitted": RunPlan(("rir_rdap",), run_broker_gate=True, full=False),
    "poc.submitted": RunPlan(("rir_poc",), run_broker_gate=True, full=False),
    # token verification is validated against poc_tokens in the decide txn — no fetch
    "poc.token_verified": RunPlan((), run_broker_gate=True, full=False),
    "document.uploaded": RunPlan(("document_ocr",), run_broker_gate=True, full=False),
    # reviewer verdict arrives in the payload — no fetch
    "website.review_completed": RunPlan((), run_broker_gate=True, full=False),
    # spec: "recompute … from current live checks; no adapter calls"
    "recalculate.requested": RunPlan((), run_broker_gate=False, full=False),
}


def plan_for(event_type: str) -> RunPlan:
    try:
        return _PLANS[event_type]
    except KeyError:
        raise ValueError(f"no run plan for event type: {event_type}") from None
