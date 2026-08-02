"""One saturating exponential-backoff schedule (re-audit `5b0f0b8..b75a320` R4-F3).

Shared by the job queue (`jobs.fail`) and the outbox publisher so the two schedules cannot diverge,
and so neither can overflow PostgreSQL timestamp arithmetic: the shift is bounded BEFORE `2**shift`
is materialized (no `2**1024` Python big-int, no `OverflowError` converting it to float), the
pre-jitter delay is capped at `cap_seconds`, and the post-jitter delay is capped again.
"""

import random

# A single retry delay is capped here. `now() + make_interval(secs => MAX_BACKOFF_SECONDS)` is far
# below timestamptz overflow, and no operational retry needs to wait longer than this.
MAX_BACKOFF_SECONDS = 6 * 3600  # 6 hours
# 2**_MAX_SHIFT is a safe intermediate to materialize; the cap handles any larger nominal delay, so
# the exponent is clamped here before exponentiation rather than after.
_MAX_SHIFT = 40


def saturating_backoff_seconds(
    base_seconds,
    attempt: int,
    *,
    cap_seconds: float = MAX_BACKOFF_SECONDS,
    jitter_fraction: float = 0.0,
    rand=None,
):
    """`base × 2**(attempt-1)`, capped at `cap_seconds`. A nonpositive base yields 0 (retry-when-due).
    With `jitter_fraction > 0`, a uniform `[0, jitter_fraction × delay)` is added and the result is
    re-capped, so the returned delay never exceeds `cap_seconds`."""
    if base_seconds <= 0:
        return 0.0 if jitter_fraction else 0
    shift = min(max(attempt - 1, 0), _MAX_SHIFT)
    delay = min(base_seconds * (2**shift), cap_seconds)
    if jitter_fraction:
        rand = random.random if rand is None else rand
        delay = min(delay + rand() * delay * jitter_fraction, cap_seconds)
    return delay
