"""Server-shaped example configurations for replay, tests and the fake server.

Identities are placeholders shaped like the Laravel app's (UUIDs). A real deployment receives its
deployment/profile/calibration identities from the web app; the collector never invents them.
The example absolute scale (1 Pa RMS at -18 dBFS) is a SYNTHETIC placeholder, not a calibration.
"""

from __future__ import annotations

import copy

from .configuration import (
    CaptureSpec,
    DeviceConfiguration,
    LocalCalibration,
    LocalInputs,
    configuration_hash,
    translate,
)

DEVICE_ID = "5a6b7c8d-9e0f-4a1b-8c2d-3e4f5a6b7c8d"
DEPLOYMENT_ID = "ab6de18e-f8aa-4444-aed3-71d3464f07ea"
PROFILE_IDS = {"calibrated": "2fc235ef-6f5b-48d1-8d35-083dfdd5a6e9", "estimated": "3fc235ef-6f5b-48d1-8d35-083dfdd5a6e9",
               "uncalibrated": "4fc235ef-6f5b-48d1-8d35-083dfdd5a6e9"}
CALIBRATION_IDS = {"calibrated": "0e745c21-fc8f-40da-9113-7a158c913fc6", "estimated": "1e745c21-fc8f-40da-9113-7a158c913fc6"}
CHANNEL = "mic-1"
EXAMPLE_SCALE_PA_PER_FS = 10 ** (18 / 20)  # 1 Pa RMS at -18 dBFS (SYNTHETIC)
ALL_METRICS = ["laeq_db", "lafmax_db", "lceq_db", "lcpeak_db", "low_frequency_leq_db", "rms_dbfs"]


def configuration_result(revision: int = 1, mode: str = "calibrated", *, profile_id: str | None = None,
                         detection: dict | None = None, recording: dict | None = None, metrics: list[str] | None = None,
                         reporting_interval_seconds: int = 30, channel: str = CHANNEL, bands_enabled: bool = False,
                         lf_band: list[float] | None = None, sample_rate_hz: int = 48000,
                         calibration_extra: dict | None = None) -> dict:
    """A ``GET /configuration`` response body shaped exactly like the Laravel app's."""
    pid = profile_id or PROFILE_IDS[mode]
    cid = CALIBRATION_IDS.get(mode)
    supported = ["rms_dbfs"] if mode == "uncalibrated" else list(ALL_METRICS)
    det = {
        "rule_version": "owner-rules-v1",
        "absolute": {"enabled": False, "metric": "lafmax_db", "level_db": None},
        "baseline_relative": {"enabled": True, "metric": "rms_dbfs" if mode == "uncalibrated" else "laeq_db",
                              "delta_db": 12.0, "baseline_window_seconds": 600},
        "min_event_duration_ms": 2000,
        "merge_gap_ms": 5000,
        "observation_period_until": None,
    }
    for k, v in (detection or {}).items():
        det[k] = {**det[k], **v} if isinstance(v, dict) and isinstance(det.get(k), dict) else v
    rec = {"enabled": True, "format": "audio/wav", "pre_roll_seconds": 10, "post_roll_seconds": 30, "max_segment_duration_seconds": 600}
    rec.update(recording or {})
    doc = {
        "schema_version": 1,
        "device_id": DEVICE_ID,
        "revision": revision,
        "reporting_interval_seconds": reporting_interval_seconds,
        "heartbeat_interval_seconds": 60,
        "measurement_interval_ms": 1000,
        "channels": [{
            "channel": channel,
            "enabled": True,
            "metrics": list(metrics) if metrics is not None else supported,
            "bands_enabled": bands_enabled,
            "measurement_profile_id": pid,
            "deployment_id": DEPLOYMENT_ID,
            "calibration_id": cid,
            "calibration_state": mode,
        }],
        "recording": rec,
        "detection": det,
        "local_retention": {"measurement_days": 7, "audio_days": 7, "max_disk_usage_percent": 80},
    }
    calibrations = []
    if cid:
        calibrations.append({"id": cid, "channel": channel, "revision": 1, "calibration_state": mode,
                             "content_hash": "c" * 64, **(calibration_extra or {})})
    return {
        "request_id": "019a0f3d-5f6a-7cad-9e1f-2a3b4c5d6e7f",
        "server_received_at": "2026-10-08T12:16:01.000Z",
        "revision": revision,
        "sha256": configuration_hash(doc),
        "issued_at": "2026-09-01T00:00:00.000Z",
        "applied_revision": None,
        "configuration": doc,
        "provenance": {
            "measurement_profiles": [{
                "id": pid, "channel": channel, "revision": 1, "calibration_state": mode, "supported_metrics": supported,
                "sample_rate_hz": sample_rate_hz, "gain_db": 0.0, "low_frequency_band_hz": lf_band or [20.0, 125.0],
                "band_definitions": None, "content_hash": "a" * 64,
            }],
            "deployments": [{"id": DEPLOYMENT_ID, "revision": 1, "effective_at": "2026-01-01T00:00:00.000Z", "content_hash": "d" * 64}],
            "calibrations": calibrations,
        },
    }


def local_inputs(mode: str = "calibrated", *, with_scale: bool = True, channel: str = CHANNEL,
                 capture: CaptureSpec | None = None, gain_controls: dict | None = None) -> LocalInputs:
    cals = {}
    cid = CALIBRATION_IDS.get(mode)
    if cid and with_scale:
        cals[cid] = LocalCalibration(calibration_id=cid, content_hash="c" * 64, pa_per_fs=EXAMPLE_SCALE_PA_PER_FS,
                                     method="reference_measurement" if mode == "calibrated" else "comparison_estimate")
    return LocalInputs(channel=channel, capture=capture or CaptureSpec(), expected_gain_controls=gain_controls or {},
                       gain_reference_check="SYNTHETIC example: no physical gain" if not gain_controls else None,
                       calibrations=cals)


def example_configuration(revision: int = 1, mode: str = "calibrated", *, local: LocalInputs | None = None,
                          **kw) -> DeviceConfiguration:
    return translate(configuration_result(revision, mode, **kw), local or local_inputs(mode))


def reseal(result: dict) -> dict:
    out = copy.deepcopy(result)
    out["sha256"] = configuration_hash(out["configuration"])
    return out
