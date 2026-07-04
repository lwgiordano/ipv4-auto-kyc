"""email_verification adapter — approval-grade, automated, local.

The platform delivers verification state in the event payload (merged into the
case snapshot at ingest); there is nothing to fetch upstream in v1.
TODO(integration): optional platform API fetch for kyb.run_requested events
that arrive without email state.
"""

import json

from kyc_tool.adapters.base import AdapterOutput, hash_inputs
from kyc_tool.domain.models import AdapterStatus


class EmailVerificationAdapter:
    adapter_id = "email_verification"

    def input_hash(self, case_snapshot: dict, event: dict) -> str:
        return hash_inputs(self.adapter_id, case_snapshot.get("email"))

    def run(self, case_snapshot: dict, event: dict) -> AdapterOutput:
        email_state = case_snapshot.get("email")
        if not email_state:
            return AdapterOutput(self.adapter_id, AdapterStatus.NOT_APPLICABLE)
        normalized = {
            "verified": bool(email_state.get("verified_at") or email_state.get("verified")),
            "email": email_state.get("email", ""),
            "domain": email_state.get("domain", ""),
            "verified_at": email_state.get("verified_at"),
        }
        return AdapterOutput(
            self.adapter_id,
            AdapterStatus.OK,
            raw=json.dumps(email_state, default=str).encode(),
            normalized=normalized,
        )
