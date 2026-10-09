import math

import numpy as np

from noise_collector.dsp.calibration import (
    P0,
    CorrectionSpec,
    correction_target_db,
    design_correction,
    parse_calibration_file,
    scale_from_reference,
    scale_from_sensitivity,
)
from noise_collector.timing.mapper import TimeMapper, TimingSettings

FS = 48000
UTC0 = 1_790_000_000.0


def feed(mapper, seconds, block=4800, ppm=0.0, mono0=1000.0, wall=UTC0 - 1000.0, start_n=0, jitter_ms=0.0, rng=None):
    rate = FS * (1 + ppm * 1e-6)
    n = start_n
    upd = None
    for _ in range(int(seconds * FS / block)):
        m = mono0 + n / rate + ((rng.standard_normal() * jitter_ms / 1000) if rng is not None else 0.0)
        upd = mapper.observe(n, m, "adc", wall)
        if upd.discontinuity:
            return upd, n
        n += block
    return upd, n


def test_mapper_tracks_usb_clock_drift():
    m = TimeMapper(FS)
    feed(m, 60, ppm=80.0)
    assert abs((m.rate / FS - 1) * 1e6 - 80.0) < 0.5
    # boundary positions follow the actual rate, so seconds do not hold exactly 48000 samples
    b1, b2 = m.boundary(int(UTC0) + 30), m.boundary(int(UTC0) + 31)
    assert b2 - b1 == round(FS * (1 + 80e-6)) or b2 - b1 == round(FS * (1 + 80e-6)) + 1


def test_mapper_tolerates_jitter_without_discontinuity():
    m = TimeMapper(FS)
    upd, _ = feed(m, 30, jitter_ms=3.0, rng=np.random.default_rng(0))
    assert upd.discontinuity is None
    assert m.resid_ewma_ms < 6


def test_wall_clock_step_is_a_discontinuity():
    m = TimeMapper(FS, TimingSettings(step_tolerance_ms=50))
    feed(m, 5)
    upd = m.observe(5 * FS, 1005.0, "adc", UTC0 - 1000.0 + 0.2)
    assert upd.discontinuity == "wall_clock_step"
    m2 = TimeMapper(FS)
    feed(m2, 5)
    assert m2.observe(5 * FS, 1005.0, "adc", UTC0 - 1000.0 + 0.01).discontinuity is None  # small slew


def test_timestamp_jump_requires_consecutive_outliers():
    m = TimeMapper(FS, TimingSettings(outlier_limit=3))
    _, n = feed(m, 10)
    assert m.observe(n, 1000.0 + n / FS + 0.5, "adc", UTC0 - 1000).discontinuity is None
    assert m.observe(n + 4800, 1000.0 + (n + 4800) / FS + 0.5, "adc", UTC0 - 1000).discontinuity is None
    assert m.observe(n + 9600, 1000.0 + (n + 9600) / FS + 0.5, "adc", UTC0 - 1000).discontinuity == "timestamp_jump"


def test_boundaries_are_monotonic_and_contiguous():
    m = TimeMapper(FS)
    feed(m, 30, ppm=-120)
    bs = [m.boundary(int(UTC0) + k) for k in range(5, 25)]
    assert all(b2 > b1 for b1, b2 in zip(bs, bs[1:]))
    for k in range(5, 24):
        n = m.boundary(int(UTC0) + k)
        assert m.second_of_sample(n) == int(UTC0) + k
        assert m.second_of_sample(n - 1) == int(UTC0) + k - 1


def test_trust_requires_sync_and_bounded_uncertainty():
    m = TimeMapper(FS)
    feed(m, 5)
    assert m.trusted(False, 1.0) == (False, "clock_unsynchronized")
    assert m.trusted(True, None) == (False, "uncertainty_unknown")
    assert m.trusted(True, 500.0) == (False, "uncertainty_exceeds_limit")
    assert m.trusted(True, 1.0)[0]


# ---------------------------------------------------------------------------- calibration

UMIK_LIKE = b'''"Sens Factor =-1.378dB, AGain =18dB, SERNO: 7000001"
10.054\t-1.7
20.108\t-0.6
50.000\t-0.1
1000.0\t0.0
10000.0\t0.8
20000.0\t-2.0
'''


def test_parse_calibration_file_keeps_header_without_interpreting():
    cal = parse_calibration_file(UMIK_LIKE)
    assert cal.header["Sens Factor"] == "-1.378dB"
    assert cal.header["SERNO"] == "7000001"
    assert len(cal.freqs_hz) == 6 and cal.response_db[0] == -1.7
    assert len(cal.sha256) == 64


def test_reference_scale_formula():
    x = np.sin(2 * np.pi * 1000 * np.arange(FS) / FS) * 0.1
    s = scale_from_reference(x, 94.0)
    p = s * x
    assert abs(20 * math.log10(np.sqrt(np.mean(p**2)) / P0) - 94.0) < 1e-9
    # manufacturer-style sensitivity: -18 dBFS RMS reading at 94 dB SPL
    s2 = scale_from_sensitivity(-18.0, 94.0)
    assert abs(20 * math.log10(10 ** (-18 / 20) * s2 / P0) - 94.0) < 1e-9


def test_correction_sign_normalisation_and_bounds():
    spec = CorrectionSpec(curve_freqs_hz=(20, 1000, 10000), curve_db=(-3.0, 1.0, 25.0), max_cut_db=20, max_boost_db=10)
    t = correction_target_db(spec, np.array([20.0, 1000.0, 10000.0]))
    # mic response -3 dB at 20 Hz (relative +1 at 1k): correction boosts by 4 dB; huge peak is clamped
    assert t[1] == 0.0
    assert abs(t[0] - 4.0) < 1e-9
    assert t[2] == -20.0
    spec_c = CorrectionSpec(curve_freqs_hz=(20, 1000), curve_db=(2.0, 0.0), curve_is="correction")
    assert abs(correction_target_db(spec_c, np.array([20.0]))[0] - 2.0) < 1e-9


def test_correction_filter_realises_synthetic_curve():
    f = np.geomspace(10, 20000, 60)
    d = 3 * np.exp(-((np.log10(f) - np.log10(30)) ** 2) / 0.1) - 2 * np.exp(-((np.log10(f) - 4) ** 2) / 0.05)
    filt = design_correction(CorrectionSpec(tuple(f), tuple(d)), FS)
    assert filt.max_error_db < 0.5
    # independent check at 1 kHz and 10 kHz from the impulse response itself
    from scipy import signal

    _, h = signal.freqz(filt.taps, worN=[1000.0, 10000.0], fs=FS)
    want = correction_target_db(CorrectionSpec(tuple(f), tuple(d)), np.array([1000.0, 10000.0]))
    assert np.allclose(20 * np.log10(np.abs(h)), want, atol=0.1)
    assert filt.description["phase"].startswith("minimum phase")
