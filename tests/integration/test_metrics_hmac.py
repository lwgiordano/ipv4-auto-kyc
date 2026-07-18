"""/v1/metrics surfaces the DB-backed HMAC witness + counters (PR 5a §6) so ops
can watch v1 traffic reach zero across replicas before the inbound sunset."""

import pytest

from kyc_tool.api import hmac_witness

pytestmark = pytest.mark.postgres


def test_metrics_exposes_hmac_witness(client, session_factory, clean_db):
    with session_factory() as s:
        hmac_witness.record_v1_accepted(s)
        hmac_witness.bump_stat(s, "v2_accepted")
        s.commit()

    hmac = client.get("/v1/metrics").json()["hmac"]
    assert hmac["v1_accepted"] >= 1
    assert hmac["v2_accepted"] >= 1
    assert hmac["observation"]["active"] is False  # seeded inactive
