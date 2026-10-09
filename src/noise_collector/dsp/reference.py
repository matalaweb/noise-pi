"""Independent reference values for weighting-filter validation.

``analog_weighting_db`` is the closed-form IEC 61672-1 (Annex E) expression; it is evaluated
directly from frequency and shares no code with the digital design. ``IEC_TABLE`` holds the
nominal tabulated weightings (IEC 61672-1:2013 Table 3) rounded to 0.1 dB, used as a second,
data-only reference.
"""

from __future__ import annotations

import numpy as np

IEC_POLE_HZ = (20.598997, 107.65265, 737.86223, 12194.217)

# nominal frequency (Hz): (A dB, C dB)
IEC_TABLE: dict[float, tuple[float, float]] = {
    10: (-70.4, -14.3),
    12.5: (-63.4, -11.2),
    16: (-56.7, -8.5),
    20: (-50.5, -6.2),
    25: (-44.7, -4.4),
    31.5: (-39.4, -3.0),
    40: (-34.6, -2.0),
    50: (-30.2, -1.3),
    63: (-26.2, -0.8),
    80: (-22.5, -0.5),
    100: (-19.1, -0.3),
    125: (-16.1, -0.2),
    160: (-13.4, -0.1),
    200: (-10.9, 0.0),
    250: (-8.6, 0.0),
    315: (-6.6, 0.0),
    400: (-4.8, 0.0),
    500: (-3.2, 0.0),
    630: (-1.9, 0.0),
    800: (-0.8, 0.0),
    1000: (0.0, 0.0),
    1250: (0.6, 0.0),
    1600: (1.0, -0.1),
    2000: (1.2, -0.2),
    2500: (1.3, -0.3),
    3150: (1.2, -0.5),
    4000: (1.0, -0.8),
    5000: (0.5, -1.3),
    6300: (-0.1, -2.0),
    8000: (-1.1, -3.0),
    10000: (-2.5, -4.4),
    12500: (-4.3, -6.2),
    16000: (-6.6, -8.5),
    20000: (-9.3, -11.2),
}


def exact_frequency(nominal: float) -> float:
    """Exact base-10 frequency for a nominal one-third-octave label (1 kHz reference)."""
    n = round(10 * np.log10(nominal / 1000.0))
    return 1000.0 * 10 ** (n / 10)


def analog_weighting_db(kind: str, f: np.ndarray | float) -> np.ndarray:
    """Closed-form analog A or C weighting in dB, normalised to 0 dB at 1 kHz."""
    f1, f2, f3, f4 = IEC_POLE_HZ

    def raw(freq: np.ndarray) -> np.ndarray:
        ff = np.asarray(freq, dtype=np.float64) ** 2
        if kind == "A":
            r = (f4**2 * ff**2) / ((ff + f1**2) * np.sqrt((ff + f2**2) * (ff + f3**2)) * (ff + f4**2))
        elif kind == "C":
            r = (f4**2 * ff) / ((ff + f1**2) * (ff + f4**2))
        else:
            raise ValueError(kind)
        return 20 * np.log10(r)

    return raw(np.asarray(f)) - raw(np.array(1000.0))
