"""Deterministic file-replay AudioSource.

Reads PCM WAV/FLAC through libsndfile, recovers the exact integer samples at the file's native
precision, and emits blocks with synthetic, perfectly regular timestamps (``ts_source =
"synthetic"``). Block sizes follow a repeating pattern or a seeded pseudo-random sequence so the
same input can be replayed with different callback partitions. Fault injection hooks drop
frames, flag driver overflows, or step the simulated wall clock at chosen sample positions.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from dataclasses import dataclass, field

import numpy as np
import soundfile as sf

from ..acquisition.engine import CapturedBlock
from ..timing.clock import SimulatedClock
from .pcm import PcmFormat

_SUBTYPES = {"PCM_16": ("int16", 16), "PCM_24": ("int24", 24), "PCM_32": ("int32", 32)}


def file_format(path: str, analysis_channel: int = 0) -> PcmFormat:
    info = sf.info(path)
    if info.subtype not in _SUBTYPES:
        raise ValueError(f"unsupported replay subtype {info.subtype}; integer PCM required")
    container, bits = _SUBTYPES[info.subtype]
    return PcmFormat(container=container, valid_bits=bits, channels=info.channels, sample_rate=info.samplerate,
                     analysis_channel=analysis_channel)


def read_samples(path: str, fmt: PcmFormat) -> np.ndarray:
    data, _ = sf.read(path, dtype="int32", always_2d=True)
    shift = 32 - fmt.valid_bits
    ints = data >> shift if shift else data
    return np.ascontiguousarray(ints[:, fmt.analysis_channel], dtype=np.int32)


@dataclass
class Faults:
    """Fault injection keyed by stream sample index."""

    drop: dict[int, int] = field(default_factory=dict)  # at sample -> frames silently lost (buffer overflow)
    driver_overflow: dict[int, int] = field(default_factory=dict)  # at sample -> frames lost, not counted by the stream
    clock_step: dict[int, float] = field(default_factory=dict)  # at sample -> wall-clock step seconds
    clock_sync: dict[int, bool] = field(default_factory=dict)  # at sample -> synchronized flag


class FileSource:
    def __init__(
        self,
        path: str,
        *,
        start_utc: float,
        mono_start: float = 1000.0,
        block_pattern: list[int] | None = None,
        seed: int | None = None,
        analysis_channel: int = 0,
        faults: Faults | None = None,
        clock: SimulatedClock | None = None,
        rate_ppm: float = 0.0,
    ) -> None:
        self.path = path
        self.fmt = file_format(path, analysis_channel)
        self.samples = read_samples(path, self.fmt)
        self.mono_start = mono_start
        self.clock = clock or SimulatedClock(start_utc - mono_start, synchronized=True)
        self.block_pattern = block_pattern or [4800]
        self.seed = seed
        self.faults = faults or Faults()
        self.rate_ppm = rate_ppm

    def _sizes(self) -> Iterator[int]:
        if self.seed is not None:
            rng = np.random.default_rng(self.seed)
            while True:
                yield int(rng.integers(64, 4097))
        yield from itertools.cycle(self.block_pattern)

    def blocks(self) -> Iterator[CapturedBlock]:
        fs = self.fmt.sample_rate
        actual_rate = fs * (1 + self.rate_ppm * 1e-6)
        n_total = len(self.samples)
        pos = 0  # file position
        stream_index = 0  # what the capture stream would report
        sizes = self._sizes()
        pending_lost = 0
        mono_extra = 0.0
        driver_overflow = False
        drop = dict(self.faults.drop)
        overflow = dict(self.faults.driver_overflow)
        steps = dict(self.faults.clock_step)
        syncs = dict(self.faults.clock_sync)
        points = sorted(set(drop) | set(overflow) | set(steps) | set(syncs))
        while pos < n_total:
            if pos in steps:
                self.clock.step(steps.pop(pos))
            if pos in syncs:
                self.clock.synchronized = syncs.pop(pos)
            if pos in drop:
                # Known-size loss (capture buffer overflow): the stream counter advances.
                lost = drop.pop(pos)
                pending_lost += lost
                stream_index += lost
                pos += lost
                continue
            if pos in overflow:
                # Unknown-size driver loss: audio is skipped and timestamps jump, but the stream
                # counter does not advance.
                lost = overflow.pop(pos)
                mono_extra += lost / actual_rate
                pos += lost
                driver_overflow = True
                continue
            size = next(sizes)
            nxt = next((p for p in points if pos < p < pos + size), None)
            if nxt is not None:
                size = nxt - pos
            size = min(size, n_total - pos)
            blk = CapturedBlock(
                samples=self.samples[pos : pos + size],
                first_sample=stream_index,
                mono_time=self.mono_start + mono_extra + stream_index / actual_rate,
                ts_source="synthetic",
                wall_minus_mono=self.clock.wall_minus_mono(),
                lost_before=pending_lost,
                driver_overflow=driver_overflow,
            )
            pending_lost = 0
            driver_overflow = False
            yield blk
            pos += size
            stream_index += size
