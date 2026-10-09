"""Owner-controlled inputs to configuration translation (capture format, gain, calibration scales).

``calibrations.toml`` holds absolute scales for server calibration records, because the web app's
``GET /configuration`` provenance does not (yet) carry sensitivity values. Each entry is pinned to
the server calibration's ``content_hash``; a new calibration revision invalidates it, so a stale
scale can never be applied under a new calibration::

    [[calibration]]
    id = "0e745c21-fc8f-40da-9113-7a158c913fc6"     # server calibration UUID
    content_hash = "<64 hex from GET /configuration provenance>"
    method = "reference_measurement"               # or manufacturer_sensitivity / comparison_estimate
    sensitivity_dbfs_at_94db = -27.4               # RMS dBFS reading for 94 dB SPL @ 1 kHz (or pa_per_fs = ...)
    response_curve_file = "/etc/noise-collector/7103946_90deg.txt"   # optional, frequency response
    curve_is = "microphone_response"
    noise_floor_laeq_db = 21.5                     # optional, with method
    noise_floor_method = "sealed-box self-noise, 2026-10-01"
"""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path

from ..contract.configuration import CaptureSpec, LocalCalibration, LocalInputs
from ..dsp.calibration import parse_calibration_file
from .settings import Settings


def load_calibrations(path: Path | None) -> dict[str, LocalCalibration]:
    if path is None or not Path(path).exists():
        return {}
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    out: dict[str, LocalCalibration] = {}
    for entry in data.get("calibration", []):
        curve = None
        curve_sha = None
        if entry.get("response_curve_file"):
            raw = Path(entry["response_curve_file"]).read_bytes()
            cal = parse_calibration_file(raw)
            curve = tuple(zip(map(float, cal.freqs_hz), map(float, cal.response_db)))
            curve_sha = hashlib.sha256(raw).hexdigest()
        cid = str(entry["id"]).lower()
        out[cid] = LocalCalibration(
            calibration_id=cid,
            content_hash=str(entry["content_hash"]).lower(),
            pa_per_fs=entry.get("pa_per_fs"),
            sensitivity_dbfs_at_94db=entry.get("sensitivity_dbfs_at_94db"),
            method=entry.get("method", "manufacturer_sensitivity"),
            response_curve=curve,
            curve_is=entry.get("curve_is", "microphone_response"),
            curve_sha256=curve_sha,
            noise_floor_laeq_db=entry.get("noise_floor_laeq_db"),
            noise_floor_method=entry.get("noise_floor_method"),
        )
    return out


def local_inputs(settings: Settings) -> LocalInputs:
    c = settings.capture
    return LocalInputs(
        channel=settings.channel.id,
        capture=CaptureSpec(container=c.container, valid_bits=c.valid_bits, channels=c.channels,
                            analysis_channel=c.analysis_channel),
        expected_gain_controls=dict(settings.microphone.expected_gain_controls),
        gain_reference_check=settings.microphone.gain_reference_check,
        calibrations=load_calibrations(settings.paths.calibrations_file),
        max_ack_retention_days=settings.storage.max_acknowledged_retention_days,
        max_audio_retention_days=settings.storage.max_verified_audio_retention_days,
        asset_dir=settings.state_dir / "profiles",
        calibration_orientation=settings.microphone.calibration_orientation,
        builtin_gain_check=settings.microphone.model == "umik-1",
    )
