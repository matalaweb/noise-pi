"""``noise-collector umik``: read-only setup report for a miniDSP UMIK-1.

Prints what the device reports (analog gain, native format, mixer state), what its calibration
file says (serial, Sens Factor, AGain, orientation), the local settings to use, and the values to
enter in the web app (profile serial, an *estimated* calibration record). Nothing is changed.
"""

from __future__ import annotations

from pathlib import Path

from .audio import umik1
from .audio.discovery import list_usb_audio, parse_stream_formats
from .audio.gain import read_gain
from .dsp.calibration import parse_calibration_file


def _cal_file_report(path: Path) -> dict:
    data = path.read_bytes()
    cal = parse_calibration_file(data)
    hdr = umik1.cal_header(cal.header)
    return {
        "path": str(path),
        "sha256": cal.sha256,
        "serial": hdr.serno,
        "sens_factor_db": hdr.sens_factor_db,
        "again_db": hdr.again_db,
        "orientation": "90deg" if umik1.is_ninety_degree_file(path.name) else "0deg",
        "points": int(len(cal.freqs_hz)),
        "range_hz": [float(cal.freqs_hz[0]), float(cal.freqs_hz[-1])],
        "response_db_at_range_ends": [float(cal.response_db[0]), float(cal.response_db[-1])],
        "header": cal.header,
    }


def report(cal_file: str | None = None) -> dict:
    out: dict = {"devices": [], "notes": []}
    umiks = [d for d in list_usb_audio() if umik1.is_umik1(d.vendor_id, d.product_id, d.product)]
    if not umiks:
        out["notes"].append("no miniDSP UMIK-1 (USB 2752:0007) found; check the cable, `lsusb`, and that this host sees USB audio")
    mixer_gain = 0.0
    device_again = None
    for d in umiks:
        g = read_gain(d.card_index)
        ok, note = umik1.mixer_check(g)
        formats = parse_stream_formats(d.stream_info)
        chans = sorted({f.get("channels") for f in formats if f.get("channels")})
        device_again = umik1.analog_gain_db(d.product)
        rep = g.controls.get(umik1.MIXER_CONTROL, "")
        if "db=" in rep and not ok:
            try:
                mixer_gain = float(rep.split("db=")[1].split(";")[0].split("|")[0])
            except ValueError:
                pass
        out["devices"].append({
            "alsa": d.alsa_hw,
            "card_id": d.card_id,
            "usb_path": d.usb_path,
            "product": d.product,
            "analog_gain_db": device_again,
            "usb_serial": d.serial,
            "usb_serial_is_placeholder": umik1.real_serial(d.serial) is None,
            "native_formats": formats,
            "mixer": g.controls or g.error,
            "mixer_ok": ok,
            "mixer_note": note,
            "suggested_settings": {
                "microphone": {"model": "umik-1", "usb_path": d.usb_path},
                "capture": {"container": "int24", "valid_bits": 24, "channels": chans[0] if len(chans) == 1 else "check native_formats",
                            "analysis_channel": 0},
            },
        })
    if len(umiks) > 1:
        out["notes"].append("several UMIK-1 microphones connected: usb_path is required to pick one")
    if cal_file:
        cal = _cal_file_report(Path(cal_file))
        out["calibration_file"] = cal
        if cal["again_db"] is not None and device_again is not None and cal["again_db"] != device_again:
            out["notes"].append(f"calibration file is for AGain {cal['again_db']:g} dB but the microphone reports {device_again} dB: "
                                "they must match (use the file for this gain setting)")
        if cal["sens_factor_db"] is not None:
            est = umik1.sensitivity_estimate(cal["sens_factor_db"], mixer_gain)
            out["web_app"] = {
                "measurement_profile": {"microphone_model": "miniDSP UMIK-1", "microphone_serial": cal["serial"],
                                        "sample_rate_hz": 48000, "gain_description": f"UMIK-1 analog {device_again if device_again is not None else cal['again_db']} dB; ALSA Mic 0.00 dB"},
                "calibration_record": {
                    "calibration_state": "estimated",
                    "reference_method": "UMIK-1 calibration file Sens Factor (REW convention), not yet confirmed with a calibrator",
                    "sensitivity_dbfs_at_94db": est["sensitivity_dbfs_at_94db"],
                    "gain_configuration": f"analog {device_again if device_again is not None else cal['again_db']} dB; Mic capture 0.00 dB",
                    "attach": f"{Path(cal_file).name} as 'Microphone frequency-response file'",
                },
                "estimate": est,
            }
            out["notes"].append("the sensitivity above is an estimate: after a 94 dB / 1 kHz calibrator check (calibration-check), "
                                "create a 'calibrated' record with the measured value")
    return out
