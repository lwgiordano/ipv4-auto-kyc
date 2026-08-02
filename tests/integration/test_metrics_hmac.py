"""/v1/metrics surfaces the DB-backed v1 witness plus a self-describing, PROCESS-LOCAL
auth-diagnostics block (PR 5a §6; re-audit `d569a15..4938840` F1 + `8aba2df..2cee937` R3-F2). The
v2/rejected counters are process-local, not DB-backed, and are exposed under `auth_diagnostics` (with
scope + process identity + zero-filled keys) so a legacy consumer cannot read them as fleet totals.
The counters are driven through the REAL signed/invalid request paths, not a private bump."""

import pytest

from kyc_tool.api import auth, hmac_witness
from tests.conftest import sign_headers_v2
from tests.integration.test_hmac_dual_accept import _body, _client

pytestmark = pytest.mark.postgres


def test_metrics_exposes_v1_witness_and_namespaced_process_local_diagnostics(
    client, session_factory, clean_db
):
    with session_factory() as s:
        hmac_witness.record_v1_accepted(s)  # DB-backed, fail-closed, aggregates across replicas
        s.commit()

    hmac = client.get("/v1/metrics").json()["hmac"]
    assert hmac["v1_accepted"] >= 1
    diag = hmac["auth_diagnostics"]
    assert diag["scope"] == "process_local"
    assert isinstance(diag["process_id"], int)
    assert diag["process_started_at"]                       # process start epoch present
    assert set(diag["counts"]) == {"v2_accepted", "rejected"}  # zero-filled, both keys always present
    assert "v2_accepted" not in hmac and "rejected" not in hmac  # NOT bare top-level (legacy misread)


def test_diagnostic_counters_advance_through_the_real_auth_paths(
    dual_accept_settings, session_factory, policy, clean_db
):
    """A REAL accepted-v2 request increments v2_accepted and a REAL invalid request increments
    rejected — driven through the app, not a private _bump(). Deleting the accepted-v2 bump in
    require_valid_signature leaves v2_accepted at 0 and fails this."""
    c = _client(dual_accept_settings, session_factory, policy)
    body = _body()
    before = auth.diagnostics_snapshot()["counts"]

    good = sign_headers_v2(body, method="POST", path_qs="/v1/cases/acme/events")
    assert c.post("/v1/cases/acme/events", content=body, headers=good).status_code == 202

    bad = dict(good)
    bad["X-KYC-Signature-V2"] = "deadbeef"  # valid v2 assertion shape, wrong signature ⇒ 401
    assert c.post("/v1/cases/acme/events", content=body, headers=bad).status_code == 401

    after = auth.diagnostics_snapshot()["counts"]
    assert after["v2_accepted"] >= before["v2_accepted"] + 1
    assert after["rejected"] >= before["rejected"] + 1
