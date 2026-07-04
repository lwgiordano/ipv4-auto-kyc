"""Per-upstream rate limiting (process-local token spacing).

Every adapter with a configured requests-per-second cap waits out the minimum
interval between calls. Simple by design: adapter fetches already run outside
transactions, so a sleep here holds no locks. Multi-process deployments
multiply the effective rate by worker count — set caps accordingly (runbook).
"""

import threading
import time


class RateLimiter:
    def __init__(self, rates_per_second: dict[str, float]) -> None:
        self._min_interval = {
            key: 1.0 / rate for key, rate in rates_per_second.items() if rate > 0
        }
        self._last_call: dict[str, float] = {}
        self._lock = threading.Lock()

    def acquire(self, key: str) -> None:
        interval = self._min_interval.get(key)
        if interval is None:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._last_call.get(key, 0.0) + interval - now
            reserve_at = max(now, self._last_call.get(key, 0.0) + interval)
            self._last_call[key] = reserve_at
        if wait > 0:
            time.sleep(wait)
