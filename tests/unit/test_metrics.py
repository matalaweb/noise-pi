"""Metric definitions against independently computed references."""

import math

import numpy as np
import pytest

from noise_collector.audio.pcm import PcmFormat
from noise_collector.dsp.calibration import P0
from noise_collector.dsp.processor import IntervalAccumulator, QualitySettings, SignalProcessor, finalize_interval
from noise_collector.dsp.reference import analog_weighting_db

FS = 48000
FMT = PcmFormat("int24", 24, 1, FS)
FULL = 1 << 23


def to_int(x):
    return np.clip(np.round(x * FULL), -FULL, FULL - 1).astype(np.int32)


def run_interval(raw, scale, settle=0.0, spl_allowed=True, noise_floor=None, blocks=4800, warm=None):
    proc = SignalProcessor(FMT, scale, settle_seconds=settle)
    proc.reset(0)
    n0 = 0
    if warm is not None:  # run the filters on preceding signal so the measured second is settled
        proc.process(warm)
        n0 = len(warm)
    acc = IntervalAccumulator(utc_second=0, first_sample=n0)
    for a in range(0, len(raw), blocks):
        acc.add(proc.process(raw[a : a + blocks]), FMT, int(FULL * 10 ** (-1 / 20)), proc.settled_from_sample)
    return finalize_interval(acc, FMT, scaled=scale is not None, spl_allowed=spl_allowed, noise_floor_laeq_db=noise_floor,
                             quality=QualitySettings(), timestamp_fallback=False)


def sine(freq, amp, n=FS, phase=0.0):
    t = np.arange(n) / FS
    return amp * np.sin(2 * np.pi * freq * t + phase)


def test_one_pascal_rms_at_1khz_is_93_98_db():
    # Scale 1 Pa per FS; a 1 kHz sine with RMS 1/sqrt(2) FS ... choose RMS = 0.5 FS and scale 2 Pa/FS -> 1 Pa RMS.
    x = sine(1000, 0.5 * math.sqrt(2), FS * 2)
    raw = to_int(x)
    res = run_interval(raw[FS:], 2.0, warm=raw[:FS])
    assert res.complete
    expected = 20 * math.log10(1.0 / P0)
    assert abs(expected - 93.979) < 0.001
    assert abs(res.metrics["laeq_db"] - expected) < 0.01  # A = 0 dB at 1 kHz
    assert abs(res.metrics["lceq_db"] - expected) < 0.01
    # Fast max of a steady sine sits within the ripple of the exponential average
    assert abs(res.metrics["lafmax_db"] - expected) < 0.1


def test_full_scale_sine_is_minus_3_01_dbfs():
    raw = to_int(sine(997, (FULL - 1) / FULL))
    res = run_interval(raw, None)
    assert abs(res.metrics["rms_dbfs"] - (-3.0103)) < 0.001
    for k in ("laeq_db", "lceq_db", "lafmax_db", "low_frequency_leq_db"):
        assert res.metrics[k] is None and res.null_reasons[k] == "uncalibrated"


def test_energy_combination_40_and_60_db_is_57_03():
    from noise_collector.acquisition.engine import _energy_mean

    assert abs(_energy_mean([40.0, 60.0]) - 57.03) < 0.005


def test_two_second_interval_energy_integration():
    """Equal-duration 40 dB and 60 dB seconds integrated together give 57.03 dB (squared pressure)."""
    amp = lambda level: 10 ** (level / 20) * P0 * math.sqrt(2)  # noqa: E731  (scale = 1 Pa per FS)
    warm = to_int(sine(1000, amp(40)))
    x = np.concatenate([sine(1000, amp(40)), sine(1000, amp(60))])
    proc = SignalProcessor(FMT, 1.0, settle_seconds=0)
    proc.reset(0)
    proc.process(warm)
    acc = IntervalAccumulator(0, FS)
    acc.add(proc.process(to_int(x)), FMT, FULL, 0)
    got = 10 * math.log10(acc.sum_pa2 / acc.n / P0**2)
    assert abs(got - 57.03) < 0.05, got


def test_lafmax_is_samplewise_max_not_interval_end():
    # 100 ms tone burst at the start of the interval, then silence-ish noise floor
    scale = 1.0
    burst = np.zeros(FS)
    burst[: FS // 10] = sine(1000, 0.5, FS // 10)
    burst += np.random.default_rng(0).standard_normal(FS) * 1e-5
    res = run_interval(to_int(burst), scale, warm=to_int(np.random.default_rng(1).standard_normal(FS) * 1e-5))
    q_end_db = res.metrics["laeq_db"]
    assert res.metrics["lafmax_db"] > q_end_db + 5  # max is captured mid-interval
    # Analytic Fast response to a 100 ms burst of mean square m: 1 - exp(-0.1/0.125) of m
    m = (0.5 / math.sqrt(2)) ** 2
    expected = 10 * math.log10(m * (1 - math.exp(-0.1 / 0.125)) / P0**2)
    assert abs(res.metrics["lafmax_db"] - expected) < 0.1


def test_laeq_matches_frequency_domain_reference_for_bandlimited_noise():
    """Independent check: FFT power weighted by the analog A curve vs time-domain filter LAeq."""
    rng = np.random.default_rng(5)
    n = FS * 4
    spec = np.fft.rfft(rng.standard_normal(n))
    f = np.fft.rfftfreq(n, 1 / FS)
    spec[(f < 30) | (f > 8000)] = 0
    x = np.fft.irfft(spec, n)
    x *= 0.05 / np.sqrt(np.mean(x**2))
    raw = to_int(x)
    xs = raw.astype(np.float64) / FULL
    res = run_interval(raw[FS:], 1.0, warm=raw[:FS])
    # reference: periodic signal -> exact circular spectrum of the quantised samples
    spec_q = np.fft.rfft(xs)
    w = np.zeros_like(f)
    w[1:] = 10 ** (analog_weighting_db("A", f[1:]) / 10)
    power = np.abs(spec_q) ** 2 * w
    power[1:-1] *= 2
    ms = power.sum() / n**2
    ref = 10 * math.log10(ms / P0**2)
    assert abs(res.metrics["laeq_db"] - ref) < 0.05, (res.metrics["laeq_db"], ref)


def test_zero_input_is_null_and_omitted():
    res = run_interval(np.zeros(FS, dtype=np.int32), 1.0)
    assert not res.complete and res.all_null
    assert res.omit_reason in ("zero_input", "all_metrics_null")
    assert "suspect_constant_input" in res.quality_flags
    assert res.null_reasons["rms_dbfs"] == "zero_energy"


def test_constant_nonzero_input_flagged_not_quiet():
    res = run_interval(np.full(FS, 1000, dtype=np.int32), 1.0)
    assert "suspect_constant_input" in res.quality_flags
    assert res.metrics["laeq_db"] is None
    assert res.metrics["rms_dbfs"] is not None
    assert not res.baseline_eligible


def test_clipping_counted_on_integer_rails_only():
    raw = to_int(sine(100, 1.5))  # hard-clipped at the rails
    res = run_interval(raw, 1.0)
    assert "clipped" in res.quality_flags
    d = res.diagnostics
    assert d["clip_positive"] > 0 and d["clip_negative"] > 0
    # A loud but unclipped signal is only near-full-scale
    res2 = run_interval(to_int(sine(100, 0.95)), 1.0)
    assert "clipped" not in res2.quality_flags and "near_full_scale" in res2.quality_flags
    assert not res.baseline_eligible


def test_dc_offset_flag_and_highpass_metrics():
    raw = to_int(sine(1000, 0.01) + 0.05)
    res = run_interval(raw, 1.0, warm=raw)
    assert "dc_offset" in res.quality_flags
    ref = run_interval(to_int(sine(1000, 0.01)), 1.0, warm=to_int(sine(1000, 0.01)))
    assert abs(res.metrics["laeq_db"] - ref.metrics["laeq_db"]) < 0.05  # DC removed by weighting


def test_gain_mismatch_withholds_spl():
    raw = to_int(sine(1000, 0.1))
    res = run_interval(raw, 1.0, spl_allowed=False)
    assert res.metrics["laeq_db"] is None and "spl_withheld_gain_mismatch" in res.quality_flags
    assert res.metrics["rms_dbfs"] is not None


def test_settling_interval_omitted():
    res = run_interval(to_int(sine(1000, 0.1)), 1.0, settle=2.0)
    assert not res.complete and res.omit_reason == "filter_settling"


def test_noise_floor_flag_only_with_profile_floor():
    raw = to_int(sine(1000, 1e-4))
    assert "below_noise_floor" not in run_interval(raw, 1.0).quality_flags
    assert "below_noise_floor" in run_interval(raw, 1.0, noise_floor=200.0).quality_flags


def test_lcpeak_disabled():
    res = run_interval(to_int(sine(1000, 0.1)), 1.0)
    assert res.metrics["lcpeak_db"] is None and res.null_reasons["lcpeak_db"] == "capability_disabled"
    assert res.diagnostics["c_sample_peak_db_spl"] is not None  # local diagnostic only


@pytest.mark.parametrize("blocks", [1, 64, 997, 4800, 48000])
def test_block_partition_does_not_change_metrics(blocks):
    raw = to_int(np.random.default_rng(9).standard_normal(FS) * 0.05)
    ref = run_interval(raw, 1.0, blocks=48000)
    got = run_interval(raw, 1.0, blocks=blocks)
    for k, v in ref.metrics.items():
        if v is not None:
            assert abs(got.metrics[k] - v) < 1e-9
