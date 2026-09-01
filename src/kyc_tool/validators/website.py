"""Website check — the HUMAN is the validator (no crawler in v1).

The reviewer's verdict arrives in the website.review_completed payload; this
just turns it into a check intent. `reviewer_id` is the actor-derived, trusted
identity from the decide-txn guard (kyc_tool.events.review_guard) — NOT read
from the payload, which is caller-supplied and unverified.
"""

from kyc_tool.domain.models import CheckStatus
from kyc_tool.validators.base import CheckIntent


def website_intent(event_payload: dict, reviewer_id: str) -> CheckIntent:
    passed = event_payload.get("result") == "pass"
    return CheckIntent(
        "website_verified",
        CheckStatus.PASS if passed else CheckStatus.FAIL,
        reason_codes=tuple(event_payload.get("reason_codes", ())),
        source=f"reviewer:{reviewer_id}",
        source_detail={"task_id": event_payload.get("task_id")},
    )
