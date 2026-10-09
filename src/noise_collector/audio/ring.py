"""Bounded buffers.

``CaptureBuffer`` sits between the PortAudio callback and the DSP thread. It is single-producer
/single-consumer over preallocated storage: the callback only copies bytes and writes a few
scalars; it never allocates arrays, logs, blocks on a lock, or touches SQLite. If the consumer
falls behind, incoming frames are counted as overflow and *not* stored; the consumer learns the
exact number of lost frames before the next stored block, so the affected interval is
invalidated instead of silently shortened.

``PcmRing`` holds the most recent decoded analysis-channel samples (pre-roll) indexed by
absolute stream sample index.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class RawBlock:
    data: bytes
    first_frame: int
    frames: int
    adc_time: float  # stream clock seconds of the first frame, 0.0 if unavailable
    current_time: float  # stream clock at callback entry
    host_mono: float  # time.monotonic() at callback entry
    status_flags: int
    lost_before: int  # frames dropped (buffer full or driver overflow) immediately before this block


class CaptureBuffer:
    STATUS_INPUT_OVERFLOW = 1

    def __init__(self, capacity_frames: int, bytes_per_frame: int, max_blocks: int = 4096) -> None:
        self.bpf = bytes_per_frame
        self.capacity = capacity_frames * bytes_per_frame
        self.data = np.zeros(self.capacity, dtype=np.uint8)
        self.max_blocks = max_blocks
        self.meta_offset = np.zeros(max_blocks, dtype=np.int64)
        self.meta_bytes = np.zeros(max_blocks, dtype=np.int64)
        self.meta_first = np.zeros(max_blocks, dtype=np.int64)
        self.meta_adc = np.zeros(max_blocks, dtype=np.float64)
        self.meta_cur = np.zeros(max_blocks, dtype=np.float64)
        self.meta_mono = np.zeros(max_blocks, dtype=np.float64)
        self.meta_status = np.zeros(max_blocks, dtype=np.int64)
        self.meta_lost = np.zeros(max_blocks, dtype=np.int64)
        # Producer-owned
        self.write_block = 0
        self.write_pos = 0
        self.next_frame = 0
        self.pending_lost = 0
        # Consumer-owned
        self.read_block = 0
        self.read_pos = 0
        # Shared counters (monotonic, written by producer only)
        self.overflow_frames = 0
        self.driver_overflows = 0
        self.callbacks = 0

    # -- producer (audio callback) ---------------------------------------------------------

    def push(self, indata, frames: int, adc_time: float, current_time: float, host_mono: float, status_flags: int) -> None:
        self.callbacks += 1
        first = self.next_frame
        self.next_frame += frames
        if status_flags & self.STATUS_INPUT_OVERFLOW:
            self.driver_overflows += 1
        nbytes = frames * self.bpf
        in_flight_bytes = self.write_pos - self.read_pos
        in_flight_blocks = self.write_block - self.read_block
        if nbytes > self.capacity - in_flight_bytes or in_flight_blocks >= self.max_blocks:
            self.pending_lost += frames
            self.overflow_frames += frames
            return
        start = self.write_pos % self.capacity
        src = np.frombuffer(indata, dtype=np.uint8, count=nbytes)
        first_part = min(nbytes, self.capacity - start)
        self.data[start : start + first_part] = src[:first_part]
        if first_part < nbytes:
            self.data[: nbytes - first_part] = src[first_part:]
        slot = self.write_block % self.max_blocks
        self.meta_offset[slot] = self.write_pos
        self.meta_bytes[slot] = nbytes
        self.meta_first[slot] = first
        self.meta_adc[slot] = adc_time
        self.meta_cur[slot] = current_time
        self.meta_mono[slot] = host_mono
        self.meta_status[slot] = status_flags
        self.meta_lost[slot] = self.pending_lost
        self.pending_lost = 0
        # Publish: position first, then block count (consumer reads the count first).
        self.write_pos += nbytes
        self.write_block += 1

    # -- consumer (DSP thread) -------------------------------------------------------------

    def pop(self) -> RawBlock | None:
        if self.read_block >= self.write_block:
            return None
        slot = self.read_block % self.max_blocks
        off = int(self.meta_offset[slot])
        nbytes = int(self.meta_bytes[slot])
        start = off % self.capacity
        first_part = min(nbytes, self.capacity - start)
        if first_part == nbytes:
            data = self.data[start : start + nbytes].tobytes()
        else:
            data = self.data[start:].tobytes() + self.data[: nbytes - first_part].tobytes()
        blk = RawBlock(
            data=data,
            first_frame=int(self.meta_first[slot]),
            frames=nbytes // self.bpf,
            adc_time=float(self.meta_adc[slot]),
            current_time=float(self.meta_cur[slot]),
            host_mono=float(self.meta_mono[slot]),
            status_flags=int(self.meta_status[slot]),
            lost_before=int(self.meta_lost[slot]),
        )
        self.read_pos = off + nbytes
        self.read_block += 1
        return blk

    def headroom_fraction(self) -> float:
        return 1.0 - (self.write_pos - self.read_pos) / self.capacity


class PcmRing:
    """Most recent ``capacity`` samples of a contiguous stream, addressed by absolute index."""

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self.buf = np.zeros(capacity, dtype=np.int32)
        self.end = 0  # absolute index one past the newest sample
        self.start = 0  # absolute index of the oldest retained sample

    def reset(self, at: int) -> None:
        self.start = self.end = at

    def append(self, first: int, samples: np.ndarray) -> None:
        if first != self.end:
            self.reset(first)
        n = len(samples)
        if n >= self.capacity:
            self.buf[:] = samples[-self.capacity :]
            self.end = first + n
            self.start = self.end - self.capacity
            # Rotate so index arithmetic below holds.
            shift = self.start % self.capacity
            self.buf = np.roll(self.buf, shift)
            return
        pos = self.end % self.capacity
        head = min(n, self.capacity - pos)
        self.buf[pos : pos + head] = samples[:head]
        if head < n:
            self.buf[: n - head] = samples[head:]
        self.end += n
        self.start = max(self.start, self.end - self.capacity)

    def read(self, a: int, b: int) -> np.ndarray:
        if a < self.start or b > self.end or a > b:
            raise IndexError(f"range [{a},{b}) outside retained [{self.start},{self.end})")
        n = b - a
        pos = a % self.capacity
        head = min(n, self.capacity - pos)
        out = np.empty(n, dtype=np.int32)
        out[:head] = self.buf[pos : pos + head]
        if head < n:
            out[head:] = self.buf[: n - head]
        return out
