import io

import numpy as np
import pytest
import soundfile as sf

from noise_collector.audio.pcm import (
    PcmFormat,
    PcmValidationError,
    clip_counts,
    decode_frames,
    decode_le,
    encode_le,
    normalise,
    select_channel,
)
from noise_collector.evidence import wav


def test_int24_packed_decode_sign_and_rails():
    fmt = PcmFormat("int24", 24, 1, 48000)
    vals = np.array([0, 1, -1, 0x7FFFFF, -0x800000, 123456, -654321], dtype=np.int32)
    raw = encode_le(vals, 24)
    assert len(raw) == 3 * len(vals)
    out = decode_frames(raw, fmt)[:, 0]
    assert out.tolist() == vals.tolist()
    assert clip_counts(out, fmt) == (1, 1)


def test_int24_in_32_requires_zero_low_byte():
    fmt = PcmFormat("int32", 24, 1, 48000)
    good = (np.array([5, -5, 0x7FFFFF], dtype=np.int64) << 8).astype("<i4").tobytes()
    assert decode_frames(good, fmt)[:, 0].tolist() == [5, -5, 0x7FFFFF]
    bad = np.array([0x101], dtype="<i4").tobytes()
    with pytest.raises(PcmValidationError):
        decode_frames(bad, fmt)


def test_int16_and_int32_round_trip():
    for bits, container in ((16, "int16"), (32, "int32")):
        fmt = PcmFormat(container, bits, 1, 48000)
        rng = np.random.default_rng(bits)
        vals = rng.integers(-(1 << (bits - 1)), 1 << (bits - 1), 1000, dtype=np.int64).astype(np.int32)
        assert decode_frames(encode_le(vals, bits), fmt)[:, 0].tolist() == vals.tolist()
        assert decode_le(encode_le(vals, bits), bits).tolist() == vals.tolist()


def test_channel_selection_documented_channel():
    fmt = PcmFormat("int16", 16, 2, 48000, analysis_channel=1)
    inter = np.array([[1, 10], [2, 20], [3, 30]], dtype="<i2").tobytes()
    assert select_channel(decode_frames(inter, fmt), fmt).tolist() == [10, 20, 30]


def test_normalisation_full_scale():
    fmt = PcmFormat("int24", 24, 1, 48000)
    x = normalise(np.array([-(1 << 23), 1 << 22]), fmt)
    assert x.tolist() == [-1.0, 0.5]


@pytest.mark.parametrize("bits", [16, 24, 32])
def test_wav_writer_is_bit_exact_through_libsndfile(tmp_path, bits):
    rng = np.random.default_rng(bits)
    vals = rng.integers(-(1 << (bits - 1)), 1 << (bits - 1), 4800, dtype=np.int64).astype(np.int32)
    p = tmp_path / "x.wav"
    p.write_bytes(wav.header(48000, bits, len(vals)) + encode_le(vals, bits))
    with open(p, "rb") as fh:
        info = wav.read_info(fh)
    assert (info.frames, info.bits, info.sample_rate) == (4800, bits, 48000)
    data, fs = sf.read(str(p), dtype="int32")
    assert fs == 48000
    assert (data.astype(np.int64) >> (32 - bits)).tolist() == vals.tolist()


def test_wav_read_info_rejects_garbage():
    with pytest.raises(ValueError):
        wav.read_info(io.BytesIO(b"not a wav file at all"))
