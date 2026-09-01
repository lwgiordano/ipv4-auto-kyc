import pytest

from kyc_tool.events.review_guard import REVIEWER_ACTOR_INVALID, reviewer_actor_reason

OK = {"type": "reviewer", "id": "rev-1"}

@pytest.mark.parametrize("actor,payload,expected", [
    (OK, {"reviewer_id": "rev-1"}, None),                              # valid
    ({"type": "system", "id": "rev-1"}, {"reviewer_id": "rev-1"}, REVIEWER_ACTOR_INVALID),  # wrong type
    (OK, {"reviewer_id": "rev-2"}, REVIEWER_ACTOR_INVALID),            # mismatch
    ({"type": "reviewer", "id": ""}, {"reviewer_id": ""}, REVIEWER_ACTOR_INVALID),   # both blank
    ({"type": "reviewer", "id": "  "}, {"reviewer_id": "  "}, REVIEWER_ACTOR_INVALID),  # whitespace
    ({"type": "reviewer", "id": "rev-1 "}, {"reviewer_id": "rev-1"}, None),  # trailing ws stripped
    (OK, {}, REVIEWER_ACTOR_INVALID),                                 # payload missing reviewer_id
])
def test_reviewer_actor_reason(actor, payload, expected):
    assert reviewer_actor_reason(actor, payload) == expected
