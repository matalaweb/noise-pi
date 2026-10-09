"""Stateful causal filters used by the signal processor.

All filters preserve state across arbitrary block boundaries, so processing the same input
in any block partition yields the same output (tests/unit/test_filters.py).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources

import numpy as np
from scipy import signal

FAST_TAU_S = 0.125


class UnsupportedSampleRate(ValueError):
    pass


@dataclass(frozen=True)
class FilterSpec:
    kind: str
    sample_rate: int
    sos: np.ndarray
    sha256: str
    method: str


@lru_cache(maxsize=None)
def load_filter(kind: str, sample_rate: int) -> FilterSpec:
    """Load committed coefficients and verify their recorded hash."""
    name = f"filters_{sample_rate}.json"
    try:
        text = resources.files("noise_collector.dsp.coefficients").joinpath(name).read_text()
    except FileNotFoundError as exc:
        raise UnsupportedSampleRate(f"no committed coefficients for {sample_rate} Hz") from exc
    item = json.loads(text)["filters"][kind]
    digest = hashlib.sha256(json.dumps(item["sos"], separators=(",", ":")).encode()).hexdigest()
    if digest != item["sha256"]:
        raise RuntimeError(f"coefficient hash mismatch for {kind}@{sample_rate}")
    return FilterSpec(kind, sample_rate, np.array(item["sos"], dtype=np.float64), digest, item["method"])


def lf_filter(sample_rate: int, edges_hz: tuple[float, float]) -> FilterSpec:
    """Low-frequency band filter for the profile's band edges.

    The committed, validated design is used for the default 20-125 Hz band. Other edges (defined
    by a server profile) use the same documented Butterworth design computed at run time; its
    coefficient hash is recorded in diagnostics like the committed ones.
    """
    if tuple(float(e) for e in edges_hz) == (20.0, 125.0):
        return load_filter("LF", sample_rate)
    from scipy import signal as _signal

    from .design import LF_BAND_ORDER

    sos = _signal.butter(LF_BAND_ORDER, list(edges_hz), btype="bandpass", fs=sample_rate, output="sos")
    digest = hashlib.sha256(json.dumps(sos.tolist(), separators=(",", ":")).encode()).hexdigest()
    return FilterSpec("LF", sample_rate, np.asarray(sos), digest, f"Butterworth band-pass N={LF_BAND_ORDER}, -3 dB edges {edges_hz} Hz (runtime)")


class SosFilter:
    """Second-order-section IIR with persistent state."""

    def __init__(self, sos: np.ndarray) -> None:
        self.sos = np.asarray(sos, dtype=np.float64)
        self.reset()

    def reset(self) -> None:
        self.zi = np.zeros((self.sos.shape[0], 2), dtype=np.float64)

    def __call__(self, x: np.ndarray) -> np.ndarray:
        y, self.zi = signal.sosfilt(self.sos, x, zi=self.zi)
        return y


class FastWeighting:
    """Exponential time weighting of squared pressure, tau = 125 ms.

    q[n] = alpha * q[n-1] + (1 - alpha) * x[n],  alpha = exp(-1 / (fs * tau))
    """

    def __init__(self, sample_rate: int, tau: float = FAST_TAU_S) -> None:
        self.alpha = float(np.exp(-1.0 / (sample_rate * tau)))
        self.b = np.array([1.0 - self.alpha])
        self.a = np.array([1.0, -self.alpha])
        self.reset()

    def reset(self) -> None:
        self.zi = np.zeros(1, dtype=np.float64)

    def __call__(self, squared: np.ndarray) -> np.ndarray:
        y, self.zi = signal.lfilter(self.b, self.a, squared, zi=self.zi)
        return y


class StreamingFir:
    """Causal FIR via FFT convolution with an input history of ``len(h) - 1`` samples."""

    def __init__(self, taps: np.ndarray) -> None:
        self.h = np.asarray(taps, dtype=np.float64)
        self.reset()

    def reset(self) -> None:
        self.history = np.zeros(len(self.h) - 1, dtype=np.float64)

    def __call__(self, x: np.ndarray) -> np.ndarray:
        if x.size == 0:
            return x.astype(np.float64)
        buf = np.concatenate([self.history, x])
        y = signal.oaconvolve(buf, self.h, mode="valid")
        if len(self.history):
            self.history = buf[-len(self.history):].copy()
        return y
