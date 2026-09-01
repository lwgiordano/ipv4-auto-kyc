"""A minimal VALID decision-callback body for tests that exercise outbox mechanics.

`enqueue_decision_callback` is the serialization boundary (re-gate finding 3): it validates the
body through `DecisionCallback` immediately before constructing the `Outbox` row, so an unmodelled
field is refused rather than stored verbatim. Before that, the enqueue accepted any `dict`, and the
fencing/supersession/ordering suites passed payloads like `{"run_id": "r1"}` — they are testing row
machinery, not callback content, and the shortcut was invisible.

Those tests are not wrong to be uninterested in the body, so this supplies a real one. Using a
valid body also means they now exercise the same serialization path production takes, which is
where the bypass lived.
"""

from kyc_tool.api.schemas import GatesBody


def valid_callback_body(**overrides) -> dict:
    """A complete, schema-valid callback body. Override any field a test cares about."""
    body = {
        "case_id": "c1",
        "run_id": "r1",
        "event_id": "e1",
        "decision": "approve",
        "score": 10,
        "gates": dict.fromkeys(GatesBody.model_fields, True),
        "buy_enablement": "enabled",
        "checks": [],
        "decided_at": "2026-08-13T00:00:00+00:00",
    }
    body.update(overrides)
    return body
