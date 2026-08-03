"""Per-upstream rate limiting (process-local token spacing).

Every adapter with a configured requests-per-second cap waits out the minimum
interval between calls. Simple by design: adapter fetches already run outside
transactions, so a sleep here holds no locks. Multi-process deployments
multiply the effective rate by worker count — set caps accordingly (runbook).
"""

import threading
import time

from kyc_tool.adapters.retry import BudgetExhausted
from kyc_tool.config import adapter_rate_violations


class RateLimiter:
    def __init__(self, rates_per_second: dict[str, float]) -> None:
        # Consumer-layer guard (re-audit `03dbfab..bc325e7` R5-F6): a NaN/negative/Infinite/tiny rate
        # removes the cap or makes an effectively infinite sleep. Refuse a bad mapping here too, so a
        # direct constructor (tests, other callers) cannot install one that the field validator would
        # have caught.
        problems = adapter_rate_violations(rates_per_second)
        if problems:
            raise ValueError("invalid adapter rate limits: " + "; ".join(problems))
        self._min_interval = {key: 1.0 / rate for key, rate in rates_per_second.items() if rate > 0}
        self._last_call: dict[str, float] = {}
        self._lock = threading.Lock()

    def acquire(self, key: str, *, deadline_monotonic: float | None = None) -> None:
        """Wait out the per-upstream spacing. Deadline-aware form (re-audit `3db5f13..a7df17b` F5):
        the RESERVED SEND TIME must fit strictly inside the deadline, else refuse with
        BudgetExhausted BEFORE reserving the slot or sleeping — a permit the claim cannot use must
        not be consumed (at 0.001 req/s the wait is ~1000s, far past any job lease), and the slot
        stays free for a claimant that can still send."""
        interval = self._min_interval.get(key)
        if interval is None:
            return
        with self._lock:
            now = time.monotonic()
            reserve_at = max(now, self._last_call.get(key, 0.0) + interval)
            if deadline_monotonic is not None and reserve_at >= deadline_monotonic:
                raise BudgetExhausted(
                    f"rate permit for {key!r} would land at +{reserve_at - now:.1f}s, "
                    "past the plan deadline"
                )
            self._last_call[key] = reserve_at
            wait = reserve_at - now
        if wait > 0:
            time.sleep(wait)
