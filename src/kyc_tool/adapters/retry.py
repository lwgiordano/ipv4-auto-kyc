"""Transient-vs-permanent classification for adapter HTTP calls (PR 10a, hardened in the
`f2929f8..6a4cd87` fold: F2 lease/rate budget, F5 classifier semantics, F6 honest contract).

CONTRACT (F6): local retries are the ONLY retry this layer performs. When they exhaust, the pipeline
records `UPSTREAM_ERROR` and the run completes as PARTIAL (G12 semantics) — the job queue does NOT
re-drive an exhausted adapter; recovery is the platform re-sending the evidence event. The transient
window here is deliberately short; it absorbs blips, not outages.

CLASSIFIER (F5): transient = connect/read/pool transport errors, 408, 429, and EVERY 5xx. Any other
status returns on the first attempt (permanent — the job layer must not hammer it). `Retry-After` is
parsed as finite non-negative delta-seconds OR an IMF-fixdate against the injected wall clock;
invalid/negative/past values fall back to the bounded exponential; every delay is clamped to
[0, 30s].

BUDGET (F2, tightened per re-audit `3db5f13..a7df17b` F5): the pipeline sets a per-plan
`RetryBudget` (deadline strictly below the job lease minus a DB margin + the per-upstream rate
authority) BEFORE the first permit, so the budget covers every wait and every physical call:
- the FIRST attempt proves remaining time > 0 before its wire call (the first permit wait counts);
- every RETRY proves the delay fits, sleeps, re-acquires a deadline-aware permit, then RE-proves
  remaining time AFTER the permit (a rate wait may consume the rest of the budget) before sending;
- every wire call carries a per-attempt HTTP timeout capped at the remaining budget (tighten-only:
  the client's own configured phase timeouts still apply when smaller).
`BudgetExhausted` from a deadline-aware permit stops the loop and surfaces the last outcome — the
run records UPSTREAM_ERROR and completes PARTIAL (G12); zero sends happen past the deadline.
Without a budget (direct unit calls), retries stay bounded by `attempts` alone.
"""

import math
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

import httpx

_MAX_RETRY_AFTER_SECONDS = 30.0  # a hostile/buggy header must not stall a worker
_MAX_SHIFT = 10  # bounds the exponential fallback before clamping


@dataclass(frozen=True)
class RetryBudget:
    """The pipeline's per-plan authority: a monotonic deadline (strictly below the job lease minus
    the DB margin) and the per-upstream rate acquire, applied around EVERY physical retry attempt."""

    deadline_monotonic: float
    acquire: object | None = None  # zero-arg callable: the upstream rate permit
    clock: object = time.monotonic


_BUDGET: ContextVar[RetryBudget | None] = ContextVar("adapter_retry_budget", default=None)


class BudgetExhausted(RuntimeError):
    """The plan budget cannot fit another wait/send (deadline-aware permit refusal or zero remaining
    time). The adapter records UPSTREAM_ERROR and the run completes PARTIAL — never a send past the
    deadline, never a sleep that outlives the job claim."""


@contextmanager
def budget_scope(budget: RetryBudget):
    token = _BUDGET.set(budget)
    try:
        yield
    finally:
        _BUDGET.reset(token)


def current_budget() -> RetryBudget | None:
    return _BUDGET.get()


def is_transient(status_code: int) -> bool:
    """The ONE written predicate: 408 (request timeout), 429, and every 5xx."""
    return status_code in (408, 429) or 500 <= status_code <= 599


def _retry_delay(response, attempt: int, backoff_seconds: float, now) -> float:
    """Finite, non-negative, clamped delay. Header forms: delta-seconds or IMF-fixdate (relative to
    the injected wall clock); invalid/negative/past/non-finite values fall back to the bounded
    exponential."""
    fallback = min(backoff_seconds * (2 ** min(attempt, _MAX_SHIFT)), _MAX_RETRY_AFTER_SECONDS)
    if response is None:
        return fallback
    header = response.headers.get("Retry-After", "")
    if not header:
        return fallback
    try:
        delta = float(header)
        if not math.isfinite(delta) or delta < 0:
            return fallback
        return min(delta, _MAX_RETRY_AFTER_SECONDS)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return fallback
    delta = when.timestamp() - now()
    if not math.isfinite(delta) or delta < 0:
        return fallback  # past/garbage date: retry on the exponential, never negative
    return min(delta, _MAX_RETRY_AFTER_SECONDS)


def get_with_retry(
    client: httpx.Client,
    url: str,
    *,
    params: dict | None = None,
    attempts: int = 3,
    backoff_seconds: float = 0.5,
    sleep=time.sleep,
    now=time.time,
) -> httpx.Response:
    """GET with bounded transient retries under the ambient RetryBudget (if any). Returns the final
    response (transient-exhausted included — the caller's raise_for_status() reports it); re-raises
    the final transport error. A permanent status returns on the FIRST attempt with zero sleeps."""
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 1:
        raise ValueError(f"attempts must be a positive int, got {attempts!r}")
    bad_backoff = not isinstance(backoff_seconds, (int, float)) or not math.isfinite(backoff_seconds)
    if bad_backoff or backoff_seconds < 0:
        raise ValueError(f"backoff_seconds must be finite and >= 0, got {backoff_seconds!r}")
    budget = _BUDGET.get()
    last_exc: httpx.TransportError | None = None
    response: httpx.Response | None = None
    for attempt in range(attempts):
        if attempt > 0:
            # Every RETRY is a NEW physical call: it must fit the plan deadline (sleeps must never
            # consume the job lease) and hold its OWN rate permit (one permit ≠ three calls) — F2.
            delay = _retry_delay(response, attempt - 1, backoff_seconds, now)
            if budget is not None and budget.clock() + delay >= budget.deadline_monotonic:
                break  # surface the last outcome; the run records UPSTREAM_ERROR (partial, G12)
            sleep(delay)
            if budget is not None and budget.acquire is not None:
                try:
                    budget.acquire()
                except BudgetExhausted:
                    break  # the permit itself cannot fit — surface the last outcome, send nothing
        send_kwargs = {}
        if budget is not None:
            # F5: the permit wait (or the prior attempt) may have consumed the budget — prove
            # remaining time IMMEDIATELY before the wire call, and bound the call to it so a slow
            # connect/read cannot outlive the claim either.
            remaining = budget.deadline_monotonic - budget.clock()
            if remaining <= 0:
                if response is None and last_exc is None:
                    # nothing to surface: the budget died before the FIRST attempt
                    raise BudgetExhausted(f"plan budget exhausted before first attempt to {url}")
                break
            send_kwargs["timeout"] = _attempt_timeout(client, remaining)
        try:
            response = client.get(url, params=params, **send_kwargs)
            last_exc = None
        except httpx.TransportError as exc:  # connect/read/pool timeouts, DNS, resets
            last_exc = exc
            response = None
        if response is not None and not is_transient(response.status_code):
            return response  # success OR permanent — either way, no retry
    if last_exc is not None:
        raise last_exc
    return response  # transient-exhausted: caller's raise_for_status() surfaces it


def _attempt_timeout(client: httpx.Client, remaining: float) -> httpx.Timeout | float:
    """Per-attempt HTTP timeout = the client's own configured phase timeouts capped at the remaining
    budget (TIGHTEN-only — a large budget never loosens a small configured timeout)."""
    base = getattr(client, "timeout", None)
    if base is None:
        return remaining

    def _cap(phase: float | None) -> float:
        return remaining if phase is None else min(phase, remaining)

    return httpx.Timeout(
        connect=_cap(base.connect), read=_cap(base.read), write=_cap(base.write), pool=_cap(base.pool)
    )
