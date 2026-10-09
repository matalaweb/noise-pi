"""miniDSP UMIK-1 specifics (see docs/umik1.md for sources and the evidence behind each rule).

Identity
  USB 2752:0007. Older units report the product string ``Umik-1  Gain: 18dB`` (two spaces; the
  number is the internal analog gain: 0/12/18 dB seen in the field); a newer revision (bcdDevice
  1.23, manufacturer ``miniDSP Ltd.``, mono, asynchronous endpoint) reports just ``UMIK-1`` and no
  gain. The ALSA card id is derived from the product string (``U18dB``, ``UMIK1``...), so it is
  never used for matching. The USB iSerial is a
  placeholder (``000-0000`` or ``1``) on every unit: the real serial is the 7-digit number on the
  body and in the calibration file (``SERNO``). Physical identity is therefore pinned by USB port
  path (or "exactly one UMIK-1 connected") plus the calibration file serial on the web-app profile.

Format
  UAC1, S24_3LE, 48 kHz only. Older units expose 2 channels carrying the same signal; others
  (including the newer revision) are mono.
  ``hw:`` cannot convert, so capture uses the native channel count and analyses one channel; the
  two channels are compared continuously and never averaged.

Gain
  ``Mic`` capture volume is a real digital gain (USB-C units: 0..127 = -63.5..0.00 dB). It must sit
  at the 0.00 dB step with the switch on; the analog gain in the product string must equal the
  calibration file's ``AGain`` when both are present (units that do not report it are noted, not
  failed).

Sensitivity (estimate only)
  REW's convention for UMIK calibration files: ``Sens Factor`` is the dBFS reading for 100 dB SPL at
  the old +24 dB Windows input setting, i.e. full-scale SPL = 124 - SensFactor at 0 dB digital gain,
  so 94 dB SPL reads (SensFactor - 30 + G_mixer) dBFS RMS (full-scale sine = -3.01 dBFS). This is a
  secondary-source convention, corroborated by REW's dialog and one calibrator check (~0.4 dB), not
  a miniDSP specification: it supports an *estimated* calibration record until a 94 dB calibrator
  check confirms it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .gain import GainReading

VENDOR_ID = "2752"
PRODUCT_ID = "0007"
PRODUCT_RE = re.compile(r"^\s*umik-1\b(?:.*?gain:\s*(\d+)\s*db)?", re.I)
PLACEHOLDER_SERIALS = frozenset({"", "0", "1", "000-0000", "0000000", "00000000"})
MIXER_CONTROL = "Mic,0"
NINETY_DEG_RE = re.compile(r"90\s*[-_ ]?\s*deg", re.I)
CONVENTION_OFFSET_DB = 30.0  # 94 dB SPL reads SensFactor - 30 dBFS under the REW convention


def is_umik1(vendor_id: str | None, product_id: str | None, product: str | None) -> bool:
    return (vendor_id or "").lower() == VENDOR_ID and (product_id or "").lower() == PRODUCT_ID and bool(
        PRODUCT_RE.match(product or "Umik-1"))


def analog_gain_db(product: str | None) -> int | None:
    """Internal analog gain from the USB product string, e.g. ``Umik-1  Gain: 18dB`` -> 18."""
    m = PRODUCT_RE.match(product or "")
    return int(m.group(1)) if m and m.group(1) else None


def real_serial(usb_serial: str | None) -> str | None:
    s = (usb_serial or "").strip()
    return None if s in PLACEHOLDER_SERIALS else s


def is_ninety_degree_file(filename: str) -> bool:
    return bool(NINETY_DEG_RE.search(filename))


@dataclass(frozen=True)
class CalHeader:
    sens_factor_db: float | None
    again_db: float | None
    serno: str | None


def _db(value: str | None) -> float | None:
    if value is None:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", value)
    return float(m.group(0)) if m else None


def cal_header(header: dict[str, str]) -> CalHeader:
    """Fields of a UMIK calibration file's quoted first line (kept verbatim elsewhere)."""
    return CalHeader(_db(header.get("Sens Factor")), _db(header.get("AGain")), (header.get("SERNO") or "").strip() or None)


def mixer_check(reading: GainReading) -> tuple[bool, str | None]:
    """The ``Mic`` capture control must read 0.00 dB with the switch on, on every channel."""
    if not reading.inspectable:
        return False, f"UMIK-1 mixer not readable: {reading.error}"
    rep = reading.controls.get(MIXER_CONTROL)
    if rep is None:
        # Some firmware exposes no capture volume at all: nothing can change the digital gain.
        return True, "UMIK-1 exposes no Mic capture control (no digital gain to verify)"
    bad = [part for part in rep.split("|") if "db=0.00" not in part or "switch=off" in part]
    if bad:
        return False, (f"UMIK-1 Mic capture is {rep}; set it to the 0.00 dB step and on "
                       "(amixer -c <card> sset Mic 100% unmute; then sudo alsactl store)")
    return True, None


def gain_check(reading: GainReading, product: str | None, expected_again_db: float | None) -> tuple[bool, str | None]:
    ok, note = mixer_check(reading)
    gain = analog_gain_db(product)
    if expected_again_db is not None and gain is not None and float(gain) != float(expected_again_db):
        return False, (f"UMIK-1 analog gain is {gain} dB but the calibration file is for AGain {expected_again_db:g} dB; "
                       "use the calibration file for this gain setting")
    if expected_again_db is not None and gain is None:
        unreported = (f"this UMIK-1 does not report its analog gain (product '{product}'), so the calibration file's "
                      f"AGain {expected_again_db:g} dB cannot be checked against the device")
        note = "; ".join(n for n in (note, unreported) if n)
    return ok, note


def sensitivity_estimate(sens_factor_db: float, mixer_gain_db: float = 0.0) -> dict:
    """Suggested calibration-record values from the calibration file header (REW convention).

    Returns the RMS dBFS reading expected for 94 dB SPL and the implied full-scale SPL. This is an
    estimate for an *estimated* calibration record; confirm with a 94 dB / 1 kHz calibrator (or a
    reference meter) before marking a record *calibrated*.
    """
    s94 = sens_factor_db - CONVENTION_OFFSET_DB + mixer_gain_db
    return {
        "sensitivity_dbfs_at_94db": round(s94, 3),
        "full_scale_spl_db": round(124.0 - sens_factor_db - mixer_gain_db, 3),
        "basis": "REW UMIK convention: full-scale SPL = 124 - Sens Factor at 0 dB digital gain (secondary source; not a miniDSP specification)",
        "calibration_state": "estimated",
        "confirm_with": "noise-collector calibration-check --level 94 with an acoustic calibrator",
    }
