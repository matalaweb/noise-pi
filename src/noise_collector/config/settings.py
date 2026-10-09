"""Locally controlled settings (``/etc/noise-collector/collector.toml``).

These are owner/installer decisions the server can never change: API origin, credentials path,
microphone selector, state directory, storage quotas, local recording permission, and timing
trust policy. Secrets live only in the separate credentials file.
"""

from __future__ import annotations

import os
import stat
import tomllib
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULT_CONFIG_PATH = Path("/etc/noise-collector/collector.toml")


class Local(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PathsSettings(Local):
    state_dir: Path = Path("/var/lib/noise-collector")
    credentials_file: Path = Path("/etc/noise-collector/credentials.toml")
    # Deprecated and ignored: the calibration now lives in [calibration] (docs/calibration.md).
    calibrations_file: Path | None = None


class ServerSettings(Local):
    base_url: str
    trusted_storage_hosts: list[str] = Field(default_factory=list)
    ca_bundle: Path | None = None
    connect_timeout_s: float = Field(default=10.0, gt=0, le=60)
    response_timeout_s: float = Field(default=30.0, gt=0, le=120)
    upload_min_bandwidth_bytes_per_s: int = Field(default=50_000, gt=0)
    max_batches_per_minute: int = Field(default=50, ge=1, le=59)
    allow_insecure_http_for_tests: bool = False

    @field_validator("base_url")
    @classmethod
    def _https(cls, v: str) -> str:
        u = urlparse(v)
        if u.scheme not in ("https", "http") or not u.netloc:
            raise ValueError("base_url must be an absolute https URL")
        if u.path not in ("", "/") or u.query or u.fragment:
            raise ValueError("base_url must be an origin (no path, query or fragment)")
        return f"{u.scheme}://{u.netloc}"

    def origin_ok(self) -> bool:
        return urlparse(self.base_url).scheme == "https" or self.allow_insecure_http_for_tests


class MicrophoneSelector(Local):
    # Known model preset. "umik-1": miniDSP UMIK-1 (USB 2752:0007; placeholder USB serial, so the
    # device is pinned by usb_path or by being the only UMIK-1 connected; see docs/umik1.md).
    model: Literal["umik-1"] | None = None
    usb_vendor_id: str | None = None
    usb_product_id: str | None = None
    usb_serial: str | None = None
    usb_path: str | None = None  # physical port path fallback, e.g. "1-1.2"
    # Mixer read-back the owner verified (copy from `noise-collector devices`). Empty = not inspected.
    expected_gain_controls: dict[str, str] = Field(default_factory=dict)
    # Required for a calibrated profile when gain cannot be read back (spec section 4).
    gain_reference_check: str | None = None
    # Reported in the measurement profile. For a UMIK-1 the model defaults to "miniDSP UMIK-1" and
    # the serial to the calibration file's SERNO (its USB serial is a placeholder).
    microphone_model: str | None = Field(default=None, max_length=255)
    microphone_serial: str | None = Field(default=None, max_length=255)
    audio_interface: str | None = Field(default=None, max_length=255)
    # Deprecated and ignored: [calibration] frequency_response_file names the file directly.
    calibration_orientation: Literal["0deg", "90deg"] | None = None

    @field_validator("usb_vendor_id", "usb_product_id", "usb_serial", "usb_path", "microphone_model", "microphone_serial",
                     "audio_interface")
    @classmethod
    def _blank(cls, v: str | None) -> str | None:
        return (v or "").strip() or None

    def ids(self) -> tuple[str | None, str | None]:
        if self.model == "umik-1":
            return self.usb_vendor_id or "2752", self.usb_product_id or "0007"
        return self.usb_vendor_id, self.usb_product_id

    def is_configured(self) -> bool:
        vid, pid = self.ids()
        return bool((vid and pid) and (self.usb_serial or self.usb_path or self.model))


class CaptureSettings(Local):
    # Native capture format of the microphone (verify with `noise-collector devices`).
    container: Literal["int16", "int24", "int32"] = "int24"
    valid_bits: int = 24
    channels: int = Field(default=1, ge=1, le=8)
    analysis_channel: int = Field(default=0, ge=0)
    latency: Literal["high", "low"] | float = "high"
    buffer_seconds: float = Field(default=2.0, ge=0.5, le=10.0)
    fallback_latency_ms: float = Field(default=0.0, ge=0, le=500)
    reconnect_delays_s: list[float] = Field(default_factory=lambda: [1, 2, 5, 10, 30])
    watchdog_s: float = Field(default=3.0, ge=1, le=60)
    gain_check_interval_s: float = Field(default=30.0, ge=5, le=3600)


class MeasurementSettings(Local):
    """The measurement chain this device reports to the server (its measurement profile)."""

    sample_rate_hz: Literal[44100, 48000, 96000] = 48000
    low_frequency_band_hz: tuple[float, float] = (20.0, 125.0)
    name: str | None = Field(default=None, max_length=255)

    @field_validator("low_frequency_band_hz")
    @classmethod
    def _band(cls, v: tuple[float, float]) -> tuple[float, float]:
        if not 0 < v[0] < v[1]:
            raise ValueError("low_frequency_band_hz must be [lower, upper] with 0 < lower < upper")
        return v


class CalibrationSettings(Local):
    """How absolute levels are obtained; reported to the server as this device's calibration record.

    ``uncalibrated``: dBFS only. ``estimated``: an absolute scale from an undocumented or partial
    chain (for a UMIK-1, the calibration file's Sens Factor). ``calibrated``: a documented chain,
    e.g. a 94 dB / 1 kHz acoustic calibrator check (``noise-collector calibration-check``).
    """

    state: Literal["uncalibrated", "estimated", "calibrated"] = "uncalibrated"
    # RMS dBFS reading for 94 dB SPL at 1 kHz (or the scale in Pa per full scale). For an estimated
    # UMIK-1 chain it defaults to the calibration file's Sens Factor - 30 dB (REW convention).
    sensitivity_dbfs_at_94db: float | None = Field(default=None, ge=-140, le=0)
    pa_per_fs: float | None = Field(default=None, gt=0)
    # Serial-specific microphone frequency-response file (UMIK-1: <serial>.txt or <serial>_90deg.txt
    # to match how the microphone is mounted). Reported to the server with its SHA-256.
    frequency_response_file: Path | None = None
    apply_frequency_response: bool = True
    curve_is: Literal["microphone_response", "correction"] = "microphone_response"
    reference_method: str | None = Field(default=None, max_length=255)
    reference_device: str | None = Field(default=None, max_length=255)
    reference_level_db: float | None = Field(default=94.0, gt=0, le=200)
    reference_frequency_hz: float | None = Field(default=1000.0, gt=0)
    performed_at: str | None = None
    performed_by: str | None = Field(default=None, max_length=255)
    notes: str | None = None
    noise_floor_laeq_db: float | None = None
    noise_floor_method: str | None = None

    @field_validator("reference_method", "reference_device", "performed_at", "performed_by", "notes", "noise_floor_method")
    @classmethod
    def _blank(cls, v: str | None) -> str | None:
        return (v or "").strip() or None


class ChannelSettings(Local):
    # Server channel name (configuration ``channels[].channel``), e.g. "mic-1".
    id: str = Field(default="mic-1", pattern=r"^[A-Za-z0-9._-]{1,32}$")


class RecordingLocal(Local):
    locally_enabled: bool = True


class Capabilities(Local):
    low_frequency_validated: bool = False


class TimingLocal(Local):
    require_clock_sync: bool = True
    step_tolerance_ms: float = Field(default=50.0, ge=5, le=5000)
    max_alignment_error_ms: float = Field(default=100.0, ge=1, le=10_000)
    adc_tolerance_ms: float = 20.0
    fallback_tolerance_ms: float = 60.0


class StorageSettings(Local):
    audio_quota_bytes: int = Field(default=0, ge=0)  # 0: derive from volume size at setup/run time
    measurement_quota_bytes: int = Field(default=0, ge=0)
    reserve_bytes: int = Field(default=0, ge=0)  # 0: max(1 GiB, 10% of volume)
    warning_fraction: float = Field(default=0.8, gt=0, lt=1)
    acknowledged_retention_days: float = Field(default=7.0, ge=0)
    verified_audio_retention_hours: float = Field(default=24.0, ge=0)
    backfill_window_days: float = Field(default=30.0, ge=1, le=30)
    # Upper bounds on server-requested local retention (local settings always win).
    max_acknowledged_retention_days: float = Field(default=30.0, ge=0)
    max_verified_audio_retention_days: float = Field(default=30.0, ge=0)


class LoggingSettings(Local):
    level: str = "INFO"
    max_bytes: int = 5 * 1024 * 1024
    backups: int = 5


class DashboardSettings(Local):
    """Optional local live view (read-only levels/events; never audio)."""

    enabled: bool = False
    bind: str = "127.0.0.1"  # LAN exposure requires an access token
    port: int = Field(default=8765, ge=0, le=65535)  # 0 = ephemeral (tests)
    access_token_file: Path | None = None
    # Explicit owner opt-out: serve a non-loopback bind without a token (trusted private network;
    # the page is read-only and never serves audio). Ignored when access_token_file is set.
    allow_unauthenticated_lan: bool = False
    # Show a "Stop event" button that ends the open event (owner control; off = strictly read-only).
    allow_stop_event: bool = False


class Settings(Local):
    paths: PathsSettings = PathsSettings()
    server: ServerSettings
    microphone: MicrophoneSelector = MicrophoneSelector()
    capture: CaptureSettings = CaptureSettings()
    measurement: MeasurementSettings = MeasurementSettings()
    calibration: CalibrationSettings = CalibrationSettings()
    channel: ChannelSettings = ChannelSettings()
    recording: RecordingLocal = RecordingLocal()
    capabilities: Capabilities = Capabilities()
    timing: TimingLocal = TimingLocal()
    storage: StorageSettings = StorageSettings()
    logging: LoggingSettings = LoggingSettings()
    dashboard: DashboardSettings = DashboardSettings()

    @property
    def state_dir(self) -> Path:
        return self.paths.state_dir

    @property
    def db_path(self) -> Path:
        return self.paths.state_dir / "collector.db"

    @property
    def run_dir(self) -> Path:
        return self.paths.state_dir / "run"


def load_settings(path: Path | None = None) -> Settings:
    p = Path(path or os.environ.get("NOISE_COLLECTOR_CONFIG", DEFAULT_CONFIG_PATH))
    with open(p, "rb") as fh:
        data = tomllib.load(fh)
    return Settings.model_validate(data)


class CredentialError(RuntimeError):
    pass


def load_token(path: Path) -> str:
    """Read the device bearer token from a file only the service account can read."""
    try:
        st = path.stat()
    except FileNotFoundError as exc:
        raise CredentialError(f"credentials file {path} not found") from exc
    if st.st_mode & (stat.S_IROTH | stat.S_IWOTH | stat.S_IWGRP):
        raise CredentialError(f"credentials file {path} must not be world-readable or group/world-writable (mode {oct(st.st_mode & 0o777)})")
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    token = data.get("device_token")
    if not isinstance(token, str) or len(token) < 16:
        raise CredentialError("credentials file must define device_token")
    return token
