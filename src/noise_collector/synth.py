"""Deterministic synthetic audio for replay and acceptance tests.

Every generated file is SYNTHETIC. The "engine-like" and "garage-door-like" patterns are crude
signal shapes for exercising detection timing and evidence capture; they are not recordings,
not models of any real vehicle or door, and must never be presented as real observations.

Levels are given in dBFS RMS (full-scale peak = 1). With a profile scale ``s`` (Pa per FS) the
corresponding unweighted SPL is ``level_dbfs + 20 log10(s / 20e-6)``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import soundfile as sf


def _rms_db(x: np.ndarray) -> float:
    return 20 * np.log10(np.sqrt(np.mean(x * x)))


def _scale_to(x: np.ndarray, level_dbfs: float) -> np.ndarray:
    return x * (10 ** (level_dbfs / 20) / np.sqrt(np.mean(x * x)))


def pink_noise(n: int, rng: np.random.Generator) -> np.ndarray:
    white = rng.standard_normal(n)
    spec = np.fft.rfft(white)
    f = np.arange(len(spec), dtype=np.float64)
    f[0] = 1.0
    spec /= np.sqrt(f)
    out = np.fft.irfft(spec, n)
    return out / np.sqrt(np.mean(out**2))


def sine(n: int, fs: int, freq: float, level_dbfs: float, phase: float = 0.0) -> np.ndarray:
    t = np.arange(n) / fs
    return np.sqrt(2) * 10 ** (level_dbfs / 20) * np.sin(2 * np.pi * freq * t + phase)


def engine_like(duration_s: float, fs: int, rng: np.random.Generator) -> np.ndarray:
    """SYNTHETIC: harmonic low-frequency rumble with rising then falling fundamental plus noise."""
    n = int(duration_s * fs)
    t = np.arange(n) / fs
    f0 = 30 + 25 * np.sin(np.pi * t / duration_s)  # 30 -> 55 -> 30 Hz
    phase = 2 * np.pi * np.cumsum(f0) / fs
    sig = sum((1.0 / h) * np.sin(h * phase) for h in range(1, 7))
    env = np.minimum(1.0, np.minimum(t / 2.0, (duration_s - t) / 2.0)).clip(0, 1)
    noise = pink_noise(n, rng) * 0.3
    return (sig + noise) * env


def garage_door_like(duration_s: float, fs: int, rng: np.random.Generator) -> np.ndarray:
    """SYNTHETIC: steady motor hum with rattling broadband bursts, abrupt start and stop."""
    n = int(duration_s * fs)
    t = np.arange(n) / fs
    hum = np.sin(2 * np.pi * 120 * t) + 0.5 * np.sin(2 * np.pi * 240 * t)
    rattle = rng.standard_normal(n) * (0.5 + 0.5 * (np.sin(2 * np.pi * 7 * t) > 0.6))
    env = np.minimum(1.0, np.minimum(t / 0.3, (duration_s - t) / 0.3)).clip(0, 1)
    return (hum + rattle) * env


def impulse(duration_s: float, fs: int, rng: np.random.Generator) -> np.ndarray:
    """SYNTHETIC: a short decaying broadband burst (door slam-like shape)."""
    n = int(duration_s * fs)
    t = np.arange(n) / fs
    return rng.standard_normal(n) * np.exp(-t / 0.05)


PATTERNS = {"engine_like": engine_like, "garage_door_like": garage_door_like, "impulse": impulse}


@dataclass
class Burst:
    start_s: float
    duration_s: float
    pattern: str
    level_dbfs: float  # RMS of the burst's active portion


@dataclass
class Scenario:
    duration_s: float
    background_dbfs: float = -60.0
    bursts: list[Burst] = field(default_factory=list)
    fs: int = 48000
    seed: int = 1
    silence: list[tuple[float, float]] = field(default_factory=list)  # digital-zero spans (fault shapes)

    def render(self) -> np.ndarray:
        rng = np.random.default_rng(self.seed)
        n = int(self.duration_s * self.fs)
        x = _scale_to(pink_noise(n, rng), self.background_dbfs)
        for b in self.bursts:
            sig = PATTERNS[b.pattern](b.duration_s, self.fs, rng)
            sig = _scale_to(sig, b.level_dbfs)
            a = int(b.start_s * self.fs)
            x[a : a + len(sig)] += sig[: max(0, n - a)]
        for a_s, b_s in self.silence:
            x[int(a_s * self.fs) : int(b_s * self.fs)] = 0.0
        return x


def to_pcm(x: np.ndarray, bits: int = 24) -> np.ndarray:
    fs_int = 1 << (bits - 1)
    return np.clip(np.round(x * fs_int), -fs_int, fs_int - 1).astype(np.int32)


def write_wav(path: str, x: np.ndarray, fs: int = 48000, bits: int = 24) -> np.ndarray:
    """Quantise to integer PCM and write; returns the exact integer samples written."""
    ints = to_pcm(x, bits)
    subtype = {16: "PCM_16", 24: "PCM_24", 32: "PCM_32"}[bits]
    sf.write(path, (ints.astype(np.int64) << (32 - bits)).astype(np.int32), fs, subtype=subtype)
    return ints
