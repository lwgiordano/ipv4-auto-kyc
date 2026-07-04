"""Website check — the HUMAN is the validator (no crawler in v1).

The reviewer's verdict arrives in the website.review_completed payload; this
just turns it into a check intent whose source is the reviewer identity.
"""

from kyc_tool.domain.models import CheckStatus
from kyc_tool.validators.base import CheckIntent


def website_intent(event_payload: dict) -> CheckIntent:
    passed = event_payload.get("result") == "pass"
    reviewer = event_payload.get("reviewer_id", "unknown-reviewer")
    return CheckIntent(
        "website_verified",
        CheckStatus.PASS if passed else CheckStatus.FAIL,
        reason_codes=tuple(event_payload.get("reason_codes", ())),
        source=f"reviewer:{reviewer}",
        source_detail={"task_id": event_payload.get("task_id")},
    )
