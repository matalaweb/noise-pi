"""Example configurations for replay, tests and the fake server.

``configuration_result`` is a ``GET /configuration`` body shaped like the Laravel app's (operational
settings only). ``example_profile`` is a measurement profile like the one ``config/chain.py`` builds
from local settings. Identities are placeholders. The example absolute scale (1 Pa RMS at -18 dBFS)
is a SYNTHETIC placeholder, not a calibration.
"""

from __future__ import annotations

import copy

from .configuration import (
    COMPUTED_METRICS,
    CaptureSpec,
    DeviceConfiguration,
    GainSpec,
    LocalInputs,
    Profile,
    ResponseCorrectionSpec,
    ScaleSpec,
    configuration_hash,
    effective,
    parse_document,
)

DEVICE_ID = "5a6b7c8d-9e0f-4a1b-8c2d-3e4f5a6b7c8d"
PROFILE_IDS = {"calibrated": "2fc235ef-6f5b-48d1-8d35-083dfdd5a6e9", "estimated": "3fc235ef-6f5b-48d1-8d35-083dfdd5a6e9",
               "uncalibrated": "4fc235ef-6f5b-48d1-8d35-083dfdd5a6e9"}
CALIBRATION_IDS = {"calibrated": "0e745c21-fc8f-40da-9113-7a158c913fc6", "estimated": "1e745c21-fc8f-40da-9113-7a158c913fc6"}
CHANNEL = "mic-1"
EXAMPLE_SCALE_PA_PER_FS = 10 ** (18 / 20)  # 1 Pa RMS at -18 dBFS (SYNTHETIC)
ALL_METRICS = ["laeq_db", "lafmax_db", "lceq_db", "lcpeak_db", "low_frequency_leq_db", "rms_dbfs"]


def configuration_result(revision: int = 1, mode: str = "calibrated", *, detection: dict | None = None,
                         recording: dict | None = None, metrics: list[str] | None = None,
                         reporting_interval_seconds: int = 30, channel: str = CHANNEL, bands_enabled: bool = False) -> dict:
    """A ``GET /configuration`` response body shaped exactly like the Laravel app's."""
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
        }],
        "recording": rec,
        "detection": det,
        "local_retention": {"measurement_days": 7, "audio_days": 7, "max_disk_usage_percent": 80},
    }
    return {
        "request_id": "019a0f3d-5f6a-7cad-9e1f-2a3b4c5d6e7f",
        "server_received_at": "2026-10-08T12:16:01.000Z",
        "revision": revision,
        "sha256": configuration_hash(doc),
        "issued_at": "2026-09-01T00:00:00.000Z",
        "applied_revision": None,
        "configuration": doc,
    }


def example_profile(mode: str = "calibrated", *, with_scale: bool = True, capture: CaptureSpec | None = None,
                    gain_controls: dict | None = None, lf_band: tuple[float, float] = (20.0, 125.0),
                    response_correction: ResponseCorrectionSpec | None = None, **extra) -> Profile:
    scale = None
    if mode != "uncalibrated" and with_scale:
        scale = ScaleSpec(method="reference_measurement" if mode == "calibrated" else "comparison_estimate",
                          pa_per_fs=EXAMPLE_SCALE_PA_PER_FS, source="SYNTHETIC example")
    return Profile(
        profile_id=PROFILE_IDS[mode],
        calibration_id=CALIBRATION_IDS.get(mode),
        mode=mode,  # type: ignore[arg-type]
        content_hash="a" * 64,
        calibration_content_hash="c" * 64 if mode != "uncalibrated" else None,
        capture=capture or CaptureSpec(),
        gain=GainSpec(inspectable=bool(gain_controls), controls=gain_controls or {},
                      reference_check=None if gain_controls else "SYNTHETIC example: no physical gain"),
        scale=scale,
        response_correction=response_correction or ResponseCorrectionSpec(),
        supported_metrics=("rms_dbfs",) if mode == "uncalibrated" else COMPUTED_METRICS,
        lf_band_hz=lf_band,
        **extra,
    )


def local_inputs(*, channel: str = CHANNEL, capture: CaptureSpec | None = None, gain_controls: dict | None = None) -> LocalInputs:
    return LocalInputs(channel=channel, capture=capture or CaptureSpec(), expected_gain_controls=gain_controls or {},
                       gain_reference_check="SYNTHETIC example: no physical gain" if not gain_controls else None)


def example_configuration(revision: int = 1, mode: str = "calibrated", *, local: LocalInputs | None = None,
                          profile: Profile | None = None, with_scale: bool = True, **kw) -> DeviceConfiguration:
    local = local or local_inputs()
    op = parse_document(configuration_result(revision, mode, **kw), local)
    return effective(op, profile or example_profile(mode, with_scale=with_scale, capture=local.capture,
                                                    gain_controls=local.expected_gain_controls or None))


def reseal(result: dict) -> dict:
    out = copy.deepcopy(result)
    out["sha256"] = configuration_hash(out["configuration"])
    return out


def example_records(profile: Profile) -> tuple[dict, ...]:
    """Registration records (config/chain.py shape) for an example profile, so replayed state can be delivered."""
    from .configuration import document_hash

    prof = {
        "id": profile.profile_id, "channel": CHANNEL, "name": "SYNTHETIC example", "microphone_model": "SYNTHETIC microphone",
        "microphone_serial": None, "audio_interface": None, "sample_rate_hz": profile.capture.sample_rate, "gain_db": None,
        "gain_description": None, "weighting_implementation_version": "example", "filter_implementation_version": "example",
        "agent_processing_version": "example", "calibration_state": profile.mode,
        "calibration_application_method": None, "supported_metrics": list(profile.supported_metrics),
        "low_frequency_lower_hz": profile.lf_band_hz[0], "low_frequency_upper_hz": profile.lf_band_hz[1], "band_centers_hz": [],
    }
    out = [{"kind": "measurement_profile", "id": profile.profile_id, "content_hash": document_hash(prof), "record": prof}]
    if profile.calibration_id:
        cal = {
            "id": profile.calibration_id, "channel": CHANNEL, "calibration_state": profile.mode,
            "reference_method": "SYNTHETIC example scale", "reference_device": None, "reference_level_db": 94.0,
            "reference_frequency_hz": 1000.0, "sensitivity_mv_per_pa": None, "sensitivity_dbfs_at_94db": -18.0,
            "gain_configuration": None, "application_method": None, "performed_at": None, "performed_by": None, "notes": None,
            "attachments": [],
        }
        out.append({"kind": "calibration", "id": profile.calibration_id, "content_hash": document_hash(cal), "record": cal})
    return tuple(out)
