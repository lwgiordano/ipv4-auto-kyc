"""Re-audit `5b0f0b8..b75a320` R4-F3: the shared saturating backoff never overflows and always
returns a delay in [0, cap]. Covers the attempt counts Codex overflowed (63, 1025, int4-max) and the
base values (negative, zero, normal, oversized)."""

import pytest

from kyc_tool.queue.backoff import MAX_BACKOFF_SECONDS, saturating_backoff_seconds


@pytest.mark.parametrize("attempt", [1, 2, 63, 1025, 2_147_483_647])
def test_no_attempt_count_overflows_or_exceeds_cap(attempt):
    # attempt 63 overflowed timestamp arithmetic; 1025 raised Python OverflowError before this helper.
    delay = saturating_backoff_seconds(10, attempt, jitter_fraction=0.25)
    assert 0 <= delay <= MAX_BACKOFF_SECONDS


@pytest.mark.parametrize("base", [0, 5, 10, 2_592_000])
def test_bases_stay_within_cap(base):
    delay = saturating_backoff_seconds(base, 4, jitter_fraction=0.25)
    assert 0 <= delay <= MAX_BACKOFF_SECONDS
    if base == 0:
        assert delay == 0.0  # zero base ⇒ retry when due


def test_negative_base_never_returns_a_negative_delay():
    # jobs.fail refuses a negative base at the consumer layer; the helper itself still never
    # produces the negative delay that requeued a job immediately.
    assert saturating_backoff_seconds(-1, 5) == 0
    assert saturating_backoff_seconds(-1, 5, jitter_fraction=0.25) == 0.0


def test_schedule_grows_then_saturates_monotonically():
    seq = [saturating_backoff_seconds(10, a) for a in range(1, 20)]  # no jitter → deterministic
    assert seq[0] == 10 and seq[1] == 20 and seq[2] == 40
    assert seq[-1] == MAX_BACKOFF_SECONDS  # saturates at the cap
    assert all(b >= a for a, b in zip(seq, seq[1:], strict=False))  # monotonic non-decreasing
