from __future__ import annotations

import pytest

from pipelineos.hubspot.ratelimit import TokenBucket, backoff_delay, parse_retry_after
from tests.support import FakeClock, RecordingSleeper


def test_backoff_ceiling_doubles_and_caps() -> None:
    delays = [backoff_delay(a, rng=lambda: 1.0, base=0.5, cap=4.0) for a in range(5)]
    assert delays == [0.5, 1.0, 2.0, 4.0, 4.0]


def test_backoff_is_full_jitter() -> None:
    assert backoff_delay(3, rng=lambda: 0.25, base=1.0, cap=100.0) == 2.0
    assert backoff_delay(3, rng=lambda: 0.0) == 0.0


def test_backoff_rejects_negative_attempt() -> None:
    with pytest.raises(ValueError):
        backoff_delay(-1, rng=lambda: 1.0)


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ("3", 3.0),
        ("0.5", 0.5),
        ("soon", None),
        ("-1", None),
        ("3600", None),
        ("nan", None),
    ],
)
def test_parse_retry_after(header: str | None, expected: float | None) -> None:
    assert parse_retry_after(header) == expected


def test_bucket_allows_capacity_then_waits_for_one_token() -> None:
    clock = FakeClock()
    sleeper = RecordingSleeper(clock)
    bucket = TokenBucket(10, 10.0, clock=clock, sleeper=sleeper)
    for _ in range(10):
        bucket.acquire()
    assert sleeper.sleeps == []
    bucket.acquire()
    assert sleeper.sleeps == [pytest.approx(1.0)]


def test_bucket_refills_over_time() -> None:
    clock = FakeClock()
    sleeper = RecordingSleeper(clock)
    bucket = TokenBucket(2, 1.0, clock=clock, sleeper=sleeper)
    bucket.acquire()
    bucket.acquire()
    clock.now += 1.0
    bucket.acquire()
    bucket.acquire()
    assert sleeper.sleeps == []


def test_bucket_rejects_bad_config() -> None:
    clock = FakeClock()
    with pytest.raises(ValueError):
        TokenBucket(0, 1.0, clock=clock, sleeper=RecordingSleeper(clock))
