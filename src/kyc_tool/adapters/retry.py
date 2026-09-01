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

BUDGET (F2/`3db5f13..a7df17b` F5; unified into ONE send authority per re-audit `7d1c435..827bc0f`
F3/F5/F6): the pipeline sets a per-plan `RetryBudget` BEFORE the first permit, and EVERY physical
call — the first included — passes through `_authorize_send` immediately before the wire:
1. acquire the deadline-aware rate permit (a permit that cannot fit refuses, BudgetExhausted);
2. re-prove claim liveness against the DB (`prove_live`) — a permit wait may outlive the claim,
   and a revoked claim must not authorize another upstream call (StaleJobClaim propagates, it is
   NEVER converted into a retry or a partial);
3. re-prove remaining wall-clock time, in that order.
The wire call itself is CONTAINED (F3/F6): the body is STREAMED with the absolute plan deadline
checked on every chunk — the deadline is total elapsed time including rate wait, headers, and body
bytes, not an HTTPX phase field (phase timeouts are still applied, tighten-only, as the secondary
inactivity bound) — and both wire and DECODED bytes are capped (`max_response_bytes`): oversized
Content-Length refuses preflight; chunked overflow and gzip expansion fail closed mid-stream as
non-retryable `UpstreamResponseTooLarge`. `BudgetExhausted` surfaces the last outcome — the run
records UPSTREAM_ERROR and completes PARTIAL (G12); zero sends happen past the deadline. Without a
budget (direct unit calls), retries stay bounded by `attempts` alone and responses buffer as before.
"""

import math
import time
import zlib
from email.utils import parsedate_to_datetime

import httpx

# The ambient authority moved to the DEPENDENCY-NEUTRAL kyc_tool.authority module (PR 10b slice 1:
# the injected-gateway unification) so storage and provider layers prove through the SAME object
# without import knots. Re-exported here so every existing import keeps working; `noqa: F401` —
# these names ARE this module's public surface.
from kyc_tool.authority import (  # noqa: F401
    BudgetExhausted,
    RetryBudget,
    authorize_external_io,
    budget_scope,
    current_budget,
    governed_delegate,
    governed_response_cap,
)
from kyc_tool.authority import remaining_or_spent as _remaining_or_spent

_MAX_RETRY_AFTER_SECONDS = 30.0  # a hostile/buggy header must not stall a worker
_MAX_SHIFT = 10  # bounds the exponential fallback before clamping


class UpstreamResponseTooLarge(RuntimeError):
    """A governed response exceeded the containment cap (declared Content-Length, streamed wire
    bytes, or DECODED bytes — gzip expansion counts). NON-RETRYABLE: re-fetching an oversized body
    burns budget for the same outcome; the adapter records UPSTREAM_ERROR (partial, G12)."""


def _authorize_send(budget: RetryBudget | None) -> float | None:
    """THE send authority (re-audit `7d1c435..827bc0f` F5): invoked immediately before EVERY
    physical call. Order is load-bearing — permit first (it may wait), then the DB claim proof
    (the wait may have outlived the claim), then the remaining-deadline proof (the wait may have
    consumed the budget). Returns the remaining seconds (None ⇒ ungoverned direct call)."""
    if budget is None:
        return None
    if budget.acquire is not None:
        budget.acquire()  # BudgetExhausted when the permit cannot fit the deadline
    if budget.prove_live is not None:
        budget.prove_live()  # StaleJobClaim propagates: a revoked claim sends NOTHING further
    return _remaining_or_spent(budget, "the wire call")


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
    budget = current_budget()
    last_exc: httpx.TransportError | None = None
    response: httpx.Response | None = None
    for attempt in range(attempts):
        if attempt > 0:
            # Every RETRY is a NEW physical call: it must fit the plan deadline (sleeps must never
            # consume the job lease) before we even sleep for it — F2.
            delay = _retry_delay(response, attempt - 1, backoff_seconds, now)
            if budget is not None and budget.clock() + delay >= budget.deadline_monotonic:
                break  # surface the last outcome; the run records UPSTREAM_ERROR (partial, G12)
            sleep(delay)
        try:
            # ONE authority for every send, first attempt included: permit → liveness → deadline.
            # StaleJobClaim propagates out of here untouched — revocation is not a retry input.
            remaining = _authorize_send(budget)
        except BudgetExhausted:
            if response is None and last_exc is None:
                raise  # nothing to surface: the budget died before the FIRST attempt
            break
        send_kwargs = {}
        if remaining is not None:
            send_kwargs["timeout"] = _attempt_timeout(client, remaining)
        try:
            if budget is not None and budget.hard_kill:
                from kyc_tool.adapters import executor  # lazy: executor imports this module

                if executor.process_portable(client):
                    # the unconditionally-killable boundary (PR 10b slice 1): the whole physical
                    # fetch runs in a fork the parent TERMINATES at the absolute deadline
                    response = executor.supervised_fetch(
                        client, url, params, budget, send_kwargs.get("timeout")
                    )
                else:  # Mock/fixture transports cannot cross a process boundary
                    response = _contained_get(client, url, params, budget, send_kwargs)
            else:
                response = _contained_get(client, url, params, budget, send_kwargs)
            last_exc = None
        except httpx.TransportError as exc:  # connect/read/pool timeouts, DNS, resets
            last_exc = exc
            response = None
        if response is not None and not is_transient(response.status_code):
            return response  # success OR permanent — either way, no retry
    if last_exc is not None:
        raise last_exc
    return response  # transient-exhausted: caller's raise_for_status() surfaces it


def _contained_get(
    client: httpx.Client,
    url: str,
    params: dict | None,
    budget: RetryBudget | None,
    send_kwargs: dict,
) -> httpx.Response:
    """The governed wire call (re-audit `7d1c435..827bc0f` F3/F6; hardened per `750630c..ca85355`
    F2/F3). With a budget:
    - the absolute deadline is proved immediately AFTER the headers arrive and again AFTER EOF —
      a header drip or a slow empty-body 200 cannot become a successful result past the deadline
      (F2). HONEST BOUND: between individual header bytes only the tightened inactivity phase
      timeout applies (h11 additionally hard-caps header size), so worker OCCUPANCY during a
      hostile header drip is bounded but can exceed the deadline; nothing is RETURNED or
      persisted from it. The unconditionally-killable boundary (supervised executor) is PR 10b.
    - the body is read RAW and decoded INCREMENTALLY with a bounded decoder (F3): wire and
      decoded caps apply BEFORE allocation (zlib max_length), so a decompression bomb cannot
      spike memory before the cap fires. Accept-Encoding is pinned to gzip; multiple/unsupported
      encodings are rejected; truncated/corrupt streams raise DecodingError.
    Without a budget (direct unit calls) the buffered path is unchanged."""
    if budget is None:
        return client.get(url, params=params, **send_kwargs)
    cap = budget.max_response_bytes
    with client.stream(
        "GET", url, params=params, headers={"Accept-Encoding": "gzip"}, **send_kwargs
    ) as streamed:
        _remaining_or_spent(budget, "header completion")  # a header drip cannot smuggle a result
        declared = streamed.headers.get("Content-Length", "")
        if cap is not None and declared.isdigit() and int(declared) > cap:
            raise UpstreamResponseTooLarge(
                f"declared Content-Length {declared} exceeds the {cap}-byte governed cap"
            )
        encoding = streamed.headers.get("Content-Encoding", "").strip().lower()
        if encoding in ("", "identity"):
            decoder = None
        elif encoding in ("gzip", "x-gzip", "deflate"):
            decoder = zlib.decompressobj(wbits=47)  # auto gzip/zlib headers, bounded via max_length
        else:  # br/zstd/multiple encodings: never negotiated (Accept-Encoding: gzip) — refuse
            raise httpx.DecodingError(
                f"unsupported Content-Encoding {encoding!r} on the governed transport",
                request=streamed.request,
            )
        chunks: list[bytes] = []
        decoded_total = 0
        wire_total = 0
        # Eagerly-constructed responses (unit MockTransport handlers pass content=) have no raw
        # stream to iterate — their constructor bytes ARE the wire form. Real transports stream.
        if getattr(streamed, "is_stream_consumed", False):
            raw_source = iter((streamed.content,))
        else:
            raw_source = streamed.iter_raw()
        try:
            for raw_chunk in raw_source:
                wire_total += len(raw_chunk)
                if cap is not None and wire_total > cap:
                    raise UpstreamResponseTooLarge(
                        f"wire bytes exceeded the {cap}-byte governed cap; refusing the body"
                    )
                if decoder is None:
                    piece = raw_chunk
                else:
                    # decode at most (cap - decoded_total + 1) bytes: the bomb detonates into a
                    # bounded buffer, never into memory (R10-F3 — iter_bytes() allocated the whole
                    # decompressed chunk before any cap could look at it)
                    allowance = (cap - decoded_total + 1) if cap is not None else 0
                    piece = decoder.decompress(raw_chunk, allowance)
                    if decoder.unconsumed_tail:
                        raise UpstreamResponseTooLarge(
                            f"decoded stream exceeds the {cap}-byte governed cap "
                            f"(wire={wire_total}); refusing the body"
                        )
                decoded_total += len(piece)
                if cap is not None and decoded_total > cap:
                    raise UpstreamResponseTooLarge(
                        f"decoded bytes exceeded the {cap}-byte governed cap "
                        f"(wire={wire_total}); refusing the body"
                    )
                _remaining_or_spent(budget, "the mid-body chunk boundary")
                if piece:
                    chunks.append(piece)
            if decoder is not None:
                tail = (
                    decoder.flush(cap - decoded_total + 1) if cap is not None else decoder.flush()
                )
                decoded_total += len(tail)
                if cap is not None and decoded_total > cap:
                    raise UpstreamResponseTooLarge(
                        f"decoded bytes exceeded the {cap}-byte governed cap at flush "
                        f"(wire={wire_total}); refusing the body"
                    )
                if tail:
                    chunks.append(tail)
                if not decoder.eof:
                    raise httpx.DecodingError(
                        "truncated or corrupt encoded body on the governed transport",
                        request=streamed.request,
                    )
        except zlib.error as exc:
            raise httpx.DecodingError(
                f"corrupt encoded body on the governed transport: {exc}",
                request=streamed.request,
            ) from exc
        # A response that COMPLETED at/after the deadline must not become evidence (F2: the slow
        # empty-body 200 witness — zero body chunks meant zero deadline checks).
        _remaining_or_spent(budget, "response completion (EOF)")
        # Rebuild a buffered response for the caller. Content-Encoding/Length describe the WIRE
        # form; the chunks are already decoded, so those headers must not survive re-decoding.
        headers = [
            (k, v)
            for k, v in streamed.headers.multi_items()
            if k.lower() not in ("content-encoding", "content-length")
        ]
        return httpx.Response(
            streamed.status_code,
            headers=headers,
            content=b"".join(chunks),
            request=streamed.request,
        )


def _attempt_timeout(client: httpx.Client, remaining: float) -> httpx.Timeout | float:
    """Per-attempt HTTP phase timeouts = the client's own configured values capped at the remaining
    budget (TIGHTEN-only — a large budget never loosens a small configured timeout). SECONDARY to
    the absolute streamed deadline in _contained_get: phases bound inactivity, not total time."""
    base = getattr(client, "timeout", None)
    if base is None:
        return remaining

    def _cap(phase: float | None) -> float:
        return remaining if phase is None else min(phase, remaining)

    return httpx.Timeout(
        connect=_cap(base.connect), read=_cap(base.read), write=_cap(base.write), pool=_cap(base.pool)
    )
