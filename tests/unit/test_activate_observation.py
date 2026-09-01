"""v1 observation activation (PR 5a §6a): the post-cutover, idempotent
compare-and-set that starts the witness clock. A rerun must never reset a live
window."""

import pytest
from sqlalchemy import text

from kyc_tool.ops.activate_hmac_v1_observation import activate

pytestmark = pytest.mark.postgres


def test_first_activation_then_rerun_no_reset(session_factory, clean_db):
    assert activate(session_factory) is True
    with session_factory() as s:
        first = s.execute(
            text("SELECT observation_started_at FROM hmac_v1_observation WHERE id = 1")
        ).scalar_one()
    assert first is not None

    assert activate(session_factory) is False  # idempotent: already active
    with session_factory() as s:
        second = s.execute(
            text("SELECT observation_started_at FROM hmac_v1_observation WHERE id = 1")
        ).scalar_one()
    assert second == first  # NOT reset
