"""The ambient EXTERNAL-CALL AUTHORITY (PR 10b slice 1 — the injected-gateway unification the
`750630c..ca85355` re-audit reserved here).

One capability object (`RetryBudget`) carries every bound an external side effect must prove:
the absolute monotonic deadline, the per-upstream rate permit, the DB claim-liveness re-proof,
the response byte cap, and the hard-kill election. It travels by contextvar (`budget_scope`) so
call sites do not change signatures — and this module is DEPENDENCY-NEUTRAL (stdlib only), so
EVERY layer that performs external I/O can prove through it without import knots:

- HTTP: `adapters/retry.py` (which re-exports this module's names for compatibility);
- object storage: `storage/object_store.get_bounded` proves INTERNALLY — a caller that never
  heard of the authority still cannot read bytes under a lost claim or a spent budget;
- provider protocols: `governed_delegate()` wraps any delegate callable in the same proof.

The rule set is the audited one: proofs run immediately before the physical effect (permit →
liveness → deadline); `remaining <= 0` is SPENT (exact equality included, single clock read);
a revoked claim propagates StaleJobClaim and is never converted into upstream evidence."""

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


class BudgetExhausted(RuntimeError):
    """The plan budget cannot fit another wait/send (deadline-aware permit refusal, zero remaining
    time, a streamed body crossing the absolute deadline, or a supervised hard kill). The adapter
    records UPSTREAM_ERROR and the run completes PARTIAL — never a send past the deadline, never
    a sleep that outlives the job claim."""


@dataclass(frozen=True)
class RetryBudget:
    """The per-plan external-call authority: a monotonic deadline (strictly below the job lease
    minus the DB margin), the per-upstream rate acquire, the per-send claim-liveness proof, the
    response containment cap, and the hard-kill election — applied around EVERY physical side
    effect, the first included."""

    deadline_monotonic: float
    acquire: object | None = None  # zero-arg callable: the deadline-aware upstream rate permit
    clock: object = time.monotonic
    prove_live: object | None = None  # zero-arg callable: DB re-proof of the ambient claim
    max_response_bytes: int | None = None  # wire AND decoded cap for every governed payload
    # When True (settings.adapter_hard_kill_boundary), governed HTTP fetches on process-portable
    # clients run in a fork-per-call subprocess the parent TERMINATES at the absolute deadline —
    # the unconditionally-killable occupancy bound the in-process checks cannot give.
    hard_kill: bool = False


_BUDGET: ContextVar[RetryBudget | None] = ContextVar("adapter_retry_budget", default=None)


@contextmanager
def budget_scope(budget: RetryBudget):
    token = _BUDGET.set(budget)
    try:
        yield
    finally:
        _BUDGET.reset(token)


def current_budget() -> RetryBudget | None:
    return _BUDGET.get()


def remaining_or_spent(budget: RetryBudget, what: str) -> float:
    """THE ONE deadline rule (re-audit `ddbff39..c3884bd` F3): remaining = deadline - clock, and
    remaining <= 0 — exact equality included — is SPENT. Every boundary (pre-send, post-header,
    mid-body, post-EOF, external I/O) proves through this helper with a single clock read, so no
    site can drift back to a `>` comparison."""
    remaining = budget.deadline_monotonic - budget.clock()
    if remaining <= 0:
        raise BudgetExhausted(f"plan budget exhausted at {what} — refusing to proceed")
    return remaining


def authorize_external_io() -> float | None:
    """Send-authority for NON-HTTP external I/O (re-audit `750630c..ca85355` F7: object-store
    reads, OCR input fetches, provider-protocol delegates sat entirely outside the governed
    helper). Same order as the HTTP send authority minus the rate permit: DB claim liveness
    re-proof, then the remaining-deadline proof. A lost/unprovable claim or a spent budget places
    ZERO external calls. No ambient budget = no-op (direct/unit callers). Returns remaining
    seconds (None ⇒ ungoverned)."""
    budget = _BUDGET.get()
    if budget is None:
        return None
    if budget.prove_live is not None:
        budget.prove_live()  # StaleJobClaim propagates — revocation is never an upstream error
    return remaining_or_spent(budget, "the external I/O call")


def governed_response_cap() -> int | None:
    """The ambient byte cap for external payloads (object-store documents share the same
    containment domain as HTTP bodies). None ⇒ ungoverned/direct caller."""
    budget = _BUDGET.get()
    return budget.max_response_bytes if budget is not None else None


def governed_delegate(fn, *args, **kwargs):
    """Run a provider-protocol delegate (e.g. FloqerClient.enrich) as a governed external call:
    prove the ambient authority immediately before invoking it. The delegate's own wire calls
    must additionally route through the governed HTTP helper when the real client lands."""
    authorize_external_io()
    return fn(*args, **kwargs)
