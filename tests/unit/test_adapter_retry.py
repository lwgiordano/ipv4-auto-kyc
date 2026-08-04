"""PR 10a retry helper, hardened per re-audit `f2929f8..6a4cd87` F2/F5/F6: the classifier is the
written contract (408/429/ALL 5xx transient, other statuses permanent-fast), Retry-After parses
delta-seconds AND HTTP-dates finitely with a 30s clamp, inputs have governed domains, and the
pipeline's RetryBudget bounds every retry under the job lease with a per-attempt rate permit."""

import httpx
import pytest

from kyc_tool.adapters.retry import (
    _MAX_RETRY_AFTER_SECONDS,
    BudgetExhausted,
    RetryBudget,
    budget_scope,
    get_with_retry,
    is_transient,
)


def _client(responder):
    return httpx.Client(transport=httpx.MockTransport(responder), base_url="https://u.test")


# ── F5: the classifier is the WRITTEN contract ─────────────────────────────────────────────────────
@pytest.mark.parametrize("status", [408, 429, 500, 501, 503, 507, 520, 599])
def test_all_5xx_plus_408_429_are_transient(status):
    assert is_transient(status)
    calls = []
    def responder(request):
        calls.append(1)
        return httpx.Response(status)
    get_with_retry(_client(responder), "/x", attempts=2, sleep=lambda s: None)
    assert len(calls) == 2  # retried


@pytest.mark.parametrize("status", [200, 302, 400, 401, 404, 410])
def test_non_transient_statuses_return_first_attempt(status):
    calls, sleeps = [], []
    def responder(request):
        calls.append(1)
        return httpx.Response(status)
    r = get_with_retry(_client(responder), "/x", sleep=sleeps.append)
    assert r.status_code == status and len(calls) == 1 and sleeps == []


# ── F5: Retry-After parsing — delta, HTTP-date, garbage, clamp ────────────────────────────────────
def test_retry_after_delta_honored_and_capped():
    sleeps = []
    get_with_retry(_client(lambda r: httpx.Response(429, headers={"Retry-After": "2"})),
                   "/x", attempts=2, sleep=sleeps.append)
    assert sleeps == [2.0]
    sleeps.clear()
    get_with_retry(_client(lambda r: httpx.Response(429, headers={"Retry-After": "99999"})),
                   "/x", attempts=2, sleep=sleeps.append)
    assert sleeps == [_MAX_RETRY_AFTER_SECONDS]


def test_retry_after_http_date_parsed_against_injected_clock():
    sleeps = []
    # header 10s ahead of the injected wall clock
    get_with_retry(
        _client(lambda r: httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:10 GMT"})),
        "/x", attempts=2, sleep=sleeps.append, now=lambda: 1445412480.0,  # 07:28:00 GMT
    )
    assert sleeps == [10.0]


@pytest.mark.parametrize("header", ["nan", "-5", "inf", "garbage", "Wed, 21 Oct 2015 07:00:00 GMT"])
def test_bad_or_past_retry_after_falls_back_finite_nonnegative(header):
    sleeps = []
    get_with_retry(
        _client(lambda r: httpx.Response(429, headers={"Retry-After": header})),
        "/x", attempts=2, backoff_seconds=0.5, sleep=sleeps.append, now=lambda: 1445412480.0,
    )
    assert len(sleeps) == 1 and 0 <= sleeps[0] <= _MAX_RETRY_AFTER_SECONDS


# ── F5: governed input domains ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("attempts", [0, -1, True])
def test_nonpositive_or_bool_attempts_refused(attempts):
    with pytest.raises(ValueError):
        get_with_retry(_client(lambda r: httpx.Response(200)), "/x", attempts=attempts)


@pytest.mark.parametrize("backoff", [-1, float("nan"), float("inf")])
def test_bad_backoff_refused(backoff):
    with pytest.raises(ValueError):
        get_with_retry(_client(lambda r: httpx.Response(200)), "/x", backoff_seconds=backoff)


# ── transport + exhaustion behavior (unchanged contract) ──────────────────────────────────────────
def test_transport_errors_retry_then_reraise():
    calls = []
    def responder(request):
        calls.append(1)
        raise httpx.ConnectTimeout("boom")
    with pytest.raises(httpx.ConnectTimeout):
        get_with_retry(_client(responder), "/x", attempts=3, sleep=lambda s: None)
    assert len(calls) == 3


def test_exhausted_transient_returns_last_response_for_raise_for_status():
    r = get_with_retry(_client(lambda req: httpx.Response(503)), "/x", attempts=2, sleep=lambda s: None)
    assert r.status_code == 503
    with pytest.raises(httpx.HTTPStatusError):
        r.raise_for_status()


# ── F2: the RetryBudget bounds sleeps under the lease and re-acquires the rate permit ─────────────
def test_budget_stops_retries_that_would_cross_the_deadline():
    calls, sleeps = [], []
    def responder(request):
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "30"})
    clock = {"t": 100.0}
    budget = RetryBudget(deadline_monotonic=110.0, acquire=None, clock=lambda: clock["t"])
    with budget_scope(budget):
        r = get_with_retry(_client(responder), "/x", attempts=3, sleep=sleeps.append)
    # 100 + 30 >= 110 ⇒ the retry cannot fit: ONE call, ZERO sleeps, last response surfaced
    assert r.status_code == 429 and len(calls) == 1 and sleeps == []


def test_budget_acquires_one_rate_permit_per_physical_attempt():
    calls, permits = [], []
    def responder(request):
        calls.append(1)
        return httpx.Response(503)
    clock = {"t": 0.0}
    budget = RetryBudget(
        deadline_monotonic=10_000.0, acquire=lambda: permits.append(1), clock=lambda: clock["t"]
    )
    with budget_scope(budget):
        get_with_retry(_client(responder), "/x", attempts=3, sleep=lambda s: None)
    # R9-F5: EVERY send — the first included — holds its own permit; no outer pipeline permit
    assert len(calls) == 3 and len(permits) == 3


# ── R8-F5: the budget covers the FIRST attempt, the permit wait, and the wire itself ──────────────
def test_budget_dead_before_first_attempt_sends_nothing_and_raises():
    """Zero sends past the deadline includes the FIRST send: a budget already exhausted (e.g. the
    first rate permit consumed it) must raise BudgetExhausted with zero wire calls, not send once."""
    calls = []
    def responder(request):
        calls.append(1)
        return httpx.Response(200)
    budget = RetryBudget(deadline_monotonic=10.0, acquire=None, clock=lambda: 10.0)
    with budget_scope(budget), pytest.raises(BudgetExhausted):
        get_with_retry(_client(responder), "/x", attempts=3, sleep=lambda s: None)
    assert calls == []


def test_permit_wait_that_consumes_the_budget_stops_the_retry():
    """The audit's executable witness: deadline=10, the RETRY permit advances the clock 0 → 20.
    Previously the second wire call still happened at t=20; now the post-permit deadline proof
    stops it — wire calls happen at [0.0] only and the last outcome is surfaced."""
    call_times = []
    clock = {"t": 0.0}
    permits = {"n": 0}
    def responder(request):
        call_times.append(clock["t"])
        return httpx.Response(503)
    def slow_permit():
        permits["n"] += 1
        if permits["n"] > 1:  # the FIRST permit is instant; the retry permit waits past everything
            clock["t"] = 20.0
    budget = RetryBudget(deadline_monotonic=10.0, acquire=slow_permit, clock=lambda: clock["t"])
    with budget_scope(budget):
        r = get_with_retry(_client(responder), "/x", attempts=3, backoff_seconds=0,
                           sleep=lambda s: None)
    assert call_times == [0.0]  # ZERO sends after the deadline
    assert r.status_code == 503  # last real outcome surfaced (UPSTREAM_ERROR → partial, G12)


def test_deadline_aware_permit_refusal_surfaces_the_last_outcome():
    """budget.acquire raising BudgetExhausted (the deadline-aware rate limiter) on a RETRY stops
    the loop without a send and without masking the prior response; a refusal before the FIRST
    send propagates (there is no outcome to surface)."""
    calls = []
    permits = {"n": 0}
    def responder(request):
        calls.append(1)
        return httpx.Response(503)
    def refusing_permit():
        permits["n"] += 1
        if permits["n"] > 1:
            raise BudgetExhausted("permit cannot fit")
    budget = RetryBudget(deadline_monotonic=1e9, acquire=refusing_permit, clock=lambda: 0.0)
    with budget_scope(budget):
        r = get_with_retry(_client(responder), "/x", attempts=3, backoff_seconds=0,
                           sleep=lambda s: None)
    assert len(calls) == 1 and r.status_code == 503

    always_refuse = RetryBudget(
        deadline_monotonic=1e9,
        acquire=lambda: (_ for _ in ()).throw(BudgetExhausted("no permit")),
        clock=lambda: 0.0,
    )
    calls.clear()
    with budget_scope(always_refuse), pytest.raises(BudgetExhausted):
        get_with_retry(_client(responder), "/x", attempts=3, sleep=lambda s: None)
    assert calls == []  # refused before the FIRST send: zero wire calls


def test_per_attempt_timeout_is_capped_at_remaining_budget_tighten_only():
    """Each wire call carries an HTTP timeout derived from the remaining budget — capped DOWN to
    the remaining time, but never loosening the client's own smaller configured phase timeout."""
    seen = []
    def responder(request):
        seen.append(request.extensions["timeout"])
        return httpx.Response(200)
    client = httpx.Client(transport=httpx.MockTransport(responder), base_url="https://u.test",
                          timeout=httpx.Timeout(5.0))
    budget = RetryBudget(deadline_monotonic=100.0, acquire=None, clock=lambda: 98.0)  # 2s remain
    with budget_scope(budget):
        get_with_retry(client, "/x", attempts=1)
    assert seen[0]["read"] == 2.0 and seen[0]["connect"] == 2.0  # tightened to the budget
    seen.clear()
    budget = RetryBudget(deadline_monotonic=100.0, acquire=None, clock=lambda: 0.0)  # 100s remain
    with budget_scope(budget):
        get_with_retry(client, "/x", attempts=1)
    assert seen[0]["read"] == 5.0  # the client's own 5s stands — a big budget never loosens it


# ── R9-F5: a permit wait that loses the claim yields NO second call ───────────────────────────────
def test_permit_that_loses_the_claim_sends_no_further_call():
    """The audit's witness: first call 503; while waiting for the retry permit the heartbeat marks
    the claim lost. The post-permit liveness proof must refuse the second send and propagate the
    revocation (StaleJobClaim) — never record a retry outcome under a lost claim. Removing the
    post-permit proof makes calls == 2 and this test fail."""
    from kyc_tool.queue import jobs

    calls = []
    def responder(request):
        calls.append(1)
        return httpx.Response(503)
    job = jobs.ClaimedJob(id=7, kind="k", case_id=None, payload={}, attempts=1, max_attempts=5,
                          claim_nonce="w:n")
    ctx = jobs.ClaimContext(job=job)
    permits = {"n": 0}
    def losing_permit():  # the RETRY permit's wait outlives the claim: heartbeat sets lost mid-wait
        permits["n"] += 1
        if permits["n"] > 1:
            ctx.lost.set()
    budget = RetryBudget(
        deadline_monotonic=1e9,
        acquire=losing_permit,
        clock=lambda: 0.0,
        prove_live=jobs.check_claim_live,
    )
    with jobs.claim_scope(ctx), budget_scope(budget), pytest.raises(jobs.StaleJobClaim):
        get_with_retry(_client(responder), "/x", attempts=3, backoff_seconds=0,
                       sleep=lambda s: None)
    assert len(calls) == 1  # the revoked claim authorized NO second upstream call


# ── R10-F2: a response COMPLETING past the deadline is refused, empty bodies included ─────────────
def test_slow_empty_body_completing_past_the_deadline_is_refused():
    """The audit's witness: headers dripped past a 0.2s budget then Content-Length: 0 — zero body
    chunks meant zero deadline checks, and a 200 was returned at 2.17s. The post-EOF proof must
    refuse a result that completed after the deadline even with NO body bytes at all."""
    clocks = iter([0.0, 0.0, 5.0])  # authorize, post-header, post-EOF — time died mid-response
    last = {"v": 0.0}

    def clock():
        last["v"] = next(clocks, last["v"])
        return last["v"]

    budget = RetryBudget(deadline_monotonic=1.0, clock=clock)
    with budget_scope(budget), pytest.raises(BudgetExhausted):
        get_with_retry(_client(lambda r: httpx.Response(200, content=b"")), "/x", attempts=1)


# ── R10-F4: an unprovable claim is an authority failure, never upstream evidence ──────────────────
def test_prove_live_db_error_fails_closed_with_zero_wire_calls():
    """The proof erroring (DB down) must surface as StaleJobClaim — fail closed — not leak through
    as a generic exception the pipeline would record as AdapterStatus.UPSTREAM_ERROR."""
    from kyc_tool.queue import jobs

    calls = []

    def broken_factory():
        raise ConnectionError("claim DB unreachable")

    job = jobs.ClaimedJob(id=9, kind="k", case_id=None, payload={}, attempts=1, max_attempts=5,
                          claim_nonce="w:n")
    ctx = jobs.ClaimContext(job=job)
    budget = RetryBudget(
        deadline_monotonic=1e9,
        clock=lambda: 0.0,
        prove_live=lambda: jobs.prove_live_for_send(broken_factory),
    )
    def responder(request):
        calls.append(1)
        return httpx.Response(200)
    with jobs.claim_scope(ctx), budget_scope(budget), pytest.raises(jobs.StaleJobClaim) as exc:
        get_with_retry(_client(responder), "/x", attempts=3)
    assert calls == []  # ZERO wire calls under an unprovable claim
    assert ctx.lost.is_set()
    assert "UNAVAILABLE" in str(exc.value)


# ── R9-F3: every adapter wire call must go through the governed helper ────────────────────────────
def test_no_adapter_calls_httpx_outside_the_governed_helper():
    """Static transport guard: a direct `client.get/post/...`, a raw boto3 import, or an
    ungoverned `store.get(` in an adapter bypasses the budget, liveness proof, absolute deadline,
    and byte containment (the RIR path and the document-OCR store read did exactly this).
    Adding one anywhere under adapters/ fails here with a pointer to the governed surface."""
    import re
    from pathlib import Path

    adapters_dir = Path(__file__).resolve().parents[2] / "src" / "kyc_tool" / "adapters"
    banned = [
        # HTTP verbs on any client object → use retry.get_with_retry
        re.compile(r"\bclient\.(get|post|put|patch|delete|request|stream|send)\("),
        # raw AWS SDK in an adapter → storage belongs behind ObjectStore's governed surface
        re.compile(r"^\s*(import boto3|from boto3)"),
        # unbounded object read → use store.get_bounded(ref, max_bytes=governed_response_cap())
        re.compile(r"\bstore\.get\(|\bobject_store\.get\("),
    ]
    offenders = []
    for path in sorted(adapters_dir.rglob("*.py")):
        if path.name == "retry.py":  # the governed transport itself
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if any(pattern.search(line) for pattern in banned):
                offenders.append(f"{path.relative_to(adapters_dir)}:{lineno}: {line.strip()}")
    assert offenders == [], (
        "ungoverned external I/O under adapters/ (route it through retry.get_with_retry / "
        "retry.authorize_external_io + store.get_bounded so the budget/liveness/deadline/byte "
        "containment apply): " + "; ".join(offenders)
    )
