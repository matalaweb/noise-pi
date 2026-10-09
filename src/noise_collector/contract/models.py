"""Device API wire models, schema_version 1.

Authoritative contract: ``contract/upstream/device-api-v1.yaml`` (vendored from the Laravel app's
``docs/openapi``; see ``contract/upstream/SOURCE``). ``tests/contract`` validates every payload the
collector emits against those upstream JSON Schemas and parses the upstream response fixtures.

Outbound models forbid unknown fields (the server rejects them with 422). Inbound models ignore
unknown fields but require the fields the collector's state machines depend on.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1
REQUEST_ID_HEADER = "X-Request-Id"

WIRE_METRICS = ("laeq_db", "lafmax_db", "lceq_db", "lcpeak_db", "low_frequency_leq_db", "rms_dbfs")
QUALITY_FLAGS = (
    "clipping",
    "audio_dropout",
    "microphone_disconnected",
    "unsynchronized_clock",
    "below_noise_floor",
    "invalid_calibration",
    "processing_error",
    "incomplete_interval",
)
QualityFlag = Literal[
    "clipping", "audio_dropout", "microphone_disconnected", "unsynchronized_clock", "below_noise_floor",
    "invalid_calibration", "processing_error", "incomplete_interval",
]
# Events may also carry ``max_duration_reached`` (ended at detection.max_event_duration_seconds)
# and ``ended_by_operator`` (stopped by the owner on the device).
EventQualityFlag = Literal[
    "clipping", "audio_dropout", "microphone_disconnected", "unsynchronized_clock", "below_noise_floor",
    "invalid_calibration", "processing_error", "incomplete_interval", "max_duration_reached", "ended_by_operator",
]
MetricName = Literal["laeq_db", "lafmax_db", "lceq_db", "lcpeak_db", "low_frequency_leq_db", "rms_dbfs"]
UUID_RE = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
CHANNEL_RE = r"^[A-Za-z0-9._-]{1,32}$"
REASON_RE = r"^[a-z][a-z0-9_]{0,63}$"


class Outbound(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Inbound(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Envelope(Inbound):
    request_id: str | None = None
    server_received_at: str | None = None


class ErrorBody(Inbound):
    code: str
    message: str | None = None
    retry: Literal["backoff", "after_clock_sync", "after_configuration_refresh", "after_correction", "never"] | None = None
    permanent: bool | None = None
    details: dict | None = None


class ErrorEnvelope(Envelope):
    error: ErrorBody


# --- measurements ---------------------------------------------------------------------------


class Band(Outbound):
    center_hz: float
    level_db: float | None
    weighting: Literal["Z", "A", "C"] = "Z"


class MeasurementRecord(Outbound):
    boot_id: str = Field(pattern=UUID_RE)
    sequence: int = Field(ge=0)
    channel: str = Field(pattern=CHANNEL_RE)
    captured_at: str
    duration_ms: Literal[1000] = 1000
    # No deployment_id: the server assigns the placement in effect at captured_at.
    profile_id: str = Field(pattern=UUID_RE)
    calibration_id: str | None
    configuration_revision: int | None = Field(ge=1)  # None: local defaults, no configuration applied
    laeq_db: float | None
    lafmax_db: float | None
    lceq_db: float | None
    lcpeak_db: float | None
    low_frequency_leq_db: float | None
    rms_dbfs: float | None = Field(le=3.02)
    quality_flags: list[QualityFlag]
    null_reasons: dict[MetricName, str] = Field(default_factory=dict)
    bands: list[Band] = Field(default_factory=list)


class MeasurementBatch(Outbound):
    schema_version: Literal[1] = SCHEMA_VERSION
    batch_id: str = Field(pattern=UUID_RE)
    sent_at: str
    records: list[MeasurementRecord] = Field(min_length=1, max_length=300)


class BatchResult(Envelope):
    """2xx only after the batch, rows and derived markers are durably committed."""

    batch_id: str
    status: Literal["accepted"]
    record_count: int
    inserted_count: int
    duplicate_count: int
    replayed: bool
    received_at: str | None = None


# --- events ---------------------------------------------------------------------------------


class Detection(Outbound):
    rule_version: str = Field(min_length=1, max_length=64)
    trigger_metric: MetricName
    trigger_kind: Literal["absolute", "baseline_relative"]
    threshold_db: float | None
    trigger_value_db: float | None
    baseline_db: float | None
    baseline_method: str | None = Field(max_length=255)


class EventSummary(Outbound):
    laeq_db: float | None = None
    lafmax_db: float | None = None
    lceq_db: float | None = None
    lcpeak_db: float | None = None
    low_frequency_leq_db: float | None = None
    rms_dbfs: float | None = None
    duration_ms: int | None = Field(default=None, ge=0)


class EventRecordingInfo(Outbound):
    expected: bool = False
    started_at: str | None = None
    ended_at: str | None = None
    expected_segments: int | None = Field(default=None, ge=1)


class EventRevision(Outbound):
    schema_version: Literal[1] = SCHEMA_VERSION
    event_id: str = Field(pattern=UUID_RE)
    revision: int = Field(ge=1)
    sent_at: str | None = None
    channel: str = Field(pattern=CHANNEL_RE)
    profile_id: str = Field(pattern=UUID_RE)
    calibration_id: str | None
    configuration_revision: int | None = Field(ge=1)
    detection_state: Literal["open", "finalized"]
    started_at: str
    ended_at: str | None
    detection: Detection
    summary: EventSummary
    recording: EventRecordingInfo
    quality_flags: list[EventQualityFlag]


class EventResult(Envelope):
    event_id: str
    revision: int
    outcome: Literal["stored", "duplicate", "stored_superseded"]
    applied_to_projection: bool
    current_revision: int
    detection_state: Literal["open", "finalized"]
    completeness_state: str | None = None
    recording_state: str | None = None


# --- recordings -----------------------------------------------------------------------------


class RecordingDeclaration(Outbound):
    schema_version: Literal[1] = SCHEMA_VERSION
    recording_id: str = Field(pattern=UUID_RE)
    segment_number: int = Field(ge=1, le=1000)
    capture_started_at: str
    duration_ms: int = Field(ge=1, le=600_000)
    mime_type: Literal["audio/wav", "audio/flac"]
    codec: Literal["pcm_s16le", "pcm_s24le", "pcm_s32le", "flac"]
    sample_rate_hz: int = Field(ge=8000, le=384000)
    channel_count: Literal[1] = 1
    bit_depth: Literal[16, 24, 32] | None
    byte_size: int = Field(ge=44, le=104_857_600)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class UploadAttempt(Inbound):
    attempt_id: str
    method: Literal["PUT"] = "PUT"
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    expires_at: str


RecordingStatusValue = Literal["pending", "uploaded", "verifying", "verified", "failed", "missing", "purged"]


class RecordingUploadResult(Envelope):
    recording_id: str
    event_id: str
    segment_number: int
    status: RecordingStatusValue
    verified: bool
    upload: UploadAttempt | None = None


class RecordingCompletion(Outbound):
    schema_version: Literal[1] = SCHEMA_VERSION
    attempt_id: str = Field(pattern=UUID_RE)


class LatestAttempt(Inbound):
    attempt_id: str
    state: str
    expires_at: str | None = None
    failure_reason: str | None = None


class RecordingStatus(Envelope):
    recording_id: str
    event_id: str | None = None
    segment_number: int | None = None
    status: RecordingStatusValue
    verified: bool
    verified_sha256: str | None = None
    verified_at: str | None = None
    failure_reason: str | None = None
    retain_local_copy: bool = True
    latest_attempt: LatestAttempt | None = None


# --- configuration --------------------------------------------------------------------------


class ConfigAck(Outbound):
    schema_version: Literal[1] = SCHEMA_VERSION
    revision: int = Field(ge=1)
    status: Literal["applied", "rejected"]
    reason: str | None = Field(default=None, max_length=2000)
    applied_at: str | None = None
    content_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ConfigAckResult(Envelope):
    revision: int
    status: Literal["applied", "rejected"]
    recorded: bool | None = None
    desired_config_revision: int | None = None
    applied_config_revision: int | None = None


# --- heartbeat ------------------------------------------------------------------------------


class Capabilities(Outbound):
    channels: list[str]
    metrics: list[MetricName]
    third_octave_bands: bool | None = False
    recording_formats: list[Literal["audio/wav", "audio/flac"]] | None = None
    max_sample_rate_hz: int | None = None


class Clock(Outbound):
    sync_state: Literal["synchronized", "unsynchronized", "unknown"]
    offset_ms: int | None = None
    source: str | None = Field(default=None, max_length=64)


class Heartbeat(Outbound):
    schema_version: Literal[1] = SCHEMA_VERSION
    sent_at: str | None
    agent_version: str = Field(max_length=64)
    boot_id: str | None = Field(pattern=UUID_RE)  # null while there is no acquisition session
    uptime_seconds: int | None = Field(ge=0)
    capabilities: Capabilities
    microphone_state: Literal["ok", "disconnected", "error", "unknown"]
    free_disk_bytes: int | None = Field(ge=0)
    total_disk_bytes: int | None = Field(ge=0)
    queued_measurement_count: int | None = Field(ge=0)
    pending_audio_bytes: int | None = Field(ge=0)
    pending_audio_count: int | None = Field(ge=0)
    oldest_pending_capture_at: str | None
    desired_config_revision: int | None = Field(ge=1)
    applied_config_revision: int | None = Field(ge=1)
    clock: Clock
    recent_dropped_intervals: int | None = Field(ge=0)
    last_capture_error: str | None = Field(max_length=2000)


class HeartbeatResult(Envelope):
    desired_config_revision: int | None = None
    applied_config_revision: int | None = None
    configuration_pending: bool = False
    heartbeat_interval_seconds: int | None = None
    reporting_interval_seconds: int | None = None


# ---------------------------------------------------------------------------- device-reported provenance
# POST /api/v1/device/provenance (contract/device-reported-provenance.md)


class ProvenanceAttachment(Outbound):
    purpose: Literal["frequency_response"]
    filename: str = Field(min_length=1, max_length=255)
    media_type: str = Field(max_length=127)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_base64: str


class ProvenanceProfile(Outbound):
    id: str = Field(pattern=UUID_RE)
    channel: str = Field(pattern=CHANNEL_RE)
    name: str | None = Field(max_length=255)
    microphone_model: str = Field(min_length=1, max_length=255)
    microphone_serial: str | None = Field(max_length=255)
    audio_interface: str | None = Field(max_length=255)
    sample_rate_hz: int = Field(gt=0)
    gain_db: float | None
    gain_description: str | None = Field(max_length=255)
    weighting_implementation_version: str = Field(min_length=1, max_length=64)
    filter_implementation_version: str = Field(min_length=1, max_length=64)
    agent_processing_version: str = Field(min_length=1, max_length=64)
    calibration_state: Literal["uncalibrated", "estimated", "calibrated"]
    calibration_application_method: str | None = Field(max_length=255)
    supported_metrics: list[MetricName] = Field(min_length=1)
    low_frequency_lower_hz: float | None
    low_frequency_upper_hz: float | None
    band_centers_hz: list[float]


class ProvenanceCalibration(Outbound):
    id: str = Field(pattern=UUID_RE)
    channel: str = Field(pattern=CHANNEL_RE)
    calibration_state: Literal["estimated", "calibrated"]
    reference_method: str = Field(min_length=1, max_length=255)
    reference_device: str | None = Field(max_length=255)
    reference_level_db: float | None
    reference_frequency_hz: float | None
    sensitivity_mv_per_pa: float | None
    sensitivity_dbfs_at_94db: float | None
    gain_configuration: str | None = Field(max_length=255)
    application_method: str | None = Field(max_length=255)
    performed_at: str | None
    performed_by: str | None = Field(max_length=255)
    notes: str | None
    attachments: list[ProvenanceAttachment] = Field(max_length=4)


class ProvenanceRegistration(Outbound):
    schema_version: Literal[1] = SCHEMA_VERSION
    sent_at: str
    measurement_profiles: list[ProvenanceProfile] = Field(max_length=8)
    calibrations: list[ProvenanceCalibration] = Field(max_length=8)
