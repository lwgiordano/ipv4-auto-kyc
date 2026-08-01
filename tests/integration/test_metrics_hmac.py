"""/v1/metrics surfaces the DB-backed v1 witness plus the PROCESS-LOCAL diagnostic counters (PR 5a
§6) so ops can watch v1 traffic reach zero before the inbound sunset. The v2/rejected diagnostics are
process-local, not DB-backed (re-audit `d569a15..4938840` F1: the rejected path must never write)."""

import pytest

from kyc_tool.api import auth, hmac_witness

pytestmark = pytest.mark.postgres


def test_metrics_exposes_v1_witness_and_process_local_diagnostics(client, session_factory, clean_db):
    with session_factory() as s:
        hmac_witness.record_v1_accepted(s)  # DB-backed, fail-closed, aggregates across replicas
        s.commit()
    auth._bump("v2_accepted")  # process-local diagnostic (what real accepted-v2 auth increments)

    hmac = client.get("/v1/metrics").json()["hmac"]
    assert hmac["v1_accepted"] >= 1
    assert hmac["diagnostics_scope"] == "process_local"     # never mistaken for a fleet total
    assert hmac["v2_accepted"] >= 1
    assert hmac["observation"]["active"] is False           # seeded inactive
