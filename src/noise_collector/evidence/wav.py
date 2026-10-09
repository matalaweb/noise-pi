"""Minimal, byte-exact PCM WAV writer and verifier for mono evidence files.

The writer emits a canonical 44-byte RIFF/WAVE header (WAVE_FORMAT_PCM, tag 1) followed
by little-endian integer samples at the converter's native precision. Samples are
never rescaled, dithered, padded, or resampled. libsndfile reads these files back
bit-exactly (see tests/unit/test_wav.py).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import BinaryIO

HEADER_BYTES = 44


@dataclass(frozen=True)
class WavInfo:
    sample_rate: int
    bits: int
    channels: int
    frames: int
    data_offset: int
    data_bytes: int


def header(sample_rate: int, bits: int, frames: int, channels: int = 1) -> bytes:
    if bits not in (16, 24, 32):
        raise ValueError("bits must be 16, 24 or 32")
    block_align = channels * bits // 8
    data_bytes = frames * block_align
    if data_bytes + 36 > 0xFFFFFFFF:
        raise ValueError("WAV data too large")
    return b"".join(
        [
            b"RIFF",
            struct.pack("<I", 36 + data_bytes),
            b"WAVE",
            b"fmt ",
            struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, sample_rate * block_align, block_align, bits),
            b"data",
            struct.pack("<I", data_bytes),
        ]
    )


def read_info(fh: BinaryIO) -> WavInfo:
    """Parse a RIFF/WAVE PCM header, tolerating extra chunks before ``data``."""
    fh.seek(0)
    riff = fh.read(12)
    if len(riff) != 12 or riff[:4] != b"RIFF" or riff[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE file")
    fmt = None
    while True:
        chdr = fh.read(8)
        if len(chdr) < 8:
            raise ValueError("missing data chunk")
        cid, size = chdr[:4], struct.unpack("<I", chdr[4:])[0]
        if cid == b"fmt ":
            body = fh.read(size)
            tag, channels, rate, _, block_align, bits = struct.unpack("<HHIIHH", body[:16])
            if tag not in (1, 0xFFFE):
                raise ValueError(f"unsupported WAV format tag {tag}")
            fmt = (channels, rate, bits, block_align)
        elif cid == b"data":
            if fmt is None:
                raise ValueError("data before fmt")
            channels, rate, bits, block_align = fmt
            offset = fh.tell()
            return WavInfo(rate, bits, channels, size // block_align, offset, size)
        else:
            fh.seek(size + (size & 1), 1)


def file_bytes(sample_rate: int, bits: int, frames: int) -> int:
    return HEADER_BYTES + frames * bits // 8
