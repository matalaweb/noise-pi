"""Sample position -> monotonic -> UTC mapping with discontinuity detection.

Three distinct time axes:

* sample index ``n``: frames counted by the capture stream (including frames lost to overflow);
* monotonic time ``m``: host CLOCK_MONOTONIC seconds;
* UTC: ``m + wall_minus_mono``, where ``wall_minus_mono`` is sampled from the host clocks.

``m(n) = anchor_m + (n - anchor_n) / rate``. ``rate`` starts at the nominal sample rate and is
re-estimated by least squares over decimated observations (one per ``obs_spacing_s``) in a
sliding window, which tracks USB clock drift. A block timestamp whose residual exceeds the
tolerance for ``outlier_limit`` consecutive blocks, a wall-clock step above
``step_tolerance_ms``, or a rate estimate outside ``max_rate_ppm`` is a discontinuity: the owner
must start a new timing epoch (and acquisition session).

Interval boundaries are computed sequentially from sample positions (see the engine), so later
refinements of the mapping can never give a sample to two UTC seconds.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

import numpy as np

BOUNDARY_EPSILON_SAMPLES = 1e-6


@dataclass(frozen=True)
class TimingSettings:
    adc_tolerance_ms: float = 20.0
    fallback_tolerance_ms: float = 60.0
    outlier_limit: int = 3
    step_tolerance_ms: float = 50.0
    max_rate_ppm: float = 1000.0
    obs_spacing_s: float = 0.25
    window_s: float = 120.0
    min_fit_span_s: float = 5.0
    max_alignment_error_ms: float = 100.0
    min_stable_s: float = 2.0
    fallback_latency_uncertainty_ms: float = 20.0
    require_clock_sync: bool = True


@dataclass
class TimingUpdate:
    discontinuity: str | None = None
    residual_ms: float = 0.0


class TimeMapper:
    def __init__(self, nominal_rate: int, settings: TimingSettings | None = None) -> None:
        self.nominal = float(nominal_rate)
        self.s = settings or TimingSettings()
        self.reset()

    def reset(self) -> None:
        self.anchor_n: int | None = None
        self.anchor_m = 0.0
        self.rate = self.nominal
        self.obs: deque[tuple[int, float]] = deque()
        self.wall_offset: float | None = None
        self.outlier_run = 0
        self.resid_ewma_ms = 0.0
        self.fallback_seen = False
        self.first_m: float | None = None
        self.last_m: float | None = None

    # -- mapping -------------------------------------------------------------------------

    def mono_of_sample(self, n: float) -> float:
        assert self.anchor_n is not None
        return self.anchor_m + (n - self.anchor_n) / self.rate

    def utc_of_sample(self, n: float) -> float:
        assert self.wall_offset is not None
        return self.mono_of_sample(n) + self.wall_offset

    def sample_at_utc(self, t: float) -> float:
        assert self.anchor_n is not None and self.wall_offset is not None
        return self.anchor_n + (t - self.wall_offset - self.anchor_m) * self.rate

    def boundary(self, utc_second: int) -> int:
        """First sample index whose mapped UTC time is >= ``utc_second``."""
        return math.ceil(self.sample_at_utc(float(utc_second)) - BOUNDARY_EPSILON_SAMPLES)

    def second_of_sample(self, n: int) -> int:
        return math.floor(self.utc_of_sample(n) + BOUNDARY_EPSILON_SAMPLES / self.rate)

    # -- observations --------------------------------------------------------------------

    def observe(self, n: int, m: float, source: str, wall_minus_mono: float) -> TimingUpdate:
        """Feed the timestamp of the first sample of a block.

        ``source`` is ``adc`` (PortAudio ADC time), ``synthetic`` (replay) or ``fallback``
        (host receive time minus buffered frames/latency).
        """
        if source == "fallback":
            self.fallback_seen = True
        if self.wall_offset is not None and abs(wall_minus_mono - self.wall_offset) * 1000 > self.s.step_tolerance_ms:
            return TimingUpdate(discontinuity="wall_clock_step")
        self.wall_offset = wall_minus_mono
        if self.anchor_n is None:
            self.anchor_n, self.anchor_m = n, m
            self.obs.append((n, m))
            self.first_m = self.last_m = m
            return TimingUpdate()
        resid_ms = (m - self.mono_of_sample(n)) * 1000
        tol = self.s.fallback_tolerance_ms if source == "fallback" else self.s.adc_tolerance_ms
        if abs(resid_ms) > tol:
            self.outlier_run += 1
            if self.outlier_run >= self.s.outlier_limit:
                return TimingUpdate(discontinuity="timestamp_jump", residual_ms=resid_ms)
            return TimingUpdate(residual_ms=resid_ms)
        self.outlier_run = 0
        self.resid_ewma_ms = 0.95 * self.resid_ewma_ms + 0.05 * abs(resid_ms)
        self.last_m = m
        if m - self.obs[-1][1] >= self.s.obs_spacing_s:
            self.obs.append((n, m))
            while self.obs and m - self.obs[0][1] > self.s.window_s:
                self.obs.popleft()
            disc = self._refit()
            if disc:
                return TimingUpdate(discontinuity=disc, residual_ms=resid_ms)
        return TimingUpdate(residual_ms=resid_ms)

    def _refit(self) -> str | None:
        if self.obs[-1][1] - self.obs[0][1] < self.s.min_fit_span_s:
            return None
        n0 = self.obs[0][0]
        ns = np.array([o[0] - n0 for o in self.obs], dtype=np.float64)
        m0 = self.obs[0][1]
        ms = np.array([o[1] - m0 for o in self.obs], dtype=np.float64)
        slope, intercept = np.polyfit(ns, ms, 1)
        rate = 1.0 / slope
        if abs(rate / self.nominal - 1) * 1e6 > self.s.max_rate_ppm:
            return "sample_rate_out_of_range"
        self.rate = rate
        self.anchor_n = n0
        self.anchor_m = m0 + float(intercept)
        return None

    # -- quality -------------------------------------------------------------------------

    def uncertainty_ms(self, clock_est_error_ms: float | None) -> float | None:
        if clock_est_error_ms is None:
            return None
        u = self.resid_ewma_ms + clock_est_error_ms
        if self.fallback_seen:
            u += self.s.fallback_latency_uncertainty_ms
        return u

    def trusted(self, clock_synchronized: bool | None, clock_est_error_ms: float | None) -> tuple[bool, str | None]:
        if self.anchor_n is None or self.wall_offset is None:
            return False, "no_mapping"
        if self.s.require_clock_sync and not clock_synchronized:
            return False, "clock_unsynchronized"
        if self.last_m is not None and self.first_m is not None and self.last_m - self.first_m < self.s.min_stable_s:
            return False, "mapping_settling"
        u = self.uncertainty_ms(clock_est_error_ms if clock_est_error_ms is not None else (0.0 if not self.s.require_clock_sync else None))
        if u is None:
            return False, "uncertainty_unknown"
        if u > self.s.max_alignment_error_ms:
            return False, "uncertainty_exceeds_limit"
        return True, None

    def describe(self) -> dict:
        return {
            "rate_hz": self.rate,
            "rate_ppm": (self.rate / self.nominal - 1) * 1e6,
            "residual_ewma_ms": self.resid_ewma_ms,
            "observations": len(self.obs),
            "fallback_timestamps": self.fallback_seen,
            "wall_minus_mono": self.wall_offset,
        }
