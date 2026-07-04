"""website_manual_review adapter — MANUAL tier. There is no crawler in v1.

Its entire job is to describe the review task orchestration should open
(domain + any discovery context); a human completes it via the review queue
and the verdict re-enters as a website.review_completed event.
"""

from kyc_tool.adapters.base import AdapterOutput, hash_inputs
from kyc_tool.domain.models import AdapterStatus
from kyc_tool.validators.normalize import domain_of


class WebsiteManualReviewAdapter:
    adapter_id = "website_manual_review"

    def input_hash(self, case_snapshot: dict, event: dict) -> str:
        return hash_inputs(self.adapter_id, case_snapshot.get("website"))

    def run(self, case_snapshot: dict, event: dict) -> AdapterOutput:
        domain = domain_of(
            case_snapshot.get("website") or case_snapshot.get("company_domain")
        )
        if not domain:
            return AdapterOutput(self.adapter_id, AdapterStatus.NOT_APPLICABLE)
        return AdapterOutput(
            self.adapter_id,
            AdapterStatus.OK,
            normalized={
                "task_request": {
                    "domain": domain,
                    "company_legal_name": case_snapshot.get("company_legal_name"),
                    # Phase 3: floqer discovery context lands here too
                    "discovery": case_snapshot.get("floqer_context", {}),
                }
            },
        )
