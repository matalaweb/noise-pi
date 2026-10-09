"""Filter design procedures. Run ``scripts/design_filters.py`` to regenerate committed coefficients.

Frequency weightings (A and C)
------------------------------
1. Analog prototype from the IEC 61672-1 pole frequencies f1=20.598997, f2=107.65265,
   f3=737.86223, f4=12194.217 Hz (A: four zeros at s=0; C: two zeros at s=0).
2. Bilinear transform without pre-warping (keeps the low-frequency poles exact).
3. One extra biquad, fitted by least squares on log-magnitude error against the analog
   response at 300 log-spaced points from 10 Hz to 16 kHz (weight 1.0 up to 12.5 kHz,
   0.3 above), compensates bilinear compression near Nyquist.
4. Gain normalised to exactly 0 dB at 1 kHz.

The design target is <= 0.5 dB error from 20 Hz to 10 kHz versus the analog formula at the
actual sample rate. Behaviour above 10 kHz is reported, not claimed. This is a software
design target, not instrument certification.

Low-frequency band
------------------
Butterworth band-pass, ``scipy.signal.butter(N=2, [20, 125], btype="bandpass")``: a 4th-order
band-pass (2nd-order slopes, 12 dB/octave) with -3.01 dB edges at 20 Hz and 125 Hz. The metric
is the output energy of this filter, not an ideal rectangular band.
"""

from __future__ import annotations

import numpy as np
from scipy import optimize, signal

from .reference import IEC_POLE_HZ, analog_weighting_db

LF_BAND_EDGES_HZ = (20.0, 125.0)
LF_BAND_ORDER = 2  # per-edge Butterworth order passed to scipy.signal.butter


def _bilinear_weighting(kind: str, fs: float) -> np.ndarray:
    f1, f2, f3, f4 = IEC_POLE_HZ
    w = lambda f: 2 * np.pi * f  # noqa: E731
    if kind == "A":
        zeros = [0.0] * 4
        poles = [-w(f1)] * 2 + [-w(f2), -w(f3)] + [-w(f4)] * 2
    elif kind == "C":
        zeros = [0.0] * 2
        poles = [-w(f1)] * 2 + [-w(f4)] * 2
    else:
        raise ValueError(kind)
    zd, pd, kd = signal.bilinear_zpk(zeros, poles, 1.0, fs)
    return signal.zpk2sos(zd, pd, kd)


def _correction_biquad(params: np.ndarray) -> np.ndarray:
    rz, tz, rp, tp = params
    b = [1.0, -2 * rz * np.cos(tz), rz * rz]
    a = [1.0, -2 * rp * np.cos(tp), rp * rp]
    return np.array([b + a])


def normalise_at(sos: np.ndarray, fs: float, freq: float = 1000.0) -> np.ndarray:
    sos = np.array(sos, dtype=np.float64)
    _, h = signal.sosfreqz(sos, [freq], fs=fs)
    sos[0, :3] /= abs(h[0])
    return sos


def design_weighting(kind: str, fs: float) -> dict:
    base = _bilinear_weighting(kind, fs)
    upper = min(16000.0, 0.45 * fs)
    fit_f = np.geomspace(10.0, upper, 300)
    target = analog_weighting_db(kind, fit_f)
    weights = np.where(fit_f <= 12500.0, 1.0, 0.3)

    def resid(p: np.ndarray) -> np.ndarray:
        sos = np.vstack([base, _correction_biquad(p)])
        _, h = signal.sosfreqz(sos, np.r_[1000.0, fit_f], fs=fs)
        m = 20 * np.log10(np.abs(h))
        return (m[1:] - m[0] - target) * weights

    fit = optimize.least_squares(
        resid,
        x0=[0.5, np.pi * 0.9, 0.3, np.pi * 0.9],
        bounds=([0.0, 0.0, 0.0, 0.0], [0.99, np.pi, 0.95, np.pi]),
    )
    sos = normalise_at(np.vstack([base, _correction_biquad(fit.x)]), fs)
    return {
        "kind": kind,
        "sample_rate": fs,
        "method": "bilinear(IEC 61672-1 analog poles) + least-squares HF correction biquad; 0 dB @ 1 kHz",
        "sos": sos.tolist(),
        "correction_params": [float(v) for v in fit.x],
    }


def design_lf_band(fs: float) -> dict:
    sos = signal.butter(LF_BAND_ORDER, LF_BAND_EDGES_HZ, btype="bandpass", fs=fs, output="sos")
    return {
        "kind": "LF",
        "sample_rate": fs,
        "method": f"Butterworth band-pass N={LF_BAND_ORDER} (4th-order total), -3 dB edges {LF_BAND_EDGES_HZ} Hz",
        "edges_hz": list(LF_BAND_EDGES_HZ),
        "edge_convention": "-3.01 dB",
        "sos": sos.tolist(),
    }
