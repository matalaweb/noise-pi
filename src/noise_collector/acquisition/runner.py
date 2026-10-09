"""The acquisition process: device supervision, the DSP loop, and durable handoff.

Threads: PortAudio callback (copy only) -> this main DSP loop (decode, engine) -> durability
thread (SQLite + evidence files). The delivery process is separate and talks to this one only
through SQLite (staged configurations, durable rows) and status files.
"""

from __future__ import annotations

import json
import logging
import signal
import threading
import time
import uuid

from .. import __version__
from ..audio.discovery import list_usb_audio, match, parse_stream_formats, portaudio_index
from ..audio.gain import compare, read_gain
from ..audio import umik1
from ..audio.alsa_source import CaptureError
from ..audio.pcm import PcmFormat
from ..config.settings import Settings
from ..config.local_inputs import local_inputs
from ..contract.configuration import ConfigRejected, DeviceConfiguration, translate
from ..storage import StorageState, measure
from ..store.db import connect, get_meta, migrate
from ..store.lock import InstanceLock
from ..timing.clock import SystemClock
from ..timing.mapper import TimingSettings
from ..timeutil import iso_utc
from ..health.status import host_health, os_boot_id, run_dir, write_status
from .durability import DurabilityApplier, QueuedSink, high_water_key
from .engine import AcquisitionEngine, EngineLocalSettings, GainState
from .ops import ConfigApplied
from .recovery import recover_state

log = logging.getLogger(__name__)


def fmt_from_profile(cfg: DeviceConfiguration) -> PcmFormat:
    c = cfg.profile.capture
    return PcmFormat(container=c.container, valid_bits=c.valid_bits, channels=c.channels,
                     sample_rate=c.sample_rate, analysis_channel=c.analysis_channel)


def check_native_format(dev, fmt: PcmFormat) -> None:
    """``hw:`` devices cannot convert: the configured channel count and rate must be native."""
    native = parse_stream_formats(dev.stream_info)
    if not native:
        return  # unknown (no /proc stream info): PortAudio's check_input_settings decides
    if not any(f.get("channels") == fmt.channels and fmt.sample_rate in f.get("rates", []) for f in native):
        offered = ", ".join(f"{f.get('format')} {f.get('channels')}ch @ {f.get('rates')}" for f in native)
        raise CaptureError(f"format mismatch: device offers {offered}; configured {fmt.channels}ch @ {fmt.sample_rate} Hz "
                           "(set [capture] channels to the native count)")


def load_config_row(conn, state: str, settings: Settings) -> tuple[int, DeviceConfiguration] | None:
    """Latest configuration in ``state`` translated against current local inputs (raises ConfigRejected)."""
    row = conn.execute(
        "SELECT revision, document_json FROM configurations WHERE state=? ORDER BY revision DESC LIMIT 1", (state,)
    ).fetchone()
    if row is None:
        return None
    try:
        return row["revision"], translate(json.loads(row["document_json"]), local_inputs(settings), verify_hash=state == "staged")
    except ConfigRejected as exc:
        exc.revision = row["revision"]  # type: ignore[attr-defined]
        raise


class AcquisitionRunner:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.stop_event = threading.Event()
        self.clock = SystemClock()
        self.engine: AcquisitionEngine | None = None
        self.mic_state = "not_configured"
        self.latest_error: str | None = None
        self.device: dict | None = None
        self.capture = None
        self.storage: StorageState | None = None
        self.storage_checked = 0.0
        self.requested_revision: int | None = None
        self.status_path = run_dir(settings.state_dir) / "acquisition-status.json"
        self.started_mono = time.monotonic()
        self.last_block_mono: float | None = None

    # ------------------------------------------------------------------ lifecycle

    def request_stop(self, *_args) -> None:
        self.stop_event.set()

    def run(self) -> int:
        s = self.s
        s.state_dir.mkdir(parents=True, exist_ok=True)
        lock = InstanceLock(s.state_dir, "acquisition").acquire()
        try:
            migrate(s.db_path, backup_dir=s.state_dir / "backups")
            self.conn = connect(s.db_path)
            self.reader = connect(s.db_path)
            recover_state(self.conn, s.state_dir, None)
            # The durability thread owns ``self.conn`` from here on.
            self.conn.close()
            dconn = connect(s.db_path, check_same_thread=False)
            self.applier = DurabilityApplier(dconn, s.state_dir)
            self.sink = QueuedSink(self.applier)
            self.sink.start()
            if threading.current_thread() is threading.main_thread():
                signal.signal(signal.SIGTERM, self.request_stop)
                signal.signal(signal.SIGINT, self.request_stop)
            self._loop()
            return 0
        finally:
            self._shutdown()
            lock.release()

    def _shutdown(self) -> None:
        if self.capture is not None:
            self.capture.stop()
            self.capture = None
        if self.engine is not None:
            self.engine.stop_stream("shutdown")
        if getattr(self, "sink", None) is not None:
            flushed = self.sink.stop(timeout=20.0)
            if not flushed:
                log.error("durability backlog not flushed within 20 s")
            self.applier.close("shutdown")
        self.mic_state = "stopped"
        self._write_status()

    # ------------------------------------------------------------------ configuration

    def _local_engine_settings(self) -> EngineLocalSettings:
        t = self.s.timing
        return EngineLocalSettings(
            channel=self.s.channel.id,
            recording_locally_enabled=self.s.recording.locally_enabled,
            low_frequency_validated=self.s.capabilities.low_frequency_validated,
            timing=TimingSettings(
                adc_tolerance_ms=t.adc_tolerance_ms,
                fallback_tolerance_ms=t.fallback_tolerance_ms,
                step_tolerance_ms=t.step_tolerance_ms,
                max_alignment_error_ms=t.max_alignment_error_ms,
                require_clock_sync=t.require_clock_sync,
            ),
        )

    def _build_engine(self, cfg: DeviceConfiguration) -> AcquisitionEngine:
        hw = get_meta(self.reader, high_water_key(self.s.channel.id))
        return AcquisitionEngine(
            config=cfg,
            local=self._local_engine_settings(),
            fmt=fmt_from_profile(cfg),
            microphone=self.device or {"usb_serial": self.s.microphone.usb_serial},
            sink=self.sink,
            clock=self.clock,
            high_water_second=int(hw) if hw else None,
            os_boot_id=os_boot_id(),
            audio_allowed=self._audio_allowed,
        )

    def _reject(self, cfg_rev: int, code: str, detail: str) -> None:
        log.error("rejecting configuration %s: %s %s", cfg_rev, code, detail)
        self.sink.submit(ConfigApplied(cfg_rev, "rejected", None, code, detail[:1800]))

    def _load(self, state: str) -> tuple[int, DeviceConfiguration] | None:
        try:
            return load_config_row(self.reader, state, self.s)
        except ConfigRejected as exc:
            rev = getattr(exc, "revision", None)
            if state == "staged" and rev is not None and rev != self.requested_revision:
                self.requested_revision = rev
                self._reject(rev, exc.code, exc.detail)
            elif state == "applied":
                self.latest_error = f"applied configuration unusable with current local settings: {exc}"
            return None

    def _poll_config(self) -> None:
        staged = self._load("staged")
        if self.engine is None:
            current = staged or self._load("applied")
            if current is None:
                self.mic_state = "not_configured"
                return
            rev, cfg = current
            try:
                self.engine = self._build_engine(cfg)
            except ValueError as exc:
                if staged:
                    self._reject(rev, "incompatible_configuration", str(exc))
                else:
                    self.latest_error = f"applied configuration unusable: {exc}"
                return
            self.requested_revision = rev
            if staged:
                self.sink.submit(ConfigApplied(rev, "applied", iso_utc(time.time()), None, self.engine.apply_notes(cfg), cfg.sha256))
            return
        if staged is None:
            return
        rev, cfg = staged
        if rev == self.requested_revision or rev <= self.engine.config.revision:
            return
        self.requested_revision = rev
        if fmt_from_profile(cfg) != self.engine.fmt:
            # Capture format change: end the stream and rebuild with the new format.
            log.info("capture format change in revision %s; restarting capture", rev)
            if self.capture is not None:
                self.capture.stop()
                self.capture = None
            self.engine.stop_stream("configuration_format_change")
            try:
                self.engine = self._build_engine(cfg)
            except ValueError as exc:
                self._reject(rev, "incompatible_configuration", str(exc))
                return
            self.sink.submit(ConfigApplied(rev, "applied", iso_utc(time.time()), None, self.engine.apply_notes(cfg), cfg.sha256))
            return
        self.engine.request_config(cfg)

    # ------------------------------------------------------------------ device

    def _open_device(self):
        s = self.s
        assert self.engine is not None
        dev = match(list_usb_audio(), s.microphone)
        # Identity: a real USB serial or the port path pins the microphone (the UMIK-1 reports a
        # placeholder serial; its real serial is checked through the calibration file on the profile).
        check_native_format(dev, self.engine.fmt)
        pa = portaudio_index(dev)
        self.device = dict(dev.identity(), portaudio_index=pa, native_formats=parse_stream_formats(dev.stream_info))
        if umik1.is_umik1(dev.vendor_id, dev.product_id, dev.product):
            self.device["umik1"] = {"analog_gain_db": umik1.analog_gain_db(dev.product), "usb_serial_is_placeholder": umik1.real_serial(dev.serial) is None}
        self.engine.microphone = self.device
        self.dev = dev
        gain = self._gain_state(dev.card_index)
        self.engine.set_gain_state(gain)
        from ..audio.alsa_source import AlsaCapture

        cap = AlsaCapture(pa, self.engine.fmt, latency=s.capture.latency, buffer_seconds=s.capture.buffer_seconds,
                          fallback_latency_ms=s.capture.fallback_latency_ms, wall_minus_mono=self.clock.wall_minus_mono)
        cap.check()
        return dev, cap

    def _gain_state(self, card_index: int) -> GainState:
        assert self.engine is not None
        reading = read_gain(card_index)
        expected = self.engine.profile.gain.controls
        ok, note = compare(expected, reading)
        dev = getattr(self, "dev", None)
        if dev is not None and (self.s.microphone.model == "umik-1" or umik1.is_umik1(dev.vendor_id, dev.product_id, dev.product)):
            u_ok, u_note = umik1.gain_check(reading, dev.product, self.engine.profile.calibration_file_again_db)
            ok = ok and u_ok
            note = "; ".join(n for n in (note, u_note) if n) or None
            return GainState(ok=ok, inspectable=True, observed=dict(reading.controls, analog_gain_db=str(umik1.analog_gain_db(dev.product))),
                             note=note)
        if reading.auto_controls:
            note = (note + "; " if note else "") + f"automatic processing controls present: {reading.auto_controls}"
        return GainState(ok=ok, inspectable=reading.inspectable, observed=reading.controls, note=note)

    def _audio_allowed(self, need: int) -> bool:
        st = self.storage
        return st is None or st.audio_allowed(need)

    # ------------------------------------------------------------------ main loop

    def _wait(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while not self.stop_event.is_set() and time.monotonic() < end:
            self._periodic()
            self.stop_event.wait(min(1.0, max(0.0, end - time.monotonic())))

    def _loop(self) -> None:
        delays = self.s.capture.reconnect_delays_s
        attempt = 0
        while not self.stop_event.is_set():
            self._poll_config()
            if self.engine is None:
                self._wait(1.0)
                continue
            try:
                dev, cap = self._open_device()
            except Exception as exc:  # discovery, PortAudio, permissions
                self.mic_state = "format_mismatch" if "format" in str(exc) else "disconnected"
                self.latest_error = f"{type(exc).__name__}: {exc}"[:300]
                log.warning("microphone unavailable: %s", self.latest_error)
                self.engine.tick()
                self._wait(delays[min(attempt, len(delays) - 1)])
                attempt += 1
                continue
            attempt = 0
            self.capture = cap
            try:
                cap.start()
            except Exception as exc:
                busy = "busy" in str(exc).lower() or "unavailable" in str(exc).lower()
                hint = " (device held by another process: PipeWire/PulseAudio? see docs/umik1.md)" if busy else ""
                self.latest_error = f"stream start failed: {exc}{hint}"[:300]
                self.capture = None
                self._wait(delays[0])
                continue
            self.engine.start_stream(str(uuid.uuid4()))
            self.mic_state = "ok" if self.engine.gain.ok else "gain_mismatch"
            log.info("capture started on %s (%s)", dev.alsa_hw, self.engine.fmt.describe())
            reason = self._capture_loop(cap, dev.card_index)
            cap.stop()
            self.capture = None
            if self.engine is not None:
                self.engine.stop_stream(reason)
            if reason != "shutdown":
                self.mic_state = "disconnected"
                self._wait(delays[0])

    def _capture_loop(self, cap, card_index: int) -> str:
        last_gain = time.monotonic()
        last_periodic = 0.0
        while not self.stop_event.is_set():
            got = False
            for blk in cap.blocks():
                self.engine.on_block(blk)
                got = True
            now = time.monotonic()
            if got:
                self.last_block_mono = now
            if cap.stalled(self.s.capture.watchdog_s):
                self.latest_error = "capture stalled or stream stopped (device unplugged?)"
                log.warning(self.latest_error)
                return "device_lost"
            if now - last_periodic >= 1.0:
                last_periodic = now
                engine_before = self.engine
                self._periodic()
                if self.engine is not engine_before or self.capture is None:
                    return "configuration_format_change"
            if now - last_gain >= self.s.capture.gain_check_interval_s:
                last_gain = now
                g = self._gain_state(card_index)
                self.engine.set_gain_state(g)
                self.mic_state = "ok" if g.ok else "gain_mismatch"
            if not got:
                time.sleep(0.02)
        return "shutdown"

    def _periodic(self) -> None:
        self._poll_config()
        now = time.monotonic()
        if now - self.storage_checked >= 5.0:
            self.storage_checked = now
            try:
                self.storage = measure(self.s.state_dir, self.s.storage)
            except OSError as exc:
                self.latest_error = f"storage check failed: {exc}"
        self._write_status()

    def _write_status(self) -> None:
        backlog = self.sink.backlog() if getattr(self, "sink", None) else None
        cap = self.capture
        critical = bool(backlog and backlog["failure"]) or (self.storage is not None and self.storage.state == "critical")
        write_status(
            self.status_path,
            {
                "component": "acquisition",
                "agent_version": __version__,
                "uptime_s": time.monotonic() - self.started_mono,
                "microphone_state": self.mic_state,
                "device": self.device,
                "latest_capture_error": self.latest_error,
                "last_block_mono_age_s": (time.monotonic() - self.last_block_mono) if self.last_block_mono else None,
                "last_durable_measurement_second": getattr(getattr(self, "applier", None), "last_measurement_second", None),
                "last_durable_commit_age_s": (time.monotonic() - self.applier.last_measurement_commit_mono)
                if getattr(self, "applier", None) and self.applier.last_measurement_commit_mono else None,
                "durability": backlog,
                "durable_capture": "critical" if critical else "ok",
                "capture_buffer": {
                    "headroom_fraction": cap.buffer.headroom_fraction(),
                    "overflow_frames": cap.buffer.overflow_frames,
                    "driver_overflows": cap.buffer.driver_overflows,
                    "callbacks": cap.buffer.callbacks,
                    "callback_errors": cap.callback_errors,
                    "latency_s": cap.latency_s(),
                    "stream_to_mono_offset_s": cap.stream_to_mono,
                    "callback_offset_spread_ms": cap.offset_spread_ms(),
                    "channel_blocks": cap.channel_blocks,
                    "channel_mismatch_blocks": cap.channel_mismatch_blocks,
                    "channel_max_abs_diff": cap.channel_max_diff,
                } if cap is not None else None,
                "engine": self.engine.status() if self.engine else None,
                "clock": self.clock.status().__dict__,
                "storage": self.storage.to_dict() if self.storage else None,
                "host": host_health(),
            },
        )


def main(settings: Settings) -> int:
    return AcquisitionRunner(settings).run()
