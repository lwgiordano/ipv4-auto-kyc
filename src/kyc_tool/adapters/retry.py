"""Transient-vs-permanent classification for adapter HTTP calls (ROADMAP PR 10 / 10a).

The JOB layer already retries with saturating backoff — but it retries the whole run transition and
cannot tell a flaky upstream from a permanently-wrong request. This helper owns the IN-ADAPTER
distinction: connect/read/timeout errors, 429 and 5xx are TRANSIENT (bounded immediate retries,
honoring a sanity-capped `Retry-After`); any other 4xx is PERMANENT and is returned on the first
attempt so the caller's `raise_for_status()` fails fast instead of hammering a request that can never
succeed. Exhausted transient retries surface the last response/error to the caller unchanged, so the
job-layer backoff still owns the long-horizon schedule.
"""

import time

import httpx

_TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_RETRY_AFTER_SECONDS = 30.0  # a hostile/buggy header must not stall a worker for hours


def _retry_delay(response: httpx.Response | None, attempt: int, backoff_seconds: float) -> float:
    if response is not None:
        header = response.headers.get("Retry-After", "")
        try:
            return min(max(float(header), 0.0), _MAX_RETRY_AFTER_SECONDS)
        except ValueError:
            pass  # absent or HTTP-date form — fall through to exponential
    return backoff_seconds * (2**attempt)


def get_with_retry(
    client: httpx.Client,
    url: str,
    *,
    params: dict | None = None,
    attempts: int = 3,
    backoff_seconds: float = 0.5,
    sleep=time.sleep,
) -> httpx.Response:
    """GET with bounded transient retries. Returns the final response (transient-exhausted included —
    the caller's raise_for_status() reports it); re-raises the final transport error. A permanent 4xx
    returns on the FIRST attempt with zero sleeps."""
    last_exc: httpx.TransportError | None = None
    response: httpx.Response | None = None
    for attempt in range(attempts):
        try:
            response = client.get(url, params=params)
            last_exc = None
        except httpx.TransportError as exc:  # connect/read/pool timeouts, DNS, resets
            last_exc = exc
            response = None
        if response is not None and response.status_code not in _TRANSIENT_STATUSES:
            return response  # success OR permanent 4xx — either way, no retry
        if attempt + 1 < attempts:
            sleep(_retry_delay(response, attempt, backoff_seconds))
    if last_exc is not None:
        raise last_exc
    return response  # transient-exhausted: caller's raise_for_status() surfaces it
