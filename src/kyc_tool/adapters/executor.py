"""Supervised external-call executor (PR 10b slice 1 — the unconditionally-killable wall-clock
boundary reserved by the `750630c..ca85355` re-audit).

The in-process governed transport proves the absolute deadline after headers, per body chunk, and
at EOF — but between individual bytes only inactivity phase timeouts apply, so a hostile drip can
OCCUPY a worker past the deadline even though nothing it produces is ever returned. When
`settings.adapter_hard_kill_boundary` is on and the client is process-portable (a real network
transport), the whole physical fetch runs in a FORK-PER-CALL child the parent TERMINATES at the
absolute deadline: occupancy itself becomes bounded, unconditionally.

Contract details:
- The child performs ONLY wire + containment (`_contained_get` under a child-local budget carrying
  the deadline + byte cap). Rate permits and the DB liveness proof already ran PARENT-side in
  `_authorize_send` — the fork must never touch inherited DB connections.
- The child exits via `os._exit` so inherited finalizers (DB sockets, pools) never run in the
  fork; a terminated child is killed by signal, which skips them likewise.
- Results/typed errors relay over a pipe; the parent reconstructs the buffered `httpx.Response`
  or re-raises the governed exception type.
- MockTransport/fixture clients are NOT portable across a process boundary — callers must route
  them through the in-process path (get_with_retry does this automatically)."""

import contextlib
import multiprocessing
import os
import time

import httpx

from kyc_tool.authority import BudgetExhausted, RetryBudget, budget_scope

# Parent grace beyond the child's own in-process deadline before SIGTERM: the child's chunk/EOF
# checks abort cooperatively at `remaining`; the kill is the backstop for a child STUCK where no
# cooperative check can run (mid-connect, mid-header-byte, a wedged resolver).
_KILL_MARGIN_SECONDS = 1.0


def process_portable(client: httpx.Client) -> bool:
    """True when the client's transport is a real network transport a fresh child process can
    reproduce from configuration (base_url/headers/auth/timeout). MockTransport and other in-memory
    test doubles are not — they stay on the in-process path."""
    transport = getattr(client, "_transport", None)
    return isinstance(transport, httpx.HTTPTransport)


def _child_fetch(config: dict, url: str, params: dict | None, remaining: float,
                 cap: int | None, conn) -> None:  # pragma: no cover — runs in the fork
    try:
        from kyc_tool.adapters import retry

        child_client = httpx.Client(
            base_url=config["base_url"],
            headers=config["headers"],
            auth=config["auth"],
            timeout=config["timeout"],
        )
        budget = RetryBudget(
            deadline_monotonic=time.monotonic() + remaining, max_response_bytes=cap
        )
        with budget_scope(budget):
            response = retry._contained_get(
                child_client, url, params, budget, {"timeout": config["timeout"]}
            )
        conn.send(("ok", response.status_code, list(response.headers.multi_items()),
                   response.content))
    except BaseException as exc:  # noqa: BLE001 — the TYPE is the payload; parent re-raises it
        with contextlib.suppress(Exception):  # parent may already have killed the pipe
            conn.send(("err", type(exc).__name__, str(exc)))
    finally:
        try:
            conn.close()
        finally:
            os._exit(0)  # NEVER run inherited finalizers (DB sockets, pools) inside the fork


def _rebuild_error(name: str, message: str) -> Exception:
    from kyc_tool.adapters.retry import UpstreamResponseTooLarge

    if name == "BudgetExhausted":
        return BudgetExhausted(message)
    if name == "UpstreamResponseTooLarge":
        return UpstreamResponseTooLarge(message)
    if name == "DecodingError":
        return httpx.DecodingError(message, request=None)
    candidate = getattr(httpx, name, None)
    if isinstance(candidate, type) and issubclass(candidate, httpx.TransportError):
        try:
            return candidate(message)
        except Exception:  # noqa: BLE001 — constructor drift: degrade to the family root
            return httpx.TransportError(message)
    return RuntimeError(f"[supervised child] {name}: {message}")


def supervised_fetch(
    client: httpx.Client,
    url: str,
    params: dict | None,
    budget: RetryBudget,
    phase_timeout,
) -> httpx.Response:
    """One governed fetch under the hard-kill boundary. Raises BudgetExhausted when the child had
    to be terminated at the deadline — the run records UPSTREAM_ERROR (partial, G12), identical to
    every other spent-budget outcome."""
    remaining = budget.deadline_monotonic - budget.clock()
    if remaining <= 0:
        raise BudgetExhausted("plan budget exhausted before the supervised fetch")
    config = {
        "base_url": str(client.base_url),
        "headers": list(client.headers.multi_items()),
        "auth": client.auth,  # inherited via fork memory — BasicAuth etc., never serialized
        "timeout": phase_timeout if phase_timeout is not None else client.timeout,
    }
    ctx = multiprocessing.get_context("fork")
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    child = ctx.Process(
        target=_child_fetch,
        args=(config, url, params, remaining, budget.max_response_bytes, child_conn),
        daemon=True,
    )
    child.start()
    child_conn.close()
    try:
        if not parent_conn.poll(remaining + _KILL_MARGIN_SECONDS):
            child.terminate()
            child.join(2)
            if child.is_alive():
                child.kill()
                child.join(2)
            raise BudgetExhausted(
                f"supervised fetch HARD-KILLED {remaining + _KILL_MARGIN_SECONDS:.1f}s after "
                "start — the absolute deadline is now an occupancy bound, not only a result bound"
            )
        message = parent_conn.recv()
    finally:
        parent_conn.close()
        child.join(2)
    if message[0] == "ok":
        _, status, headers, body = message
        request = httpx.Request("GET", httpx.URL(str(client.base_url)).join(url))
        return httpx.Response(status, headers=headers, content=body, request=request)
    _, name, text = message
    raise _rebuild_error(name, text)
