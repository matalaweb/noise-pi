# PCM conventions and scaling

| Item | Convention (tested in `tests/unit/test_pcm_wav.py`) |
|---|---|
| Byte order | Little-endian host order from PortAudio (aarch64/x86_64) |
| Signedness | Two's-complement signed |
| `int16` | 2-byte samples, 16 valid bits |
| `int24` | Packed 3-byte samples (`S24_3LE`), 24 valid bits; preferred for UMIK-1-class devices |
| `int32` + 24 valid bits | Left-justified 24-in-32. The low byte must be 0, or decoding fails (layout not validated) |
| `int32` + 32 valid bits | Full 32-bit |
| Channel selection | Native channel count is captured; `analysis_channel` is selected and documented. Channels are never averaged. |
| Evidence | Selected-channel integers at valid-bit precision, written unchanged to WAV (never padded) |
| Normalization (DSP copy) | `x = s / 2^(bits-1)`; full-scale peak = 1.0; full-scale sine = -3.01 dBFS |
| Clipping rails | `-2^(bits-1)` and `2^(bits-1) - 1`, counted on integers before any filtering |

The absolute scale applies after optional response correction:
`p = scale_pa_per_fs * corrected_x`. See `docs/calibration.md`.
