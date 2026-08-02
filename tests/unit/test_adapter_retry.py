"""PR 10a: in-adapter transient classification (adapters/retry.py). Transient (timeouts, 429, 5xx)
retries bounded with a sanity-capped Retry-After; permanent 4xx fails FAST (zero retries) so the job
layer never hammers a request that cannot succeed; exhaustion surfaces the last response/error."""

import httpx
import pytest

from kyc_tool.adapters.retry import _MAX_RETRY_AFTER_SECONDS, get_with_retry


def _client(responder):
    return httpx.Client(transport=httpx.MockTransport(responder), base_url="https://u.test")


def test_transient_5xx_retries_then_succeeds():
    calls, sleeps = [], []
    def responder(request):
        calls.append(1)
        return httpx.Response(500 if len(calls) < 3 else 200, json={"ok": True})
    r = get_with_retry(_client(responder), "/x", sleep=sleeps.append)
    assert r.status_code == 200 and len(calls) == 3 and len(sleeps) == 2


def test_permanent_4xx_returns_first_attempt_with_zero_sleeps():
    calls, sleeps = [], []
    def responder(request):
        calls.append(1)
        return httpx.Response(400)
    r = get_with_retry(_client(responder), "/x", sleep=sleeps.append)
    assert r.status_code == 400 and len(calls) == 1 and sleeps == []


def test_retry_after_is_honored_and_capped():
    sleeps = []
    def responder(request):
        return httpx.Response(429, headers={"Retry-After": "2"})
    r = get_with_retry(_client(responder), "/x", attempts=2, sleep=sleeps.append)
    assert r.status_code == 429 and sleeps == [2.0]

    sleeps.clear()
    def hostile(request):
        return httpx.Response(429, headers={"Retry-After": "99999"})
    get_with_retry(_client(hostile), "/x", attempts=2, sleep=sleeps.append)
    assert sleeps == [_MAX_RETRY_AFTER_SECONDS]  # a buggy/hostile header cannot stall the worker


def test_transport_errors_retry_then_reraise():
    calls, sleeps = [], []
    def responder(request):
        calls.append(1)
        raise httpx.ConnectTimeout("boom")
    with pytest.raises(httpx.ConnectTimeout):
        get_with_retry(_client(responder), "/x", attempts=3, sleep=sleeps.append)
    assert len(calls) == 3 and len(sleeps) == 2


def test_exhausted_transient_returns_last_response_for_raise_for_status():
    def responder(request):
        return httpx.Response(503)
    r = get_with_retry(_client(responder), "/x", attempts=2, sleep=lambda s: None)
    assert r.status_code == 503
    with pytest.raises(httpx.HTTPStatusError):
        r.raise_for_status()  # the caller's existing raise_for_status stays the failure path


def test_success_first_try_never_sleeps():
    sleeps = []
    r = get_with_retry(_client(lambda req: httpx.Response(200)), "/x", sleep=sleeps.append)
    assert r.status_code == 200 and sleeps == []


def test_companies_house_and_gleif_route_through_the_helper():
    """The two httpx-backed adapters consume the shared helper (not bare client.get), so a transient
    upstream blip retries in-adapter instead of failing the whole run transition."""
    import inspect

    from kyc_tool.adapters import companies_house, gleif

    assert "get_with_retry" in inspect.getsource(companies_house.CompaniesHouseAdapter.run)
    assert "get_with_retry" in inspect.getsource(gleif.GleifAdapter.run)
