"""Server configuration (``GET /api/v1/device/configuration``) and the collector's effective configuration.

Authoritative contract: ``contract/upstream/device-api-v1.yaml`` (Laravel ``docs/openapi``).

* ``canonical_json``/``document_hash`` reproduce ``App\\Support\\CanonicalJson``: keys sorted by byte
  order recursively, list order preserved, no whitespace, unescaped ``/`` and Unicode, floats in
  PHP's shortest round-trip form with a preserved ``.0`` (PHP exponent spelling), and empty
  objects/arrays both encoded as ``[]`` (PHP cannot tell them apart after decoding).
* The server document carries operational settings only (intervals, recording, detection,
  retention, enabled metrics). The measurement chain (profile + calibration) is built locally
  (``config/chain.py``) and registered with the server; see contract/device-reported-provenance.md.
* ``parse_document`` validates a server document against owner-controlled local inputs; anything
  the collector cannot honour raises ``ConfigRejected`` so the previous configuration stays active
  and a rejection is acknowledged. ``local_defaults`` is what runs before any configuration is
  published. ``effective`` combines either with the local profile into the ``DeviceConfiguration``
  the engine runs.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
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
    # Older revisions also carry measurement_profile_id / deployment_id / calibration_id /
    # calibration_state; they are ignored (the device reports its own chain).
    channel: str
    enabled: bool = True
    metrics: list[str] = Field(default_factory=list)
    bands_enabled: bool = False


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
    max_event_duration_seconds: int = 600
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


class ConfigurationResult(Server):
    revision: int
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    issued_at: str
    applied_revision: int | None = None
    configuration: dict


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
    # An event still above threshold after this long is ended (``max_duration_reached``) and the
    # baselines are re-learnt, so a lasting level shift (door left open, a fan) cannot keep one
    # event and its recording open indefinitely.
    max_event_seconds: int = Field(default=600, ge=60, le=14400)


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


class Operational(Eff):
    """Operational settings from a server document, or the local defaults (``revision`` None)."""

    revision: int | None = Field(default=None, ge=1)
    sha256: str | None = None
    issued_at: str | None = None
    device_id: str = ""
    channel: str
    configured_metrics: tuple[str, ...] = METRICS
    detection: DetectionSettings = DetectionSettings()
    recording: RecordingSettings = RecordingSettings()
    delivery: DeliverySettings = DeliverySettings()
    retention: RetentionSettings = RetentionSettings()
    notes: tuple[str, ...] = ()

    @property
    def is_local_defaults(self) -> bool:
        return self.revision is None


class DeviceConfiguration(Operational):
    """What the engine runs: operational settings plus the locally built measurement profile."""

    profile: Profile

    def reports(self, metric: str) -> bool:
        return metric in self.configured_metrics and metric in self.profile.supported_metrics

    @property
    def operational(self) -> Operational:
        return Operational.model_validate({k: getattr(self, k) for k in Operational.model_fields})


# ---------------------------------------------------------------------------- translation


@dataclass
class LocalInputs:
    channel: str
    capture: CaptureSpec
    expected_gain_controls: dict[str, str] = field(default_factory=dict)
    gain_reference_check: str | None = None
    max_ack_retention_days: float = 30.0
    max_audio_retention_days: float = 30.0
    # The microphone model's gain is read back and verified by built-in rules (UMIK-1 preset).
    builtin_gain_check: bool = False
    acknowledged_retention_days: float | None = None
    verified_audio_retention_hours: float | None = None
    recording_locally_enabled: bool = True


def _retention(local: LocalInputs, measurement_days: float, audio_days: float, max_disk_percent: int) -> RetentionSettings:
    return RetentionSettings(
        acknowledged_measurement_days=min(float(measurement_days), local.max_ack_retention_days)
        if local.acknowledged_retention_days is None else local.acknowledged_retention_days,
        verified_audio_days=min(float(audio_days), local.max_audio_retention_days)
        if local.verified_audio_retention_hours is None else local.verified_audio_retention_hours / 24,
        max_disk_usage_percent=max(10, min(95, max_disk_percent)),
    )


def local_defaults(local: LocalInputs) -> Operational:
    """Runs until a configuration is published: measurements only, no detection rules."""
    doc = ConfigurationDocument.model_validate({"schema_version": 1, "device_id": "", "revision": 1, "channels": []})
    rec = doc.recording
    return Operational(
        channel=local.channel,
        configured_metrics=METRICS,
        detection=DetectionSettings(rules=[]),
        recording=RecordingSettings(enabled=local.recording_locally_enabled, pre_roll_seconds=rec.pre_roll_seconds,
                                    post_roll_seconds=rec.post_roll_seconds, segment_max_seconds=rec.max_segment_duration_seconds,
                                    container="flac"),
        delivery=DeliverySettings(measurement_batch_seconds=doc.reporting_interval_seconds, heartbeat_seconds=doc.heartbeat_interval_seconds),
        retention=_retention(local, doc.local_retention.measurement_days, doc.local_retention.audio_days,
                             doc.local_retention.max_disk_usage_percent),
        notes=("local defaults: no configuration published yet (measurements only, no detection rules)",),
    )


def parse_document(result: dict, local: LocalInputs, *, verify_hash: bool = True) -> Operational:
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
    notes: list[str] = []

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
        max_event_seconds=min(14400, max(60, det.max_event_duration_seconds)),
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
    return Operational(
        revision=res.revision,
        sha256=res.sha256,
        issued_at=res.issued_at,
        device_id=doc.device_id,
        channel=ch.channel,
        configured_metrics=tuple(m for m in ch.metrics if m in METRICS),
        detection=detection,
        recording=recording,
        delivery=delivery,
        retention=_retention(local, ret.measurement_days, ret.audio_days, ret.max_disk_usage_percent),
        notes=tuple(notes),
    )


def effective(op: Operational, profile: Profile, chain_notes: tuple[str, ...] = ()) -> DeviceConfiguration:
    unsupported = [m for m in op.configured_metrics if m not in profile.supported_metrics]
    notes = list(op.notes) + list(chain_notes)
    if unsupported and not op.is_local_defaults:
        notes.append(f"not reported by this profile: {', '.join(unsupported)}")
    return DeviceConfiguration.model_validate({**{k: getattr(op, k) for k in Operational.model_fields}, "profile": profile,
                                               "notes": tuple(notes)})
