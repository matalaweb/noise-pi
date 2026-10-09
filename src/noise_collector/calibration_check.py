"""Read-only absolute-scale check against a reference tone (calibrator or reference meter).

The reference RMS is taken on the *corrected* signal (same point in the chain where the
profile scale applies), band-limited to one-third octave around the reference frequency to
reject background noise, after discarding the first second. The result is a report the owner
can use to provision a new calibrated profile in Laravel; it never changes local state.
"""

from __future__ import annotations

import json
import math
import time

import numpy as np
from scipy import signal

from .dsp.calibration import P0, CorrectionSpec, design_correction, scale_from_reference, scale_from_sensitivity
from .timeutil import iso_utc


def _applied_profile(args):
    """The local measurement chain's profile (what the collector measures with)."""
    try:
        from .config.chain import build_chain
        from .config.settings import load_settings
        from .store.db import connect, get_meta

        s = load_settings(args.config and __import__("pathlib").Path(args.config))
        try:
            install = get_meta(connect(s.db_path, readonly=True), "installation_id") or "unregistered"
        except Exception:
            install = "unregistered"
        return s, build_chain(s, install).profile
    except Exception:
        return None, None


def _capture(settings, profile, seconds: float):
    from .audio.alsa_source import AlsaCapture
    from .audio.discovery import list_usb_audio, match, portaudio_index
    from .audio.gain import read_gain
    from .audio.pcm import PcmFormat

    dev = match(list_usb_audio(), settings.microphone)
    c = profile.capture
    fmt = PcmFormat(c.container, c.valid_bits, c.channels, c.sample_rate, c.analysis_channel)
    cap = AlsaCapture(portaudio_index(dev), fmt)
    cap.check()
    cap.start()
    chunks = []
    end = time.monotonic() + seconds
    try:
        while time.monotonic() < end:
            for b in cap.blocks():
                chunks.append(b.samples)
            time.sleep(0.05)
    finally:
        cap.stop()
    return np.concatenate(chunks), fmt, {"device": dev.identity(), "gain": read_gain(dev.card_index).controls}


def run(args) -> dict:
    settings, profile = _applied_profile(args)
    meta: dict = {}
    if args.file:
        from .audio.file_source import file_format, read_samples

        fmt = file_format(args.file)
        raw = read_samples(args.file, fmt)
        meta["source"] = args.file
    else:
        if settings is None or profile is None:
            return {"error": "live check needs settings and an applied profile (or use --file); stop the collector service first"}
        raw, fmt, meta = _capture(settings, profile, args.seconds)
    fs = fmt.sample_rate
    x = raw.astype(np.float64) / fmt.full_scale
    corr = "none"
    if profile is not None and profile.response_correction.method != "none":
        rc = profile.response_correction
        filt = design_correction(CorrectionSpec(tuple(p[0] for p in rc.curve), tuple(p[1] for p in rc.curve), rc.curve_is,
                                                rc.normalise_hz, rc.max_boost_db, rc.max_cut_db, tuple(rc.valid_range_hz)), fs)
        x = signal.oaconvolve(x, filt.taps)[: len(x)]
        corr = filt.description["method"]
    f = args.frequency
    sos = signal.butter(4, [f / 2 ** (1 / 6), f * 2 ** (1 / 6)], btype="bandpass", fs=fs, output="sos")
    y = signal.sosfilt(sos, x)[fs:]
    if len(y) < fs:
        return {"error": "need at least 2 s of reference signal"}
    rms = float(np.sqrt(np.mean(y * y)))
    broadband = float(np.sqrt(np.mean(x[fs:] ** 2)))
    measured_scale = scale_from_reference(y, args.level)
    out = {
        "checked_at": iso_utc(time.time()),
        "reference_level_db": args.level,
        "reference_frequency_hz": f,
        "band_filter": "Butterworth N=4 one-third octave around reference",
        "response_correction": corr,
        "measured_band_dbfs": round(20 * math.log10(rms), 3),
        "broadband_dbfs": round(20 * math.log10(broadband), 3),
        "band_to_broadband_db": round(20 * math.log10(rms / broadband), 3),
        "scale_pa_per_fs_from_reference": measured_scale,
        "note": "broadband well above band level means background noise or the wrong frequency; repeat in a quiet room",
        **meta,
    }
    if args.sensitivity_dbfs is not None:
        s2 = scale_from_sensitivity(args.sensitivity_dbfs, 94.0)
        out["scale_pa_per_fs_from_sensitivity"] = s2
        out["sensitivity_vs_reference_db"] = round(20 * math.log10(s2 / measured_scale), 3)
    if profile is not None and profile.scale is not None:
        ps = profile.scale.pa_per_fs
        out["profile_id"] = profile.profile_id
        out["calibration_id"] = profile.calibration_id
        out["profile_scale_pa_per_fs"] = ps
        out["level_with_profile_scale_db"] = round(20 * math.log10(rms * ps / P0), 3)
        out["profile_minus_reference_db"] = round(20 * math.log10(ps / measured_scale), 3)
    # Paste into /etc/noise-collector/collector.toml [calibration], then restart: the collector
    # registers the new calibration with the server and uses it from then on.
    out["collector_toml_calibration_hint"] = {
        "state": "calibrated",
        "sensitivity_dbfs_at_94db": round(20 * math.log10(10 ** (94 / 20) * P0 / measured_scale), 4),
        "reference_method": "94 dB / 1 kHz acoustic calibrator",
        "reference_level_db": 94.0,
        "reference_frequency_hz": 1000.0,
        "performed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    return out
