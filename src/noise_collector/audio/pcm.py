"""Integer PCM formats, decoding, and scaling conventions.

Conventions (documented in docs/pcm-and-scaling.md, tested in tests/unit/test_pcm.py):

* PortAudio delivers interleaved frames in host byte order. Raspberry Pi OS 64-bit and
  every supported development host are little-endian; ``decode`` asserts this.
* ``container`` is the per-sample storage PortAudio hands us: ``int16`` (2 bytes),
  ``int24`` (packed 3 bytes) or ``int32`` (4 bytes).
* ``valid_bits`` is the precision of the converter. ``int32`` with ``valid_bits=24`` is
  the left-justified "24-in-32" layout: the low 8 bits must be zero or decoding fails.
* Decoded samples are signed two's-complement integers in the *valid-bit* range,
  held in ``int32``. These integers are the preserved evidence values.
* Normalised samples are ``x = sample / 2**(valid_bits - 1)``, so the negative rail is
  exactly -1.0 and a full-scale-peak sine has RMS 1/sqrt(2) (-3.01 dBFS).
* ADC clipping is counted on the integer samples at the format rails
  ``-2**(valid_bits-1)`` and ``2**(valid_bits-1) - 1``.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Literal

import numpy as np

Container = Literal["int16", "int24", "int32"]

_CONTAINER_BYTES = {"int16": 2, "int24": 3, "int32": 4}


class PcmValidationError(ValueError):
    """Captured bytes do not match the declared PCM layout."""


@dataclass(frozen=True)
class PcmFormat:
    container: Container
    valid_bits: int
    channels: int
    sample_rate: int
    analysis_channel: int = 0

    def __post_init__(self) -> None:
        if self.container not in _CONTAINER_BYTES:
            raise ValueError(f"unsupported container {self.container!r}")
        allowed = {"int16": (16,), "int24": (24,), "int32": (24, 32)}[self.container]
        if self.valid_bits not in allowed:
            raise ValueError(f"{self.container} cannot carry {self.valid_bits} valid bits")
        if self.channels < 1:
            raise ValueError("channels must be >= 1")
        if not 0 <= self.analysis_channel < self.channels:
            raise ValueError("analysis_channel out of range")
        if self.sample_rate <= 0:
            raise ValueError("sample_rate must be positive")

    @property
    def bytes_per_sample(self) -> int:
        return _CONTAINER_BYTES[self.container]

    @property
    def bytes_per_frame(self) -> int:
        return self.bytes_per_sample * self.channels

    @property
    def full_scale(self) -> int:
        return 1 << (self.valid_bits - 1)

    @property
    def rail_positive(self) -> int:
        return self.full_scale - 1

    @property
    def rail_negative(self) -> int:
        return -self.full_scale

    @property
    def evidence_bits(self) -> int:
        """Bit depth of preserved evidence (never padded beyond the converter precision)."""
        return self.valid_bits

    @property
    def portaudio_dtype(self) -> str:
        return self.container

    def describe(self) -> dict:
        return {
            "container": self.container,
            "valid_bits": self.valid_bits,
            "channels": self.channels,
            "sample_rate": self.sample_rate,
            "analysis_channel": self.analysis_channel,
            "byte_order": "little",
            "signed": True,
            "normalisation": f"x = sample / 2**{self.valid_bits - 1}",
        }


def _require_little_endian() -> None:
    if sys.byteorder != "little":  # pragma: no cover - no supported big-endian target
        raise PcmValidationError("only little-endian hosts are supported")


def decode_frames(raw: bytes | bytearray | memoryview | np.ndarray, fmt: PcmFormat) -> np.ndarray:
    """Decode interleaved PCM bytes to an ``(frames, channels)`` int32 array of valid-bit values."""
    _require_little_endian()
    buf = np.frombuffer(raw, dtype=np.uint8) if not isinstance(raw, np.ndarray) else raw.view(np.uint8).reshape(-1)
    if buf.size % fmt.bytes_per_frame:
        raise PcmValidationError(
            f"{buf.size} bytes is not a whole number of {fmt.bytes_per_frame}-byte frames"
        )
    if fmt.container == "int16":
        out = buf.view("<i2").astype(np.int32)
    elif fmt.container == "int24":
        b = buf.reshape(-1, 3).astype(np.int32)
        out = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        out = np.where(out & 0x800000, out - (1 << 24), out).astype(np.int32)
    else:
        words = buf.view("<i4")
        if fmt.valid_bits == 24:
            if np.any(words & 0xFF):
                raise PcmValidationError("24-in-32 sample has non-zero low byte; layout not validated")
            out = (words >> 8).astype(np.int32)
        else:
            out = words.astype(np.int32)
    return out.reshape(-1, fmt.channels)


def select_channel(frames: np.ndarray, fmt: PcmFormat) -> np.ndarray:
    """Return the documented analysis channel as a contiguous 1-D int32 array."""
    return np.ascontiguousarray(frames[:, fmt.analysis_channel], dtype=np.int32)


def normalise(samples: np.ndarray, fmt: PcmFormat) -> np.ndarray:
    """Convert valid-bit integers to float64 with full-scale peak at 1.0."""
    return samples.astype(np.float64) / float(fmt.full_scale)


def encode_le(samples: np.ndarray, bits: int) -> bytes:
    """Encode 1-D valid-bit integers as little-endian PCM of ``bits`` (16, 24 or 32)."""
    s = np.asarray(samples, dtype=np.int32)
    if bits == 16:
        if s.size and (s.max() > 32767 or s.min() < -32768):
            raise PcmValidationError("value out of 16-bit range")
        return s.astype("<i2").tobytes()
    if bits == 24:
        if s.size and (s.max() > 0x7FFFFF or s.min() < -0x800000):
            raise PcmValidationError("value out of 24-bit range")
        u = s.astype("<i4").view(np.uint8).reshape(-1, 4)
        return np.ascontiguousarray(u[:, :3]).tobytes()
    if bits == 32:
        return s.astype("<i4").tobytes()
    raise ValueError(f"unsupported bit depth {bits}")


def decode_le(raw: bytes, bits: int) -> np.ndarray:
    """Inverse of :func:`encode_le` for mono evidence files and chunks."""
    container: Container = {16: "int16", 24: "int24", 32: "int32"}[bits]  # type: ignore[assignment]
    fmt = PcmFormat(container=container, valid_bits=bits, channels=1, sample_rate=1)
    return decode_frames(raw, fmt)[:, 0]


def clip_counts(samples: np.ndarray, fmt: PcmFormat) -> tuple[int, int]:
    """Count samples sitting on the positive and negative digital rails."""
    return int(np.count_nonzero(samples >= fmt.rail_positive)), int(
        np.count_nonzero(samples <= fmt.rail_negative)
    )
