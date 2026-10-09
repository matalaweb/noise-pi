"""Trailing descriptive percentile of eligible one-second levels.

This is the ``percentile``-th percentile (numpy "linear" interpolation) of eligible one-second
levels whose interval start lies within the last ``window_seconds``. It is a descriptive
statistic of 1 s levels, not a standards-based L90 and not an arithmetic Leq. It is
unavailable until ``min_eligible_seconds`` values are inside the window; because the window
slides, a baseline with no fresh eligible data for the whole window becomes unavailable
rather than adapting to a continuously loud event.
"""

from __future__ import annotations

from collections import deque

import numpy as np


class RollingPercentile:
    def __init__(self, window_seconds: int, percentile: float, min_count: int) -> None:
        self.window = window_seconds
        self.percentile = percentile
        self.min_count = min_count
        self.values: deque[tuple[int, float]] = deque()

    def configure(self, window_seconds: int, percentile: float, min_count: int) -> None:
        self.window, self.percentile, self.min_count = window_seconds, percentile, min_count

    def clear(self) -> None:
        self.values.clear()

    def add(self, second: int, value: float) -> None:
        self.values.append((second, value))

    def prune(self, now_second: int) -> None:
        cutoff = now_second - self.window
        while self.values and self.values[0][0] < cutoff:
            self.values.popleft()

    def count(self, now_second: int) -> int:
        self.prune(now_second)
        return len(self.values)

    def value(self, now_second: int) -> float | None:
        """Baseline for evaluating interval ``now_second`` (uses values strictly before it)."""
        self.prune(now_second)
        if len(self.values) < self.min_count:
            return None
        return float(np.percentile(np.fromiter((v for _, v in self.values), dtype=np.float64), self.percentile))
