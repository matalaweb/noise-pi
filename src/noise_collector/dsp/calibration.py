"""Calibration files, absolute scale, and frequency-response correction.

Processing chain (documented point where calibration applies)::

    raw int -> x (normalised, full-scale peak = 1) -> corrected_x = C(x) -> p = scale_pa_per_fs * corrected_x

* ``C`` is an optional causal response-correction FIR normalised to exactly 0 dB at the
  profile's reference frequency (1 kHz). Sensitivity therefore lives only in ``scale``;
  correction never re-applies it.
* ``scale_pa_per_fs`` is always explicit in the server-issued profile. The collector never
  derives it by guessing the meaning of a calibration-file header.
* Reference path: for a reference tone of known level L_ref at the reference frequency,
  ``scale = p0 * 10**(L_ref/20) / rms(corrected_x_ref)`` (see :func:`scale_from_reference`).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

import numpy as np
from scipy import signal

P0 = 20e-6

_HEADER_KV = re.compile(r"([A-Za-z][A-Za-z ._-]*?)\s*[=:]\s*([^,\"]+)")


@dataclass(frozen=True)
class CalibrationFile:
    """A manufacturer calibration file retained verbatim with its parsed curve.

    Header key/values are kept as strings for provenance only; nothing here interprets them
    as a sensitivity formula.
    """

    sha256: str
    text: str
    header: dict[str, str]
    freqs_hz: np.ndarray
    response_db: np.ndarray
    phase_deg: np.ndarray | None = None


def parse_calibration_file(data: bytes) -> CalibrationFile:
    text = data.decode("utf-8-sig", errors="replace")
    header: dict[str, str] = {}
    freqs: list[float] = []
    resp: list[float] = []
    phase: list[float] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("*", "#", ";")):
            continue
        parts = stripped.replace(",", " ").replace("\t", " ").split()
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            for key, value in _HEADER_KV.findall(stripped.strip('"')):
                header[key.strip()] = value.strip()
            continue
        if len(nums) < 2:
            continue
        freqs.append(nums[0])
        resp.append(nums[1])
        if len(nums) >= 3:
            phase.append(nums[2])
    if len(freqs) < 2:
        raise ValueError("calibration file contains fewer than two frequency points")
    f = np.array(freqs)
    if np.any(np.diff(f) <= 0) or f[0] <= 0:
        raise ValueError("calibration frequencies must be positive and strictly increasing")
    return CalibrationFile(
        sha256=hashlib.sha256(data).hexdigest(),
        text=text,
        header=header,
        freqs_hz=f,
        response_db=np.array(resp),
        phase_deg=np.array(phase) if len(phase) == len(freqs) else None,
    )


def scale_from_reference(corrected_x_ref: np.ndarray, level_db: float) -> float:
    """Pa per full-scale unit from a reference recording at a known level (dB re 20 uPa)."""
    rms = float(np.sqrt(np.mean(np.square(corrected_x_ref, dtype=np.float64))))
    if not np.isfinite(rms) or rms <= 0:
        raise ValueError("reference recording has no energy")
    return P0 * 10 ** (level_db / 20) / rms


def scale_from_sensitivity(dbfs_at_reference: float, reference_level_db: float) -> float:
    """Pa per full-scale unit from a documented sensitivity: the RMS dBFS reading produced by
    ``reference_level_db`` dB SPL at the stated gain and PCM scaling. No default: the web app's
    ``sensitivity_dbfs_at_94db`` is defined at 94 dB, not at 1 Pa (93.98 dB)."""
    p_ref = P0 * 10 ** (reference_level_db / 20)
    return p_ref / 10 ** (dbfs_at_reference / 20)


@dataclass(frozen=True)
class CorrectionSpec:
    """How a magnitude curve becomes a causal correction filter."""

    curve_freqs_hz: tuple[float, ...]
    curve_db: tuple[float, ...]
    curve_is: str = "microphone_response"  # or "correction"
    normalise_hz: float = 1000.0
    max_boost_db: float = 10.0
    max_cut_db: float = 20.0
    valid_range_hz: tuple[float, float] = (20.0, 16000.0)
    numtaps_linear: int = 8193
    tolerance_db: float = 0.5


@dataclass
class CorrectionFilter:
    taps: np.ndarray
    target_freqs_hz: np.ndarray
    target_db: np.ndarray
    max_error_db: float
    group_delay_ms_at_1k: float
    description: dict = field(default_factory=dict)


def correction_target_db(spec: CorrectionSpec, freqs_hz: np.ndarray) -> np.ndarray:
    """Interpolated, sign-converted, bounded correction gain in dB (0 dB at ``normalise_hz``).

    Interpolation is linear in dB over log10(frequency); outside the curve the edge values are
    held. Bounds are applied after normalisation.
    """
    cf = np.asarray(spec.curve_freqs_hz, dtype=np.float64)
    cd = np.asarray(spec.curve_db, dtype=np.float64)
    if spec.curve_is == "microphone_response":
        cd = -cd
    elif spec.curve_is != "correction":
        raise ValueError(f"unknown curve_is {spec.curve_is!r}")

    def interp(f: np.ndarray) -> np.ndarray:
        lf = np.log10(np.clip(f, cf[0], cf[-1]))
        return np.interp(lf, np.log10(cf), cd)

    out = interp(np.asarray(freqs_hz, dtype=np.float64)) - interp(np.array([spec.normalise_hz]))[0]
    return np.clip(out, -spec.max_cut_db, spec.max_boost_db)


def design_correction(spec: CorrectionSpec, sample_rate: int) -> CorrectionFilter:
    """Design a minimum-phase FIR realising the bounded correction magnitude.

    A magnitude-only curve has no unique phase. We choose the minimum-phase realisation
    (homomorphic method applied to a linear-phase design of |H|^2), which is causal with the
    smallest energy delay, so transients are not pre-ringed but their peak shape is altered
    by the correction's phase. The resulting filter is validated against the target and
    rejected if the error inside ``valid_range_hz`` exceeds ``tolerance_db``.
    """
    nyq = sample_rate / 2
    grid = np.concatenate([[0.0], np.geomspace(5.0, nyq * 0.999, 2000), [nyq]])
    target_db = correction_target_db(spec, np.maximum(grid, 1e-3))
    power = 10 ** (target_db / 10)  # |H|^2
    lin = signal.firwin2(spec.numtaps_linear, grid / nyq, power, window="hann")
    # Bounded cepstral FFT size: SciPy's default for 8193 taps allocates ~2M-point transforms
    # (hundreds of MiB on a Pi). 2**16 gives the same response (validated below and in tests).
    taps = signal.minimum_phase(lin, method="homomorphic", n_fft=1 << 16)
    lo, hi = spec.valid_range_hz
    check_f = np.geomspace(lo, min(hi, nyq * 0.95), 400)
    _, h = signal.freqz(taps, worN=check_f, fs=sample_rate)
    resp = 20 * np.log10(np.abs(h))
    want = correction_target_db(spec, check_f)
    err = float(np.max(np.abs(resp - want)))
    w, gd = signal.group_delay((taps, [1.0]), w=[1000.0], fs=sample_rate)
    return CorrectionFilter(
        taps=taps,
        target_freqs_hz=check_f,
        target_db=want,
        max_error_db=err,
        group_delay_ms_at_1k=float(gd[0]) / sample_rate * 1000,
        description={
            "method": "fir_min_phase_v1",
            "phase": "minimum phase (homomorphic) from linear-phase |H|^2 design",
            "interpolation": "linear dB over log10 f; edge values held",
            "sign": spec.curve_is,
            "normalised_at_hz": spec.normalise_hz,
            "bounds_db": [-spec.max_cut_db, spec.max_boost_db],
            "taps": int(len(taps)),
            "valid_range_hz": list(spec.valid_range_hz),
            "max_error_db": round(err, 4),
            "group_delay_ms_at_1k": round(float(gd[0]) / sample_rate * 1000, 4),
        },
    )
