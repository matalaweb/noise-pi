"""Filter response and statefulness. Software design targets only, not standards conformance."""

import json
from pathlib import Path

import numpy as np
import pytest
from scipy import signal

from noise_collector.dsp.design import design_lf_band, design_weighting
from noise_collector.dsp.filters import FastWeighting, SosFilter, StreamingFir, load_filter
from noise_collector.dsp.reference import IEC_TABLE, analog_weighting_db, exact_frequency

RATES = (44100, 48000, 96000)


def response_db(sos, f, fs):
    _, h = signal.sosfreqz(sos, np.atleast_1d(f), fs=fs)
    return 20 * np.log10(np.abs(h))


@pytest.mark.parametrize("fs", RATES)
@pytest.mark.parametrize("kind", ["A", "C"])
def test_weighting_matches_analog_within_half_db_20hz_to_10khz(kind, fs):
    sos = load_filter(kind, fs).sos
    f = np.geomspace(20, 10000, 400)
    err = response_db(sos, f, fs) - analog_weighting_db(kind, f)
    assert np.max(np.abs(err)) < 0.5, f"max error {np.max(np.abs(err)):.3f} dB"
    assert abs(response_db(sos, 1000.0, fs)[0]) < 1e-9  # normalised at 1 kHz


@pytest.mark.parametrize("kind", ["A", "C"])
def test_weighting_matches_iec_table_at_48k(kind):
    sos = load_filter(kind, 48000).sos
    for nominal, (a, c) in IEC_TABLE.items():
        if not 20 <= nominal <= 10000:
            continue
        expected = a if kind == "A" else c
        got = response_db(sos, exact_frequency(nominal), 48000)[0]
        assert abs(got - expected) < 0.1, (nominal, got, expected)


def test_iec_table_consistent_with_closed_form():
    """Two independent references agree (guards against typos in either)."""
    for nominal, (a, c) in IEC_TABLE.items():
        f = exact_frequency(nominal)
        assert abs(analog_weighting_db("A", f) - a) < 0.06
        assert abs(analog_weighting_db("C", f) - c) < 0.06


@pytest.mark.parametrize("fs", RATES)
def test_committed_coefficients_are_reproducible(fs):
    committed = json.loads((Path(__file__).parents[2] / f"src/noise_collector/dsp/coefficients/filters_{fs}.json").read_text())
    for kind, fresh in (("A", design_weighting("A", fs)), ("C", design_weighting("C", fs)), ("LF", design_lf_band(fs))):
        assert np.allclose(np.array(fresh["sos"]), np.array(committed["filters"][kind]["sos"]), atol=1e-9)


def test_filters_are_stable():
    for fs in RATES:
        for k in ("A", "C", "LF"):
            sos = load_filter(k, fs).sos
            for sec in sos:
                assert np.all(np.abs(np.roots(sec[3:])) < 1.0)


def test_lf_band_edges_and_passband():
    sos = load_filter("LF", 48000).sos
    assert abs(response_db(sos, 20.0, 48000)[0] + 3.01) < 0.05
    assert abs(response_db(sos, 125.0, 48000)[0] + 3.01) < 0.05
    assert abs(response_db(sos, np.sqrt(20 * 125), 48000)[0]) < 0.05
    assert response_db(sos, 1000.0, 48000)[0] < -35
    assert response_db(sos, 5.0, 48000)[0] < -20


def _split_apply(make, x, sizes):
    f = make()
    out, pos = [], 0
    for n in sizes:
        out.append(f(x[pos : pos + n]))
        pos += n
    out.append(f(x[pos:]))
    return np.concatenate(out)


@pytest.mark.parametrize(
    "make",
    [
        lambda: SosFilter(load_filter("A", 48000).sos),
        lambda: SosFilter(load_filter("LF", 48000).sos),
        lambda: FastWeighting(48000),
        lambda: StreamingFir(np.random.default_rng(0).standard_normal(257)),
    ],
)
def test_state_preserved_across_arbitrary_blocks(make):
    x = np.random.default_rng(1).standard_normal(20000)
    whole = make()(x)
    for sizes in ([1, 2, 3, 4097, 50, 999], [4800] * 3, [7] * 100):
        assert np.allclose(_split_apply(make, x, sizes), whole, atol=1e-12)


def test_fast_time_constant_step_response():
    fs = 48000
    fw = FastWeighting(fs)
    q = fw(np.ones(fs))
    # 1 - 1/e at one time constant (125 ms)
    assert abs(q[int(0.125 * fs) - 1] - (1 - np.exp(-1))) < 1e-3
    # Analytical: q[n] = 1 - alpha^(n+1)
    n = np.arange(fs)
    assert np.allclose(q, 1 - fw.alpha ** (n + 1), atol=1e-12)


def test_streaming_fir_matches_direct_convolution():
    h = np.random.default_rng(2).standard_normal(64)
    x = np.random.default_rng(3).standard_normal(5000)
    got = _split_apply(lambda: StreamingFir(h), x, [100, 1, 2000])
    assert np.allclose(got, np.convolve(x, h)[: len(x)], atol=1e-10)
