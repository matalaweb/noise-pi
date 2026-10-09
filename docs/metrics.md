# Measurements and DSP

All processing is causal float64 with filter state preserved across callback blocks and second
boundaries. Filters reset only after a real discontinuity (sample loss, new session) or a profile
change. After a reset, a 2 s settling interval is flagged and omitted.

## Processing chain

```
int samples (valid-bit scale) -> x = s / 2^(bits-1)
  -> corrected_x = C(x)            optional min-phase FIR, 0 dB at 1 kHz (profile response_correction)
  -> p = scale_pa_per_fs * corrected_x     (no scale => uncalibrated: SPL fields null)
  -> pA = A(p), pC = C(p), pLF = LF(p);  q = Fast(pA^2)
```

## Metrics (complete one-second interval, actual covered samples)

| Field | Definition |
|---|---|
| `laeq_db` | 10 log10(mean(pA^2)/p0^2) |
| `lceq_db` | 10 log10(mean(pC^2)/p0^2) |
| `lafmax_db` | max over samples of 10 log10(q/p0^2); q[n] = a q[n-1] + (1-a) pA^2, a = exp(-1/(fs*0.125)) |
| `lcpeak_db` | **null**: sampled C-weighted peak is not a validated reconstruction. The sample peak is kept locally as `c_sample_peak_*`. |
| `low_frequency_leq_db` | 10 log10(mean(pLF^2)/p0^2). Output energy of the LF filter below, not an ideal band. |
| `rms_dbfs` | 20 log10(rms(x)), full-scale peak = 1 (DC included). A full-scale sine reads -3.01 dBFS. |

* Zero energy gives `null` with a local reason, never -inf or an invented floor. If every
  metric is null, the interval is not uploaded and is counted in `dropped_intervals`.
* Constant input, including digital silence, is flagged `suspect_constant_input`. It is
  treated as an acquisition fault, not quiet.
* Clipping is counted on the integer rails (`clipped`), with a separate `near_full_scale`
  threshold (-1 dBFS). Clipped seconds never enter the baseline. A lack of rail hits does not
  prove the microphone did not overload acoustically.
* `below_noise_floor` is only set when the profile carries a floor estimate and method.
* LAFmax >= LAeq is **not** enforced. Values are rounded to 0.01 dB on the wire.

## Filters (committed, hashed, reproducible)

`scripts/design_filters.py` regenerates `src/noise_collector/dsp/coefficients/filters_<fs>.json`
for 44.1, 48 and 96 kHz and writes `docs/validation/filter-response.md`.

* **A/C**: the IEC 61672-1 analog poles go through a bilinear transform. One least-squares
  high-frequency correction biquad compensates the bilinear compression near Nyquist. Gain is
  0 dB at 1 kHz. At 48 kHz the error versus the closed-form analog response is within ±0.03 dB
  from 10 Hz to 10 kHz, +0.06 dB at 12.5 kHz, and -0.18 dB at 16 kHz, then rolls off toward
  Nyquist (-5 dB at 20 kHz). The design target (±0.5 dB, 20 Hz–10 kHz) is a software target,
  not instrument certification.
* **LF**: Butterworth band-pass, `butter(2, [20, 125])`. That is 4th order overall, with -3.01 dB
  edges at 20 Hz and 125 Hz, 12 dB/octave skirts, and about 2 s settling. Coefficient hashes are
  recorded per sample rate.
* **Fast**: first-order recursive average of squared A-weighted pressure, with state across
  intervals. The test checks the analytic step response q[n] = 1 - a^(n+1).

## Response correction (optional, profile `fir_min_phase_v1`)

* The magnitude curve is interpolated linearly in dB over log10(f), with edge values held.
* Sign convention: `curve_is: microphone_response` applies the negated curve.
  `curve_is: correction` applies the curve as-is.
* The result is normalized to 0 dB at 1 kHz and clamped to [-max_cut, +max_boost].
* Realization: an 8193-tap linear-phase design of |H|^2, then a homomorphic minimum-phase
  conversion (4097 taps, FFT size 2^16). A magnitude-only curve has no unique phase. Minimum
  phase is causal and avoids pre-ringing, but it alters transient peak shapes.
* The design is rejected if its error inside `valid_range_hz` exceeds 0.5 dB.
* A profile with `method: none` is scalar-only and is described as such.

## Validation in the test suite

| Spec check | Test |
|---|---|
| PCM normalization and raw round trips for 16/24/24-in-32/32-bit | `tests/unit/test_pcm_wav.py` |
| 1 Pa RMS -> 93.98 dB; full-scale sine -> -3.01 dBFS; 40/60 dB -> 57.03 dB | `tests/unit/test_metrics.py` |
| Fast 125 ms step response, state across blocks | `tests/unit/test_filters.py` |
| Different callback sizes -> equivalent metrics and identical event sample ranges/bytes | `test_metrics.py::test_block_partition…`, `tests/integration/test_engine_replay.py::test_block_partition_invariance` |
| A/C within 0.5 dB, 20 Hz–10 kHz, vs closed form *and* the IEC table | `tests/unit/test_filters.py` |
| LAeq vs an independent frequency-domain reference within 0.05 dB | `test_metrics.py::test_laeq_matches_frequency_domain_reference…` |
| LF edges, correction sign/normalization/bounds, synthetic curves, zero/constant/clipping/DC | `test_filters.py`, `test_timing_calibration.py`, `test_metrics.py` |
| LCpeak unavailable | `test_metrics.py::test_lcpeak_disabled` |

None of this validates microphone gain, placement, acoustic uncertainty, or real timestamp
behavior. Those are hardware gates (`docs/hardware-validation.md`).
