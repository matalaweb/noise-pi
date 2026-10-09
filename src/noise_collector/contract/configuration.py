"""Server configuration (``GET /api/v1/device/configuration``) and the collector's effective configuration.

Authoritative contract: ``contract/upstream/device-api-v1.yaml`` (Laravel ``docs/openapi``).

* ``canonical_json``/``document_hash`` reproduce ``App\\Support\\CanonicalJson``: keys sorted by byte
  order recursively, list order preserved, no whitespace, unescaped ``/`` and Unicode, floats in
  PHP's shortest round-trip form with a preserved ``.0`` (PHP exponent spelling), and empty
  objects/arrays both encoded as ``[]`` (PHP cannot tell them apart after decoding).
* ``translate`` turns the server document + provenance + owner-controlled local inputs into the
  ``DeviceConfiguration`` the engine runs. Anything the collector cannot honour raises
  ``ConfigRejected`` so the previous valid configuration stays active and a rejection is acknowledged.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

METRICS = ("laeq_db", "lafmax_db", "lceq_db", "lcpeak_db", "low_frequency_leq_db", "rms_dbfs")
COMPUTED_METRICS = ("laeq_db", "lafmax_db", "lceq_db", "low_frequency_leq_db", "rms_dbfs")  # lcpeak not validated
ABSOLUTE_METRICS = tuple(m for m in METRICS if m != "rms_dbfs")
RECORDING_FORMATS = ("audio/wav", "audio/flac")


class ConfigRejected(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------- canonical hash


def _php_float(v: float) -> str:
    """PHP ``json_encode`` (serialize_precision=-1, JSON_PRESERVE_ZERO_FRACTION) spelling of a float.

    Same shortest round-trip digits as Python ``repr``; PHP uses exponent notation only for
    decimal exponents < -4 or >= 17 (Python switches at 16), writes ``1.0e-5``/``2.5e+20``.
    Verified against PHP 8 in tests/unit/test_canonical_json.py.
    """
    from decimal import Decimal

    if not math.isfinite(v):
        raise ValueError("non-finite number")
    r = repr(v)
    if "e" not in r:
        return r if "." in r else r + ".0"
    mant, e = r.split("e")
    exp = int(e)
    if -4 <= exp < 17:
        plain = format(Decimal(r), "f")
        return plain if "." in plain else plain + ".0"
    if "." not in mant:
        mant += ".0"
    return f"{mant}e{'+' if exp >= 0 else '-'}{abs(exp)}"


def _php_string(s: str) -> str:
    out = json.dumps(s, ensure_ascii=False)
    return out.replace(chr(0x2028), "\\u2028").replace(chr(0x2029), "\\u2029")


def _encode(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _php_float(value)
    if isinstance(value, str):
        return _php_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_encode(v) for v in value) + "]"
    if isinstance(value, dict):
        if not value:
            return "[]"
        items = sorted(value.items(), key=lambda kv: kv[0].encode("utf-8"))
        return "{" + ",".join(_php_string(k) + ":" + _encode(v) for k, v in items) + "}"
    raise TypeError(f"cannot canonicalize {type(value).__name__}")


def canonical_json(value: Any) -> bytes:
    return _encode(value).encode("utf-8")


def document_hash(doc: Any) -> str:
    return hashlib.sha256(canonical_json(doc)).hexdigest()


# Fields that DeviceConfigurationService::buildDocument() casts to PHP float before hashing. The
# served document is decoded from a MySQL JSON column, where 15.0 comes back as 15, so these must be
# re-typed to reproduce the server's publish-time hash (reported upstream; see contract/README.md).
CONFIG_FLOAT_FIELDS = (("detection", "absolute", "level_db"), ("detection", "baseline_relative", "delta_db"))


def configuration_hash(doc: dict) -> str:
    import copy

    typed = copy.deepcopy(doc)
    for path in CONFIG_FLOAT_FIELDS:
        node: Any = typed
        for key in path[:-1]:
            node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, dict) and isinstance(node.get(path[-1]), int) and not isinstance(node.get(path[-1]), bool):
            node[path[-1]] = float(node[path[-1]])
    return document_hash(typed)


# ---------------------------------------------------------------------------- server document


class Server(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ChannelEntry(Server):
    channel: str
    enabled: bool = True
    metrics: list[str] = Field(default_factory=list)
    bands_enabled: bool = False
    measurement_profile_id: str | None
    deployment_id: str | None
    calibration_id: str | None = None
    calibration_state: Literal["uncalibrated", "estimated", "calibrated"] | None = None


class RecordingDoc(Server):
    enabled: bool = True
    format: Literal["audio/wav", "audio/flac"] = "audio/wav"
    pre_roll_seconds: int = 10
    post_roll_seconds: int = 30
    max_segment_duration_seconds: int = 600


class AbsoluteRuleDoc(Server):
    enabled: bool = False
    metric: str = "lafmax_db"
    level_db: float | None = None


class RelativeRuleDoc(Server):
    enabled: bool = False
    metric: str = "laeq_db"
    delta_db: float | None = None
    baseline_window_seconds: int = 300


class DetectionDoc(Server):
    rule_version: str = "v1"
    absolute: AbsoluteRuleDoc = AbsoluteRuleDoc()
    baseline_relative: RelativeRuleDoc = RelativeRuleDoc()
    min_event_duration_ms: int = 1000
    merge_gap_ms: int = 5000
    observation_period_until: str | None = None


class RetentionDoc(Server):
    measurement_days: int = 7
    audio_days: int = 7
    max_disk_usage_percent: int = 80


class ConfigurationDocument(Server):
    schema_version: Literal[1]
    device_id: str
    revision: int = Field(ge=1)
    reporting_interval_seconds: int = 30
    heartbeat_interval_seconds: int = 60
    measurement_interval_ms: Literal[1000] = 1000
    channels: list[ChannelEntry]
    recording: RecordingDoc = RecordingDoc()
    detection: DetectionDoc = DetectionDoc()
    local_retention: RetentionDoc = RetentionDoc()


class ProvProfile(Server):
    id: str
    channel: str
    revision: int
    calibration_state: Literal["uncalibrated", "estimated", "calibrated"]
    microphone_model: str | None = None
    microphone_serial: str | None = None
    audio_interface: str | None = None
    gain_description: str | None = None
    supported_metrics: list[str]
    sample_rate_hz: int
    gain_db: float | None = None
    low_frequency_band_hz: list[float] | None = None
    band_definitions: Any = None
    content_hash: str


class ProvDeployment(Server):
    id: str
    revision: int
    effective_at: str
    content_hash: str


class ProvAttachment(Server):
    id: str
    purpose: str
    filename: str
    byte_size: int
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    download_path: str


class ProvCalibration(Server):
    id: str
    channel: str
    revision: int
    calibration_state: Literal["estimated", "calibrated"]
    content_hash: str
    reference_method: str | None = None
    reference_device: str | None = None
    sensitivity_dbfs_at_94db: float | None = None
    sensitivity_mv_per_pa: float | None = None
    reference_level_db: float | None = None
    reference_frequency_hz: float | None = None
    gain_configuration: str | None = None
    application_method: str | None = None
    correction_metadata: Any = None
    performed_at: str | None = None
    attachments: list[ProvAttachment] = Field(default_factory=list)

    def frequency_response_files(self) -> list[ProvAttachment]:
        return [a for a in self.attachments if a.purpose == "frequency_response"]


class Provenance(Server):
    measurement_profiles: list[ProvProfile] = Field(default_factory=list)
    deployments: list[ProvDeployment] = Field(default_factory=list)
    calibrations: list[ProvCalibration] = Field(default_factory=list)


class ConfigurationResult(Server):
    revision: int
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    issued_at: str
    applied_revision: int | None = None
    configuration: dict
    provenance: Provenance = Provenance()


# ---------------------------------------------------------------------------- effective configuration


class Eff(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CaptureSpec(Eff):
    sample_rate: int = 48000
    container: Literal["int16", "int24", "int32"] = "int24"
    valid_bits: int = 24
    channels: int = 1
    analysis_channel: int = 0


class GainSpec(Eff):
    inspectable: bool
    controls: dict[str, str] = Field(default_factory=dict)
    reference_check: str | None = None


class ScaleSpec(Eff):
    method: Literal["manufacturer_sensitivity", "reference_measurement", "comparison_estimate", "server_sensitivity"]
    pa_per_fs: float = Field(gt=0)
    source: str = ""


class ResponseCorrectionSpec(Eff):
    method: Literal["none", "fir_min_phase_v1"] = "none"
    curve: tuple[tuple[float, float], ...] | None = None
    curve_is: Literal["microphone_response", "correction"] = "microphone_response"
    normalise_hz: float = 1000.0
    max_boost_db: float = Field(default=10.0, ge=0, le=20)
    max_cut_db: float = Field(default=20.0, ge=0, le=40)
    valid_range_hz: tuple[float, float] = (20.0, 16000.0)
    source_sha256: str | None = None


class NoiseFloorSpec(Eff):
    laeq_db: float
    method: str


class Profile(Eff):
    profile_id: str
    calibration_id: str | None = None
    mode: Literal["uncalibrated", "estimated", "calibrated"]
    content_hash: str = ""
    calibration_content_hash: str | None = None
    capture: CaptureSpec = CaptureSpec()
    gain: GainSpec = GainSpec(inspectable=False)
    gain_db: float | None = None
    scale: ScaleSpec | None = None
    response_correction: ResponseCorrectionSpec = ResponseCorrectionSpec()
    noise_floor: NoiseFloorSpec | None = None
    supported_metrics: tuple[str, ...] = METRICS
    lf_band_hz: tuple[float, float] = (20.0, 125.0)
    microphone_model: str | None = None
    microphone_serial: str | None = None
    # From the calibration file header when present (UMIK-1: "AGain", "Sens Factor"); the analog
    # gain must match the connected microphone's gain setting.
    calibration_file_again_db: float | None = None
    calibration_file_sens_factor_db: float | None = None

    @property
    def absolute_allowed(self) -> bool:
        return self.mode != "uncalibrated"


class Rule(Eff):
    id: str
    kind: Literal["relative", "absolute"]
    metric: Literal["laeq_db", "lafmax_db", "lceq_db", "low_frequency_leq_db", "rms_dbfs"]
    delta_db: float | None = Field(default=None, ge=0, le=80)
    threshold_db: float | None = Field(default=None, ge=-200, le=250)
    consecutive_seconds: int = Field(default=2, ge=1, le=60)
    enabled: bool = True

    @model_validator(mode="after")
    def _check(self) -> "Rule":
        if self.kind == "relative" and (self.delta_db is None or self.threshold_db is not None):
            raise ValueError("relative rule needs delta_db only")
        if self.kind == "absolute" and (self.threshold_db is None or self.delta_db is not None):
            raise ValueError("absolute rule needs threshold_db only")
        return self


class BaselineSettings(Eff):
    percentile: float = Field(default=20.0, gt=0, lt=100)
    window_seconds: int = Field(default=600, ge=60, le=3600)
    min_eligible_seconds: int = Field(default=120, ge=10, le=3600)
    recovery_seconds: int = Field(default=30, ge=0, le=600)


def _default_rules() -> list[Rule]:
    return [Rule(id="baseline_relative", kind="relative", metric="laeq_db", delta_db=12.0, consecutive_seconds=2)]


class DetectionSettings(Eff):
    rule_version: str = "v1"
    baseline: BaselineSettings = BaselineSettings()
    hysteresis_db: float = Field(default=3.0, ge=0, le=20)
    quiet_seconds: int = Field(default=5, ge=1, le=120)
    rules: list[Rule] = Field(default_factory=_default_rules)
    observation_period_until: str | None = None


class RecordingSettings(Eff):
    enabled: bool = True
    pre_roll_seconds: int = Field(default=10, ge=0, le=120)
    post_roll_seconds: int = Field(default=30, ge=0, le=300)
    segment_max_seconds: int = Field(default=600, ge=10, le=600)
    container: Literal["wav", "flac"] = "wav"

    @property
    def mime_type(self) -> str:
        return "audio/flac" if self.container == "flac" else "audio/wav"


class DeliverySettings(Eff):
    measurement_batch_seconds: int = Field(default=30, ge=5, le=300)
    heartbeat_seconds: int = Field(default=60, ge=10, le=3600)
    config_poll_seconds: int = Field(default=60, ge=30, le=3600)


class RetentionSettings(Eff):
    acknowledged_measurement_days: float = 7.0
    verified_audio_days: float = 1.0
    max_disk_usage_percent: int = 80


class DeviceConfiguration(Eff):
    """What the engine runs. Built only by ``translate`` (or test helpers that call it)."""

    revision: int = Field(ge=1)
    sha256: str
    issued_at: str
    device_id: str = ""
    deployment_id: str
    deployment_content_hash: str = ""
    channel: str
    configured_metrics: tuple[str, ...] = METRICS
    profile: Profile
    detection: DetectionSettings = DetectionSettings()
    recording: RecordingSettings = RecordingSettings()
    delivery: DeliverySettings = DeliverySettings()
    retention: RetentionSettings = RetentionSettings()
    notes: tuple[str, ...] = ()

    def reports(self, metric: str) -> bool:
        return metric in self.configured_metrics and metric in self.profile.supported_metrics


# ---------------------------------------------------------------------------- translation


@dataclass
class LocalCalibration:
    """Owner-provided absolute scale for one server calibration revision."""

    calibration_id: str
    content_hash: str
    pa_per_fs: float | None = None
    sensitivity_dbfs_at_94db: float | None = None
    method: str = "manufacturer_sensitivity"
    response_curve: tuple[tuple[float, float], ...] | None = None
    curve_is: str = "microphone_response"
    curve_sha256: str | None = None
    noise_floor_laeq_db: float | None = None
    noise_floor_method: str | None = None


@dataclass
class LocalInputs:
    channel: str
    capture: CaptureSpec
    expected_gain_controls: dict[str, str] = field(default_factory=dict)
    gain_reference_check: str | None = None
    calibrations: dict[str, LocalCalibration] = field(default_factory=dict)
    max_ack_retention_days: float = 30.0
    max_audio_retention_days: float = 30.0
    # Downloaded calibration files (frequency_response attachments) by sha256.
    asset_dir: "Path | None" = None
    # Which serial-specific file to use when a calibration carries several (UMIK-1: 0deg / 90deg).
    calibration_orientation: str | None = None
    usb_serial: str | None = None
    # The microphone model's gain is read back and verified by built-in rules (UMIK-1 preset).
    builtin_gain_check: bool = False
    acknowledged_retention_days: float | None = None
    verified_audio_retention_hours: float | None = None


def _scale_from(cal: ProvCalibration, local: LocalCalibration | None) -> tuple[ScaleSpec | None, str | None]:
    from ..dsp.calibration import scale_from_sensitivity

    if cal.sensitivity_dbfs_at_94db is not None:
        return ScaleSpec(method="server_sensitivity", pa_per_fs=scale_from_sensitivity(cal.sensitivity_dbfs_at_94db, 94.0),
                         source=f"server calibration {cal.id} r{cal.revision}"), None
    if local is None:
        return None, f"no absolute scale for calibration {cal.id}: SPL fields stay null (add it to calibrations.toml)"
    if local.content_hash != cal.content_hash:
        return None, f"calibrations.toml entry for {cal.id} is pinned to a different calibration revision; SPL fields stay null"
    if local.pa_per_fs is not None:
        pa = local.pa_per_fs
    elif local.sensitivity_dbfs_at_94db is not None:
        pa = scale_from_sensitivity(local.sensitivity_dbfs_at_94db, 94.0)
    else:
        return None, f"calibrations.toml entry for {cal.id} has no scale"
    method = local.method if local.method in ("manufacturer_sensitivity", "reference_measurement", "comparison_estimate") else "manufacturer_sensitivity"
    if cal.calibration_state == "calibrated" and method == "comparison_estimate":
        return None, "a comparison estimate cannot back a calibrated profile"
    return ScaleSpec(method=method, pa_per_fs=pa, source=f"local calibrations.toml for {cal.id}"), None


def _header_db(value: str | None) -> float | None:
    from ..audio.umik1 import _db

    return _db(value)


def _pick_response_file(cal: ProvCalibration, local: LocalInputs, notes: list[str]) -> ProvAttachment | None:
    files = cal.frequency_response_files()
    if not files:
        return None
    if len(files) == 1:
        return files[0]
    from ..audio.umik1 import is_ninety_degree_file

    want = (local.calibration_orientation or "").lower()
    if want in ("90deg", "0deg"):
        matches = [f for f in files if is_ninety_degree_file(f.filename) == (want == "90deg")]
        if len(matches) == 1:
            return matches[0]
    notes.append(f"calibration {cal.id} has {len(files)} frequency-response files; set [microphone] calibration_orientation "
                 "('0deg' or '90deg') to choose one; no response correction applied")
    return None


def _server_response_correction(cal: ProvCalibration, prof: ProvProfile, local: LocalInputs,
                                notes: list[str], header_out: dict | None = None) -> ResponseCorrectionSpec | None:
    """Response correction from the calibration's frequency-response attachment (e.g. a UMIK-1 file)."""
    from ..dsp.calibration import parse_calibration_file

    meta = cal.correction_metadata if isinstance(cal.correction_metadata, dict) else {}
    if str(meta.get("apply_frequency_response", "true")).lower() in ("false", "0", "no"):
        if cal.frequency_response_files():
            notes.append("frequency-response file present but correction_metadata.apply_frequency_response is false")
        return None
    chosen = _pick_response_file(cal, local, notes)
    if chosen is None:
        return None
    path = Path(local.asset_dir) / chosen.sha256 if local.asset_dir else None
    if path is None or not path.exists():
        raise ConfigRejected("asset_unavailable", f"frequency-response file {chosen.filename} has not been downloaded")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != chosen.sha256:
        raise ConfigRejected("asset_hash_mismatch", chosen.filename)
    try:
        curve_file = parse_calibration_file(data)
    except ValueError as exc:
        raise ConfigRejected("invalid_calibration_file", f"{chosen.filename}: {exc}") from exc
    if header_out is not None:
        header_out.update(curve_file.header)
    serno = curve_file.header.get("SERNO")
    if serno and prof.microphone_serial and serno.strip() != prof.microphone_serial.strip():
        raise ConfigRejected("calibration_serial_mismatch",
                             f"{chosen.filename} is for serial {serno}, profile microphone serial is {prof.microphone_serial}")
    curve_is = str(meta.get("curve_is", "microphone_response"))
    if curve_is not in ("microphone_response", "correction"):
        raise ConfigRejected("invalid_calibration_metadata", f"curve_is={curve_is!r}")
    notes.append(f"response correction from {chosen.filename} (sha256 {chosen.sha256[:12]}...)")
    return ResponseCorrectionSpec(
        method="fir_min_phase_v1",
        curve=tuple(zip(map(float, curve_file.freqs_hz), map(float, curve_file.response_db))),
        curve_is=curve_is,  # type: ignore[arg-type]
        source_sha256=chosen.sha256,
    )


def translate(result: dict, local: LocalInputs, *, verify_hash: bool = True) -> DeviceConfiguration:
    try:
        res = ConfigurationResult.model_validate(result)
    except Exception as exc:  # pydantic ValidationError
        raise ConfigRejected("malformed_configuration", str(exc)[:500]) from exc
    if verify_hash and res.sha256 not in (configuration_hash(res.configuration), document_hash(res.configuration)):
        raise ConfigRejected("hash_mismatch", f"canonical hash {configuration_hash(res.configuration)} != {res.sha256}")
    try:
        doc = ConfigurationDocument.model_validate(res.configuration)
    except Exception as exc:
        raise ConfigRejected("unsupported_configuration", str(exc)[:500]) from exc
    if doc.revision != res.revision:
        raise ConfigRejected("revision_mismatch", f"document revision {doc.revision} != {res.revision}")
    entries = [c for c in doc.channels if c.channel == local.channel]
    if not entries:
        raise ConfigRejected("channel_not_configured", f"configuration has no channel {local.channel!r}")
    ch = entries[0]
    if not ch.enabled:
        raise ConfigRejected("channel_disabled", f"channel {local.channel!r} is disabled in this configuration")
    if ch.bands_enabled:
        raise ConfigRejected("unsupported_capability", "third-octave bands are not implemented by this collector")
    prof = next((p for p in res.provenance.measurement_profiles if p.id == ch.measurement_profile_id), None)
    dep = next((d for d in res.provenance.deployments if d.id == ch.deployment_id), None)
    if prof is None or dep is None:
        raise ConfigRejected("provenance_missing", "profile or deployment referenced by the channel is not in provenance")
    if prof.channel != local.channel:
        raise ConfigRejected("provenance_mismatch", "profile channel differs from the local channel")
    if prof.microphone_serial and local.usb_serial and prof.microphone_serial.strip() != local.usb_serial.strip():
        raise ConfigRejected("microphone_serial_mismatch",
                             f"profile microphone serial {prof.microphone_serial} != local microphone serial {local.usb_serial}")
    if prof.sample_rate_hz not in (44100, 48000, 96000):
        raise ConfigRejected("unsupported_sample_rate", f"{prof.sample_rate_hz} Hz has no validated filter coefficients")
    notes: list[str] = []
    scale = None
    cal = None
    if prof.calibration_state != "uncalibrated":
        cal = next((c for c in res.provenance.calibrations if c.id == ch.calibration_id), None)
        if cal is None or cal.calibration_state != prof.calibration_state or cal.channel != local.channel:
            raise ConfigRejected("provenance_mismatch", "calibration missing or inconsistent with the profile")
        lc = local.calibrations.get(cal.id)
        scale, note = _scale_from(cal, lc)
        if note:
            notes.append(note)
        if (prof.calibration_state == "calibrated" and not local.expected_gain_controls and not local.gain_reference_check
                and not local.builtin_gain_check):
            notes.append("calibrated profile without gain read-back or reference check: SPL withheld")
            scale = None
    elif ch.calibration_id is not None:
        raise ConfigRejected("provenance_mismatch", "uncalibrated profile must not reference a calibration")
    lc = local.calibrations.get(cal.id) if cal else None
    correction = ResponseCorrectionSpec()
    noise_floor = None
    cal_header: dict[str, str] = {}
    server_correction = _server_response_correction(cal, prof, local, notes, cal_header) if cal is not None else None
    if server_correction is not None and scale is not None:
        correction = server_correction
    if lc is not None and scale is not None:
        if lc.response_curve and server_correction is None:
            correction = ResponseCorrectionSpec(method="fir_min_phase_v1", curve=lc.response_curve, curve_is=lc.curve_is,  # type: ignore[arg-type]
                                                source_sha256=lc.curve_sha256)
        if lc.noise_floor_laeq_db is not None and lc.noise_floor_method:
            noise_floor = NoiseFloorSpec(laeq_db=lc.noise_floor_laeq_db, method=lc.noise_floor_method)
    lf = tuple(prof.low_frequency_band_hz) if prof.low_frequency_band_hz else (20.0, 125.0)
    if len(lf) != 2 or not (0 < lf[0] < lf[1] < prof.sample_rate_hz / 2):
        raise ConfigRejected("invalid_profile", f"low-frequency band {lf} is not usable")
    profile = Profile(
        profile_id=prof.id,
        calibration_id=cal.id if cal else None,
        mode=prof.calibration_state,
        content_hash=prof.content_hash,
        calibration_content_hash=cal.content_hash if cal else None,
        capture=local.capture.model_copy(update={"sample_rate": prof.sample_rate_hz}),
        gain=GainSpec(inspectable=bool(local.expected_gain_controls) or local.builtin_gain_check,
                      controls=local.expected_gain_controls, reference_check=local.gain_reference_check),
        gain_db=prof.gain_db,
        scale=scale,
        response_correction=correction,
        noise_floor=noise_floor,
        supported_metrics=tuple(m for m in prof.supported_metrics if m in METRICS),
        lf_band_hz=(float(lf[0]), float(lf[1])),
        microphone_model=prof.microphone_model,
        microphone_serial=prof.microphone_serial,
        calibration_file_again_db=_header_db(cal_header.get("AGain")),
        calibration_file_sens_factor_db=_header_db(cal_header.get("Sens Factor")),
    )

    # Detection rules
    det = doc.detection
    consecutive = max(1, math.ceil(det.min_event_duration_ms / 1000))
    quiet = max(1, math.ceil(det.merge_gap_ms / 1000))
    rules: list[Rule] = []
    for kind, rd in (("absolute", det.absolute), ("relative", det.baseline_relative)):
        if not rd.enabled:
            continue
        if rd.metric not in COMPUTED_METRICS:
            raise ConfigRejected("unsupported_trigger_metric", f"{rd.metric} cannot be used as a trigger by this collector")
        if kind == "absolute":
            if det.absolute.level_db is None:
                raise ConfigRejected("invalid_detection", "absolute rule enabled without level_db")
            rules.append(Rule(id="absolute", kind="absolute", metric=rd.metric, threshold_db=det.absolute.level_db,  # type: ignore[arg-type]
                              consecutive_seconds=consecutive))
        else:
            if det.baseline_relative.delta_db is None:
                raise ConfigRejected("invalid_detection", "baseline_relative rule enabled without delta_db")
            rules.append(Rule(id="baseline_relative", kind="relative", metric=rd.metric, delta_db=det.baseline_relative.delta_db,  # type: ignore[arg-type]
                              consecutive_seconds=consecutive))
    window = min(3600, max(60, det.baseline_relative.baseline_window_seconds))
    detection = DetectionSettings(
        rule_version=det.rule_version[:64] or "v1",
        baseline=BaselineSettings(window_seconds=window, min_eligible_seconds=min(120, window // 2)),
        quiet_seconds=quiet,
        rules=rules,
        observation_period_until=det.observation_period_until,
    )
    if not rules:
        notes.append("no detection rule enabled: measurements only")

    rec = doc.recording
    post_roll = max(rec.post_roll_seconds, quiet)
    if post_roll != rec.post_roll_seconds:
        notes.append(f"post-roll raised to {post_roll} s to contain the {quiet} s quiet confirmation")
    try:
        recording = RecordingSettings(enabled=rec.enabled, pre_roll_seconds=rec.pre_roll_seconds, post_roll_seconds=post_roll,
                                      segment_max_seconds=rec.max_segment_duration_seconds,
                                      container="flac" if rec.format == "audio/flac" else "wav")
        delivery = DeliverySettings(measurement_batch_seconds=doc.reporting_interval_seconds,
                                    heartbeat_seconds=doc.heartbeat_interval_seconds)
    except Exception as exc:
        raise ConfigRejected("out_of_bounds", str(exc)[:500]) from exc
    ret = doc.local_retention
    retention = RetentionSettings(
        acknowledged_measurement_days=min(float(ret.measurement_days), local.max_ack_retention_days)
        if local.acknowledged_retention_days is None else local.acknowledged_retention_days,
        verified_audio_days=min(float(ret.audio_days), local.max_audio_retention_days)
        if local.verified_audio_retention_hours is None else local.verified_audio_retention_hours / 24,
        max_disk_usage_percent=max(10, min(95, ret.max_disk_usage_percent)),
    )
    return DeviceConfiguration(
        revision=res.revision,
        sha256=res.sha256,
        issued_at=res.issued_at,
        device_id=doc.device_id,
        deployment_id=dep.id,
        deployment_content_hash=dep.content_hash,
        channel=ch.channel,
        configured_metrics=tuple(m for m in ch.metrics if m in METRICS),
        profile=profile,
        detection=detection,
        recording=recording,
        delivery=delivery,
        retention=retention,
        notes=tuple(notes),
    )
