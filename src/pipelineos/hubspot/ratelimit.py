"""Client-side rate limiting and retry timing.

HubSpot's Free/Starter tier allows 100 requests per 10 s per app, and REST 429s carry no
documented ``Retry-After``. So the client paces itself with a token bucket set just under the
limit and, when it is throttled anyway, backs off with full jitter. Time is injected (a clock
and a sleeper) so tests can assert exact sleep sequences without sleeping.
"""

from __future__ import annotations

import math
from collections.abc import Callable

Clock = Callable[[], float]
Sleeper = Callable[[float], None]
Rng = Callable[[], float]

# HubSpot holds a record lock for 2 s after a 423; retrying sooner just collects another 423.
LOCKED_FLOOR_S = 2.0
# Any Retry-After longer than this is treated as garbage and replaced by our own backoff.
MAX_RETRY_AFTER_S = 60.0


def backoff_delay(attempt: int, *, rng: Rng, base: float = 0.5, cap: float = 30.0) -> float:
    """Full-jitter exponential backoff: uniform in [0, min(cap, base * 2**attempt)].

    ``rng`` returns a float in [0, 1). Passing ``lambda: 1.0`` gives the deterministic ceiling.
    """
    if attempt < 0:
        raise ValueError("attempt must be >= 0")
    ceiling = min(cap, math.ldexp(base, attempt))
    return ceiling * rng()


def parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header given in seconds; ``None`` if absent or not sensible."""
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    if math.isnan(seconds) or seconds < 0 or seconds > MAX_RETRY_AFTER_S:
        return None
    return seconds


class TokenBucket:
    """Continuously refilling bucket: ``capacity`` requests per ``interval_s`` seconds."""

    def __init__(self, capacity: int, interval_s: float, *, clock: Clock, sleeper: Sleeper) -> None:
        if capacity <= 0 or interval_s <= 0:
            raise ValueError("capacity and interval_s must be positive")
        self._capacity = float(capacity)
        self._rate = capacity / interval_s
        self._clock = clock
        self._sleep = sleeper
        self._tokens = float(capacity)
        self._last = clock()

    def acquire(self) -> None:
        self._refill()
        if self._tokens < 1.0:
            wait = (1.0 - self._tokens) / self._rate
            self._sleep(wait)
            self._refill()
        self._tokens -= 1.0

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._last)
        self._last = now
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
