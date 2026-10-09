"""Retry pacing: exponential backoff with full jitter, Retry-After, and a request rate limiter."""

from __future__ import annotations

import random
import time


def full_jitter(attempt: int, base: float = 1.0, cap: float = 300.0, rng: random.Random | None = None) -> float:
    """AWS-style full jitter: uniform(0, min(cap, base * 2**attempt))."""
    r = rng or random
    return r.uniform(0.0, min(cap, base * (2 ** min(attempt, 30))))


def next_delay(attempt: int, retry_after: float | None, rng: random.Random | None = None) -> float:
    d = full_jitter(attempt, rng=rng)
    if retry_after is not None:
        d = max(d, retry_after)
    return min(d, 3600.0)


class RateLimiter:
    """Token bucket: at most ``per_minute`` acquisitions per rolling minute on average."""

    def __init__(self, per_minute: int, burst: int | None = None, clock=time.monotonic) -> None:
        self.rate = per_minute / 60.0
        self.capacity = float(burst if burst is not None else max(1, per_minute // 6))
        self.tokens = self.capacity
        self.clock = clock
        self.last = clock()

    def _refill(self) -> None:
        now = self.clock()
        self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
        self.last = now

    def try_acquire(self) -> bool:
        self._refill()
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False

    def wait_time(self) -> float:
        self._refill()
        return 0.0 if self.tokens >= 1.0 else (1.0 - self.tokens) / self.rate

    def penalize(self, seconds: float) -> None:
        """Reduce request pressure after a 429 by draining the bucket for ``seconds``."""
        self.tokens = min(self.tokens, -seconds * self.rate)
