"""The acquisition engine: timing, DSP, intervals, detection, event evidence and config application.

The engine is single-threaded and deterministic: given the same blocks, clock readings and
configuration it emits the same ordered operations. Live capture and file replay drive it the
same way; only the block source and the sink differ.

Per block:
  1. timing observation (may end the session on a discontinuity and start a new one);
  2. append raw analysis-channel samples to the pre-roll ring;
  3. DSP on a float64 copy, split at UTC-second boundaries into interval accumulators;
  4. each closed interval -> measurement op, detector, event actions, pending config;
  5. pump event audio from the ring into the active segment.
"""

from __future__ import annotations

import logging
import math
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from .. import __version__
from ..audio.pcm import PcmFormat
from ..contract.configuration import DeviceConfiguration, Profile
from ..contract.models import EventRevision, MeasurementRecord, QUALITY_FLAGS
from ..detect.detector import (
    Detector,
    DetectorInterval,
    EventAbort,
    EventEnd,
    EventQuiet,
    EventResumed,
    EventStart,
    effective_rules,
)
from ..dsp.calibration import CorrectionSpec, design_correction
from ..dsp.processor import (
    IntervalAccumulator,
    IntervalResult,
    QualitySettings,
    SignalProcessor,
    finalize_interval,
)
from ..evidence.spool import SegmentOpen
from ..timeutil import iso_second, iso_utc
from ..timing.clock import ClockSource
from ..timing.mapper import TimeMapper, TimingSettings
from . import ops

log = logging.getLogger(__name__)

SERVER_RECORDING_LIMIT_BYTES = 100 * 1024 * 1024
SEGMENT_BYTE_LIMIT = 95 * 1024 * 1024  # conservative margin below the server's 100 MiB ceiling
BASELINE_METHOD = "p{pct:g} of eligible 1 s {metric} levels, trailing {window} s (min {min} s)"

# Local diagnostic flags -> server quality flags (contract QualityFlag enum). Flags without a
# mapping stay in local diagnostics only.
WIRE_FLAG = {
    "clipped": "clipping",
    "below_noise_floor": "below_noise_floor",
    "spl_withheld_gain_mismatch": "invalid_calibration",
    "calibration_scale_unavailable": "invalid_calibration",
    "suspect_constant_input": "processing_error",
}
# Why observation of an event stopped -> server flag explaining it (besides incomplete_interval).
INCOMPLETE_FLAG = {
    "device_lost": "microphone_disconnected",
    "capture_buffer_overflow": "audio_dropout",
    "driver_overflow_unknown_loss": "audio_dropout",
    "sample_index_discontinuity": "audio_dropout",
    "timestamp_jump": "audio_dropout",
    "process_interrupted": "processing_error",
}
# Local null reasons -> lowercase identifiers sent in ``null_reasons``.
WIRE_REASON = {
    "capability_disabled": "unsupported",
    "uncalibrated": "uncalibrated",
    "gain_mismatch": "gain_mismatch",
    "zero_energy": "zero_energy",
    "suspect_constant_input": "suspect_input",
    "calibration_scale_unavailable": "calibration_unavailable",
}


@dataclass(frozen=True)
class EngineLocalSettings:
    """Owner-controlled local settings that the server cannot override."""

    channel: str = "mic-1"
    recording_locally_enabled: bool = True
    low_frequency_validated: bool = False
    settle_seconds: float = 2.0
    near_full_scale_dbfs: float = -1.0
    preroll_headroom_seconds: float = 3.0
    max_block_seconds: float = 1.0
    timing: TimingSettings = TimingSettings()


@dataclass(frozen=True)
class CapturedBlock:
    samples: np.ndarray  # int32 analysis-channel samples at valid-bit scale
    first_sample: int  # stream sample index of samples[0]
    mono_time: float  # monotonic time of samples[0]
    ts_source: str  # adc | fallback | synthetic
    wall_minus_mono: float
    lost_before: int = 0
    driver_overflow: bool = False


@dataclass
class GainState:
    ok: bool = True
    inspectable: bool = False
    observed: dict = field(default_factory=dict)
    note: str | None = None


@dataclass
class _Session:
    session_id: str
    stream_id: str
    epoch: int
    start_sample: int
    seq: int = 0
    last_sample: int = 0
    last_utc: float | None = None


@dataclass
class _IntervalInfo:
    second: int
    start_sample: int
    end_sample: int
    result: IntervalResult
    trusted: bool


@dataclass
class _OpenEvent:
    event_id: str
    session_id: str
    start: EventStart
    provenance: dict
    revision: int = 0
    trusted: bool = True
    intervals: list[_IntervalInfo] = field(default_factory=list)
    flags: set[str] = field(default_factory=set)
    rec_start_sample: int | None = None
    preroll_shortfall_samples: int = 0
    seg_number: int = 0
    seg_id: str | None = None
    seg_start: int = 0
    written_through: int = 0
    rec_end_sample: int | None = None
    recording_stop_reason: str | None = None
    segments: list[str] = field(default_factory=list)
    provisional_end: int | None = None
    rollover_pending: bool = False
    end_close: tuple[str, bool] = ("post_roll_complete", False)


def _energy_mean(values: list[float]) -> float | None:
    if not values:
        return None
    return 10 * math.log10(sum(10 ** (v / 10) for v in values) / len(values))


class AcquisitionEngine:
    def __init__(
        self,
        *,
        config: DeviceConfiguration,
        local: EngineLocalSettings,
        fmt: PcmFormat,
        microphone: dict,
        sink: ops.Sink,
        clock: ClockSource,
        gain: GainState | None = None,
        high_water_second: int | None = None,
        os_boot_id: str | None = None,
        audio_allowed: Callable[[int], bool] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.local = local
        self.fmt = fmt
        self.microphone = microphone
        self.sink = sink
        self.clock = clock
        self.gain = gain or GainState()
        self.high_water = high_water_second
        self.os_boot_id = os_boot_id
        self.audio_allowed = audio_allowed or (lambda _bytes: True)
        self.new_id = id_factory or (lambda: str(uuid.uuid4()))
        self.fs = fmt.sample_rate
        self.quality = QualitySettings(near_full_scale_dbfs=local.near_full_scale_dbfs)
        self.near_fs_threshold = int(math.ceil(fmt.full_scale * 10 ** (local.near_full_scale_dbfs / 20)))
        self.mapper = TimeMapper(self.fs, local.timing)
        self.session: _Session | None = None
        self.stream_id: str | None = None
        self.epoch_counter = 0
        self.history: deque[_IntervalInfo] = deque(maxlen=128)
        self.cur: IntervalAccumulator | None = None
        self.cur_end = 0
        self.cur_trusted = True
        self.next_k: int | None = None
        self.next_start: int | None = None
        self.event: _OpenEvent | None = None
        self.pending_config: DeviceConfiguration | None = None
        self.config = config
        self.counters: dict[str, int] = {}
        self._check_compatible(config.profile)
        self._activate(config.profile)
        self.detector = self._new_detector()
        self.ring = self._new_ring()
        self.last_interval_end: int | None = None

    # ------------------------------------------------------------------ configuration

    @property
    def profile(self) -> Profile:
        return self.config.profile

    def _check_compatible(self, profile: Profile) -> None:
        c = profile.capture
        f = self.fmt
        if (c.sample_rate, c.valid_bits, c.channels, c.analysis_channel) != (f.sample_rate, f.valid_bits, f.channels, f.analysis_channel):
            raise ValueError(f"profile capture {c.model_dump()} does not match device format {f.describe()}")

    def _correction_taps(self, profile: Profile) -> np.ndarray | None:
        rc = profile.response_correction
        if rc.method == "none":
            return None
        assert rc.curve
        spec = CorrectionSpec(
            curve_freqs_hz=tuple(p[0] for p in rc.curve),
            curve_db=tuple(p[1] for p in rc.curve),
            curve_is=rc.curve_is,
            normalise_hz=rc.normalise_hz,
            max_boost_db=rc.max_boost_db,
            max_cut_db=rc.max_cut_db,
            valid_range_hz=rc.valid_range_hz,
        )
        filt = design_correction(spec, self.fs)
        if filt.max_error_db > spec.tolerance_db:
            raise ValueError(f"correction filter error {filt.max_error_db:.2f} dB exceeds {spec.tolerance_db} dB")
        return filt.taps

    def _activate(self, profile: Profile) -> None:
        scale = profile.scale.pa_per_fs if profile.scale is not None else None
        self.processor = SignalProcessor(self.fmt, scale, self._correction_taps(profile), self.local.settle_seconds,
                                         lf_band_hz=profile.lf_band_hz)
        self.spl_allowed = self.gain.ok or not self.gain.inspectable

    def _new_detector(self) -> Detector:
        cfg = self.config
        rules, self.disabled_rules = effective_rules(cfg.detection.rules, self.profile.mode, self.local.low_frequency_validated,
                                                     self.profile.scale is not None)
        return Detector(settings=cfg.detection, rules=rules, post_roll_seconds=cfg.recording.post_roll_seconds)

    def _new_ring(self):
        from ..audio.ring import PcmRing

        rec = self.config.recording
        max_confirm = max((r.consecutive_seconds for r in self.config.detection.rules), default=2)
        seconds = rec.pre_roll_seconds + max_confirm + self.local.preroll_headroom_seconds + self.local.max_block_seconds
        return PcmRing(int(seconds * self.fs))

    def request_config(self, cfg: DeviceConfiguration) -> None:
        """Apply ``cfg`` at the next complete measurement boundary (or immediately when idle)."""
        self.pending_config = cfg
        if self.session is None:
            self._apply_pending(at_utc=None)

    def _apply_pending(self, at_utc: float | None) -> None:
        cfg = self.pending_config
        if cfg is None:
            return
        self.pending_config = None
        old = self.config
        try:
            self._check_compatible(cfg.profile)
            if cfg.channel != self.config.channel:
                raise ValueError(f"configuration channel {cfg.channel} != engine channel {self.config.channel}")
            if cfg.recording.post_roll_seconds < cfg.detection.quiet_seconds:
                raise ValueError("post_roll_seconds must be >= detection.quiet_seconds")
            new_taps_check = self._correction_taps(cfg.profile)  # validates the design before switching
            del new_taps_check
        except ValueError as exc:
            self.sink.submit(ops.ConfigApplied(cfg.revision, "rejected", None, "incompatible_configuration", str(exc)[:500],
                                               cfg.sha256))
            return
        forced = cfg.profile != old.profile or cfg.deployment_id != old.deployment_id
        ring_change = cfg.recording.pre_roll_seconds != old.recording.pre_roll_seconds
        self.config = cfg
        if forced:
            self._split("configuration_split")
            self._activate(self.profile)
            self.processor.reset(self._position())
            self.detector = self._new_detector()
        else:
            rules, self.disabled_rules = effective_rules(cfg.detection.rules, self.profile.mode, self.local.low_frequency_validated,
                                                         self.profile.scale is not None)
            self.detector.update(cfg.detection, rules, cfg.recording.post_roll_seconds)
        if ring_change and self.event is None:
            pos = self._position()
            self.ring = self._new_ring()
            self.ring.reset(pos)
        if self.event is not None and not self._recording_enabled():
            self._stop_recording("recording_disabled")
        when = iso_utc(at_utc) if at_utc is not None else iso_utc(self.clock.wall_minus_mono() + _mono_now())
        self.sink.submit(ops.ConfigApplied(cfg.revision, "applied", when, None, self.apply_notes(cfg), cfg.sha256))

    def apply_notes(self, cfg: DeviceConfiguration) -> str | None:
        """Human-readable notes sent as the acknowledgment ``reason`` for an applied revision."""
        notes = list(cfg.notes)
        if cfg.recording.enabled and not self.local.recording_locally_enabled:
            notes.append("recording disabled locally by the owner; measurements continue")
        if self.disabled_rules:
            notes.append("rules not active: " + ", ".join(f"{k} ({v})" for k, v in sorted(self.disabled_rules.items())))
        return "; ".join(notes)[:2000] if notes else None

    def _recording_enabled(self) -> bool:
        return self.config.recording.enabled and self.local.recording_locally_enabled

    def set_gain_state(self, gain: GainState) -> None:
        """Update inspected gain. A mismatch withholds SPL (flag ``invalid_calibration``) under the
        same profile; it never relabels or rescales data, and never switches calibration state."""
        prev_ok = self.gain.ok
        self.gain = gain
        self.spl_allowed = gain.ok or not gain.inspectable
        if gain.ok != prev_ok:
            self._count("gain_state_changes", error=gain.note)

    # ------------------------------------------------------------------ sessions

    def _position(self) -> int:
        return self.ring.end

    def start_stream(self, stream_id: str | None = None) -> None:
        self.stop_stream("stream_restart")
        self.stream_id = stream_id or self.new_id()

    def _begin_session(self, blk: CapturedBlock) -> None:
        assert self.stream_id is not None
        self.epoch_counter += 1
        self.mapper.reset()
        self.mapper.observe(blk.first_sample, blk.mono_time, blk.ts_source, blk.wall_minus_mono)
        self.session = _Session(self.new_id(), self.stream_id, self.epoch_counter, blk.first_sample, last_sample=blk.first_sample)
        self.processor.reset(blk.first_sample)
        self.ring.reset(blk.first_sample)
        self.detector = self._new_detector()
        self.cur = None
        self.next_k = self.next_start = None
        utc0 = self.mapper.utc_of_sample(blk.first_sample)
        self.session.last_utc = utc0
        self.sink.submit(
            ops.SessionStart(
                session_id=self.session.session_id,
                channel=self.config.channel,
                stream_id=self.stream_id,
                timing_epoch=self.epoch_counter,
                start_sample=blk.first_sample,
                started_mono=blk.mono_time,
                started_utc=iso_utc(utc0),
                microphone=self.microphone,
                pcm_format=self.fmt.describe(),
                gain={"ok": self.gain.ok, "inspectable": self.gain.inspectable, "observed": self.gain.observed, "note": self.gain.note},
                profile_id=self.profile.profile_id,
                configuration_revision=self.config.revision,
                os_boot_id=self.os_boot_id,
                agent_version=__version__,
            )
        )
        self._apply_pending(at_utc=utc0)

    def _end_session(self, reason: str) -> None:
        s = self.session
        if s is None:
            return
        self._split(reason)
        if self.cur is not None:
            self.cur.invalid_reasons.add(reason)
            self._omit_current()
        end = self.ring.end
        ended_utc = iso_utc(s.last_utc) if s.last_utc is not None else None
        self.sink.submit(ops.SessionEnd(s.session_id, end, ended_utc, reason, self.mapper.describe()))
        self.session = None

    def stop_stream(self, reason: str) -> None:
        """The capture stream ended (device lost, shutdown, restart)."""
        self._end_session(reason)
        self.stream_id = None

    def tick(self) -> None:
        """Called periodically while no stream is running so configuration can still be applied."""
        if self.session is None:
            self._apply_pending(at_utc=None)

    # ------------------------------------------------------------------ block processing

    def on_block(self, blk: CapturedBlock) -> None:
        if self.stream_id is None:
            self.start_stream()
        if self.session is None:
            self._begin_session(blk)
        elif blk.driver_overflow:
            # The driver lost an unknown number of frames: sample positions no longer map to time.
            self._count("driver_overflows")
            self._restart_session(blk, "driver_overflow_unknown_loss")
        elif blk.first_sample - blk.lost_before != self.ring.end:
            self._restart_session(blk, "sample_index_discontinuity")
        else:
            if blk.lost_before > 0:
                self._hole(self.ring.end, blk.first_sample, "capture_buffer_overflow")
            upd = self.mapper.observe(blk.first_sample, blk.mono_time, blk.ts_source, blk.wall_minus_mono)
            if upd.discontinuity:
                self._count(f"timing_{upd.discontinuity}")
                self._restart_session(blk, upd.discontinuity)
        assert self.session is not None
        self.ring.append(blk.first_sample, blk.samples)
        pb = self.processor.process(blk.samples)
        self._feed(blk.first_sample, pb, blk.ts_source == "fallback")
        self.session.last_sample = blk.first_sample + len(blk.samples)
        self.session.last_utc = self.mapper.utc_of_sample(self.session.last_sample)
        self._pump_recording()

    def _restart_session(self, blk: CapturedBlock, reason: str) -> None:
        """End the session at its last known sample and start a new one at ``blk``.

        The inter-session gap is recorded by the durability layer from the previous session's
        end (time, sample, reason) and the new session's start.
        """
        self._end_session(reason)
        self._begin_session(blk)

    def _hole(self, start: int, end: int, cause: str) -> None:
        """Samples [start, end) were lost inside a session: invalidate and split, keep the session."""
        self._count("lost_frames", end - start)
        if self.cur is not None:
            self.cur.invalid_reasons.add(cause)
            self._omit_current()
        if self.next_k is not None:
            self._detector_loss(self.next_k, cause)
        self.next_k = self.next_start = None
        self._stop_event_on_loss(cause)
        self.sink.submit(
            ops.Gap(
                session_id=self.session.session_id if self.session else None,
                channel=self.config.channel,
                cause=cause,
                start_utc=iso_utc(self.mapper.utc_of_sample(start)),
                end_utc=iso_utc(self.mapper.utc_of_sample(end)),
                start_sample=start,
                end_sample=end,
                clock_quality=None,
                detail={"frames": end - start},
            )
        )
        self.processor.reset(end)
        self.ring.reset(end)

    def _feed(self, first: int, pb, fallback_ts: bool) -> None:
        n = len(pb)
        pos = 0
        while pos < n:
            s = first + pos
            if self.cur is None:
                self._open_interval(s, fallback_ts)
            assert self.cur is not None
            take = min(n - pos, self.cur_end - s)
            self.cur.add(pb.slice(pos, pos + take), self.fmt, self.near_fs_threshold, self.processor.settled_from_sample)
            if fallback_ts:
                self.cur.flags.add("timestamp_fallback")
            pos += take
            if first + pos >= self.cur_end:
                self._close_interval()

    def _open_interval(self, s: int, fallback_ts: bool) -> None:
        aligned = self.next_k is not None and self.next_start == s
        if aligned:
            k = self.next_k
        else:
            k = self.mapper.second_of_sample(s)
        end = self.mapper.boundary(k + 1)
        if end <= s:
            k = self.mapper.second_of_sample(s)
            end = self.mapper.boundary(k + 1)
            aligned = False
        self.cur = IntervalAccumulator(utc_second=k, first_sample=s)
        if not aligned and self.mapper.boundary(k) != s:
            self.cur.invalid_reasons.add("partial_coverage")
        elif aligned:
            expected = self.mapper.rate
            if abs((end - s) - expected) > max(2.0, expected * 0.005):
                self.cur.invalid_reasons.add("timing_adjustment")
        self.cur_end = end

    def _omit_current(self) -> None:
        """Close the open interval early as omitted (sample loss, stop, split)."""
        acc = self.cur
        self.cur = None
        if acc is None or acc.n == 0:
            return
        res = finalize_interval(
            acc, self.fmt, scaled=self.processor.scale is not None, spl_allowed=self.spl_allowed,
            noise_floor_laeq_db=None, quality=self.quality, timestamp_fallback=False,
        )
        self._emit_measurement(res, acc.first_sample + acc.n, trusted=False, note="interval_incomplete")

    def _close_interval(self) -> None:
        acc = self.cur
        assert acc is not None and self.session is not None
        self.cur = None
        nf = self.profile.noise_floor
        res = finalize_interval(
            acc,
            self.fmt,
            scaled=self.processor.scale is not None,
            spl_allowed=self.spl_allowed,
            noise_floor_laeq_db=nf.laeq_db if nf else None,
            quality=self.quality,
            timestamp_fallback="timestamp_fallback" in acc.flags,
        )
        st = self.clock.status()
        trusted, note = self.mapper.trusted(st.synchronized, st.est_error_ms)
        end_sample = acc.first_sample + acc.n
        self.next_k = acc.utc_second + 1
        self.next_start = end_sample
        info = self._emit_measurement(res, end_sample, trusted=trusted, note=note)
        self.history.append(info)
        self.last_interval_end = end_sample
        values = {m: res.metrics[m] for m in ("laeq_db", "lafmax_db", "lceq_db", "low_frequency_leq_db", "rms_dbfs")}
        if self.event is not None:
            self.event.intervals.append(info)
            if not trusted:
                self.event.trusted = False
        actions = self.detector.on_interval(
            DetectorInterval(acc.utc_second, res.complete, values, res.baseline_eligible, res.clipped)
        )
        self._handle(actions, info)
        self._apply_pending(at_utc=float(acc.utc_second + 1))

    def _emit_measurement(self, res: IntervalResult, end_sample: int, *, trusted: bool, note: str | None) -> _IntervalInfo:
        assert self.session is not None
        k = res.utc_second
        upload = False
        seq = None
        wire = None
        if res.complete:
            self.session.seq += 1
            seq = self.session.seq
            if trusted and self.high_water is not None and k <= self.high_water:
                trusted, note = False, "utc_overlap"
            upload = trusted
            wire = self._wire_record(res, seq, k)
            if wire is None:
                upload = False
                note = "no_reportable_metric"
            if upload:
                self.high_water = k
        else:
            self._count("dropped_intervals")
        diag = dict(res.diagnostics)
        diag["null_reasons"] = res.null_reasons
        diag["quality_flags"] = res.quality_flags
        diag["timing"] = {"trusted": trusted, "note": note, "rate_hz": self.mapper.rate, "epoch": self.session.epoch}
        self.sink.submit(
            ops.Measurement(
                session_id=self.session.session_id,
                sequence=seq,
                channel=self.config.channel,
                utc_second=k,
                status="complete" if res.complete else "omitted",
                omit_reason=res.omit_reason,
                first_sample=res.first_sample,
                sample_count=res.sample_count,
                timing_trusted=trusted,
                timing_note=note,
                wire=wire,
                diagnostics=diag,
                upload=upload,
            )
        )
        return _IntervalInfo(k, res.first_sample, end_sample, res, trusted)

    def _scale_missing(self) -> bool:
        return self.profile.absolute_allowed and self.processor.scale is None

    def _wire_record(self, res: IntervalResult, seq: int, k: int) -> dict | None:
        """Wire record per the server rules: unsupported metrics null; a supported metric that is
        null carries a ``null_reasons`` entry; flags limited to the contract vocabulary."""
        p = self.profile
        metrics: dict[str, float | None] = {}
        reasons: dict[str, str] = {}
        for m in ("laeq_db", "lafmax_db", "lceq_db", "lcpeak_db", "low_frequency_leq_db", "rms_dbfs"):
            applicable = m in p.supported_metrics and (m == "rms_dbfs" or p.absolute_allowed)
            v = res.metrics.get(m)
            if not applicable:
                metrics[m] = None
                continue
            if not self.config.reports(m):
                metrics[m] = None
                reasons[m] = "not_configured"
                continue
            metrics[m] = v
            if v is None:
                r = res.null_reasons.get(m, "unavailable")
                if m != "rms_dbfs" and self._scale_missing() and r == "uncalibrated":
                    r = "calibration_scale_unavailable"
                reasons[m] = WIRE_REASON.get(r, r)
        flags = {WIRE_FLAG[f] for f in res.quality_flags if f in WIRE_FLAG}
        if self._scale_missing():
            flags.add("invalid_calibration")
        if all(v is None for v in metrics.values()) and not flags:
            return None
        assert self.session is not None
        return MeasurementRecord(
            boot_id=self.session.session_id,
            sequence=seq,
            channel=self.config.channel,
            captured_at=iso_second(k),
            duration_ms=1000,
            deployment_id=self.config.deployment_id,
            profile_id=p.profile_id,
            calibration_id=p.calibration_id,
            configuration_revision=self.config.revision,
            quality_flags=sorted(f for f in flags if f in QUALITY_FLAGS),
            null_reasons=dict(sorted(reasons.items())),
            bands=[],
            **metrics,
        ).model_dump()

    # ------------------------------------------------------------------ events

    def _detector_loss(self, k: int, reason: str) -> None:
        self._handle(self.detector.on_data_loss(k, reason), None)

    def _split(self, reason: str) -> None:
        """Terminate any open event (as incomplete or truncated) because observation stops here."""
        if self.event is not None:
            k = self.next_k if self.next_k is not None else (self.event.intervals[-1].second + 1 if self.event.intervals else self.event.start.start_second)
            self._handle(self.detector.on_data_loss(k, reason), None)
            if self.event is not None:  # detector was not in an event state (should not happen)
                self._finish_event("incomplete", reason, last_observed=k)

    def _stop_event_on_loss(self, cause: str) -> None:
        if self.event is not None:
            self._split(cause)

    def _handle(self, actions, info: _IntervalInfo | None) -> None:
        for a in actions:
            if isinstance(a, EventStart):
                self._start_event(a)
            elif isinstance(a, EventQuiet):
                if self.event:
                    self.event.provisional_end = a.provisional_end_second
            elif isinstance(a, EventResumed):
                if self.event:
                    self.event.provisional_end = None
            elif isinstance(a, EventEnd):
                if self.event is None:
                    continue
                self.event.provisional_end = a.provisional_end_second
                if a.post_roll_truncated:
                    self.event.flags.add("post_roll_truncated")
                    self._stop_recording(a.reason or "post_roll_truncated", at=self.ring.end if info is None else None)
                else:
                    assert info is not None
                    self.event.rec_end_sample = info.end_sample
                    self._pump_recording()
                self._finish_event("complete", None, last_observed=None)
            elif isinstance(a, EventAbort):
                if self.event is None:
                    continue
                self._stop_recording(a.reason)
                self._finish_event("incomplete", a.reason, last_observed=a.last_observed_second)

    def _interval_start_sample(self, k: int) -> int | None:
        for i in self.history:
            if i.second == k:
                return i.start_sample
        return None

    def _start_event(self, a: EventStart) -> None:
        assert self.session is not None
        p = self.profile
        ev = _OpenEvent(
            event_id=self.new_id(),
            session_id=self.session.session_id,
            start=a,
            provenance={
                "deployment_id": self.config.deployment_id,
                "profile_id": p.profile_id,
                "calibration_id": p.calibration_id,
                "configuration_revision": self.config.revision,
            },
        )
        pre = [i for i in self.history if a.start_second <= i.second <= a.confirm_second]
        ev.intervals.extend(pre)
        ev.trusted = all(i.trusted for i in pre)
        self.event = ev
        self._count("events_started")
        self._emit_revision("active", None, None)
        if self._recording_enabled():
            start_sample = self._interval_start_sample(a.start_second)
            if start_sample is None:
                start_sample = self.ring.end
            pre_k = a.start_second - self.config.recording.pre_roll_seconds
            desired = self._interval_start_sample(pre_k)
            if desired is None:
                desired = start_sample - self.config.recording.pre_roll_seconds * self.fs
            rec_start = max(desired, self.ring.start)
            ev.preroll_shortfall_samples = rec_start - desired
            if ev.preroll_shortfall_samples > 0:
                ev.flags.add("preroll_shortfall")
            ev.rec_start_sample = rec_start
            ev.written_through = rec_start
            self._open_segment(rec_start)
        else:
            ev.flags.add("recording_disabled")
        self._pump_recording()

    def _segment_limit_samples(self) -> int:
        by_time = self.config.recording.segment_max_seconds * self.fs
        by_bytes = (SEGMENT_BYTE_LIMIT - 44) // (self.fmt.evidence_bits // 8)
        return min(by_time, by_bytes)

    def _open_segment(self, at: int) -> bool:
        ev = self.event
        assert ev is not None and self.session is not None
        need = self._segment_limit_samples() * (self.fmt.evidence_bits // 8)
        if not self.audio_allowed(need):
            ev.flags.add("audio_coverage_loss")
            ev.recording_stop_reason = "audio_quota"
            self._count("recordings_refused_quota")
            return False
        ev.seg_number += 1
        ev.seg_id = self.new_id()
        ev.seg_start = at
        ev.segments.append(ev.seg_id)
        self.sink.submit(
            ops.SegmentOpenOp(
                SegmentOpen(
                    recording_id=ev.seg_id,
                    event_id=ev.event_id,
                    segment_number=ev.seg_number,
                    session_id=self.session.session_id,
                    start_sample=at,
                    capture_started_at=iso_utc(self.mapper.utc_of_sample(at)),
                    sample_rate=self.fs,
                    bits=self.fmt.evidence_bits,
                    provenance=dict(
                        ev.provenance,
                        timing_epoch=self.session.epoch,
                        stream_id=self.session.stream_id,
                        pcm_format=self.fmt.describe(),
                        preroll_shortfall_samples=ev.preroll_shortfall_samples if ev.seg_number == 1 else 0,
                    ),
                    uploadable=ev.trusted,
                    container=self.config.recording.container if self.fmt.evidence_bits in (16, 24) else "wav",
                )
            )
        )
        return True

    def _close_segment(self, reason: str, incomplete: bool) -> None:
        ev = self.event
        if ev is None or ev.seg_id is None:
            return
        self.sink.submit(ops.SegmentCloseOp(ev.seg_id, ev.written_through, reason, incomplete))
        ev.seg_id = None

    def _pump_recording(self) -> None:
        ev = self.event
        if ev is None or (ev.seg_id is None and not ev.rollover_pending):
            return
        target = self.ring.end if ev.rec_end_sample is None else min(self.ring.end, ev.rec_end_sample)
        limit = self._segment_limit_samples()
        while ev.written_through < target:
            if ev.seg_id is None:
                # Open the next segment lazily, only once there is audio for it.
                ev.rollover_pending = False
                if not self._open_segment(ev.written_through):
                    return
            upto = min(target, ev.seg_start + limit)
            if ev.written_through < self.ring.start:
                # Should not happen with a correctly sized ring; never fabricate the missing span.
                self._count("recording_ring_underrun")
                ev.flags.add("audio_coverage_loss")
                self._close_segment("ring_underrun", incomplete=True)
                ev.recording_stop_reason = "ring_underrun"
                return
            samples = self.ring.read(ev.written_through, upto)
            if not self.sink.submit(ops.AudioOp(ev.seg_id, ev.written_through, samples)):
                ev.flags.add("audio_coverage_loss")
                self._count("audio_ops_dropped_backlog")
                self._close_segment("storage_backlog", incomplete=True)
                ev.recording_stop_reason = "storage_backlog"
                return
            ev.written_through = upto
            if upto - ev.seg_start >= limit:
                self._close_segment("segment_rollover", incomplete=False)
                ev.rollover_pending = True
        if ev.rec_end_sample is not None and ev.written_through >= ev.rec_end_sample:
            ev.rollover_pending = False
            if ev.seg_id is not None:
                self._close_segment(*ev.end_close)

    def _stop_recording(self, reason: str, at: int | None = None) -> None:
        ev = self.event
        if ev is None:
            return
        ev.end_close = (reason, True)
        if ev.seg_id is None and ev.rollover_pending:
            ev.rec_end_sample = at if at is not None else (ev.rec_end_sample or self.ring.end)
            self._pump_recording()
        if ev.seg_id is not None:
            if at is not None:
                ev.rec_end_sample = at
            else:
                ev.rec_end_sample = ev.rec_end_sample or self.ring.end
            self._pump_recording()
            if ev.seg_id is not None:
                self._close_segment(reason, incomplete=True)
        if reason == "recording_disabled":
            ev.flags.add("recording_disabled")
        if ev.recording_stop_reason is None:
            ev.recording_stop_reason = reason

    def _summary(self, ev: _OpenEvent, end_second: int | None) -> tuple[dict, list[_IntervalInfo]]:
        """Energy-weighted equivalent levels and maxima over detection seconds only."""
        det = [i for i in ev.intervals if i.second >= ev.start.start_second and (end_second is None or i.second < end_second)]
        valid = [i for i in det if i.result.complete]

        def vals(key: str) -> list[float]:
            return [i.result.metrics[key] for i in valid if i.result.metrics.get(key) is not None]

        lafmax = vals("lafmax_db")
        raw = {
            "laeq_db": _energy_mean(vals("laeq_db")),
            "lafmax_db": max(lafmax) if lafmax else None,
            "lceq_db": _energy_mean(vals("lceq_db")),
            "lcpeak_db": None,
            "low_frequency_leq_db": _energy_mean(vals("low_frequency_leq_db")),
            "rms_dbfs": _energy_mean(vals("rms_dbfs")),
        }
        p = self.profile
        summary = {m: (v if (m in p.supported_metrics and (m == "rms_dbfs" or p.absolute_allowed)) else None) for m, v in raw.items()}
        summary["duration_ms"] = (end_second - ev.start.start_second) * 1000 if end_second is not None else None
        return summary, det

    def _utc_now_iso(self) -> str:
        return iso_utc(self.mapper.utc_of_sample(self.ring.end)) if self.mapper.anchor_n is not None else iso_utc(
            self.clock.wall_minus_mono() + _mono_now())

    def _emit_revision(self, state: str, termination_reason: str | None, last_observed: int | None) -> None:
        """Emit a complete snapshot. ``state``: active -> open; complete/incomplete -> finalized.

        An incomplete event (observation stopped: data loss, disconnect, split, shutdown) is
        finalized at the *last observed* second and flagged ``incomplete_interval`` (plus
        ``audio_dropout``/``microphone_disconnected`` when that was the cause). The server requires
        ``ended_at`` on finalized events; the flags state that this is where observation ended,
        not that the noise stopped.
        """
        ev = self.event
        assert ev is not None
        ev.revision += 1
        final = state != "active"
        end_second = ev.provisional_end if state == "complete" else last_observed
        if final and end_second is not None:
            end_second = max(end_second, ev.start.start_second)
        summary, det = self._summary(ev, end_second) if final else (None, [])
        flags: set[str] = set()
        for i in (det if final else ev.intervals):
            if i.result.clipped:
                flags.add("clipping")
        if self._scale_missing():
            flags.add("invalid_calibration")
        if state == "incomplete":
            flags.add("incomplete_interval")
            cause = INCOMPLETE_FLAG.get(termination_reason or "")
            if cause:
                flags.add(cause)
        trig = ev.start.trigger
        b = ev.start.baseline
        bs = self.config.detection.baseline
        relative = trig["kind"] == "relative"
        first = next((i for i in ev.intervals if i.second == trig["first_qualifying_second"]), None)
        trigger_value = first.result.metrics.get(trig["metric"]) if first else None
        recorded = ev.rec_start_sample is not None and bool(ev.segments)
        expected = recorded or (not final and ev.rec_start_sample is not None)
        payload = EventRevision(
            event_id=ev.event_id,
            revision=ev.revision,
            sent_at=self._utc_now_iso(),
            channel=self.config.channel,
            deployment_id=ev.provenance["deployment_id"],
            profile_id=ev.provenance["profile_id"],
            calibration_id=ev.provenance["calibration_id"],
            configuration_revision=ev.provenance["configuration_revision"],
            detection_state="finalized" if final else "open",
            started_at=iso_second(ev.start.start_second),
            ended_at=iso_second(end_second) if final and end_second is not None else None,
            detection={
                "rule_version": ev.start.rule_version,
                "trigger_metric": trig["metric"],
                "trigger_kind": "baseline_relative" if relative else "absolute",
                "threshold_db": trig["delta_db"] if relative else trig["threshold_db"],
                "trigger_value_db": trigger_value,
                "baseline_db": b.get(trig["metric"]) if relative else None,
                "baseline_method": BASELINE_METHOD.format(pct=bs.percentile, metric=trig["metric"], window=bs.window_seconds,
                                                          min=bs.min_eligible_seconds) if relative else None,
            },
            summary=summary or {},
            recording={
                "expected": bool(expected),
                "started_at": iso_utc(self.mapper.utc_of_sample(ev.rec_start_sample)) if ev.rec_start_sample is not None else None,
                "ended_at": iso_utc(self.mapper.utc_of_sample(ev.written_through)) if final and recorded else None,
                "expected_segments": len(ev.segments) if final and recorded else None,
            },
            quality_flags=sorted(flags),
        )
        body = payload.model_dump()
        self.sink.submit(
            ops.EventRevisionOp(
                event_id=ev.event_id,
                session_id=ev.session_id,
                channel=self.config.channel,
                start_second=ev.start.start_second,
                revision=ev.revision,
                state="open" if not final else state,
                payload=body,
                uploadable=ev.trusted,
                termination_reason=termination_reason,
                last_observed_at=iso_second(last_observed) if last_observed is not None else None,
                detail={
                    "local_flags": sorted(ev.flags),
                    "preroll_shortfall_samples": ev.preroll_shortfall_samples,
                    "recording_stop_reason": ev.recording_stop_reason,
                    "segments": ev.segments,
                    "rec_start_sample": ev.rec_start_sample,
                    "written_through": ev.written_through,
                    "disabled_rules": self.disabled_rules,
                    "trigger": trig,
                    "baseline": b,
                    "enabled_rules": ev.start.rules,
                },
            )
        )

    def _finish_event(self, state: str, reason: str | None, last_observed: int | None) -> None:
        ev = self.event
        if ev is None:
            return
        if ev.seg_id is not None:
            self._close_segment(reason or "event_finished", incomplete=state != "complete")
        self._emit_revision(state, reason, last_observed)
        self._count(f"events_{state}")
        self.event = None

    # ------------------------------------------------------------------ misc

    def _count(self, name: str, delta: int = 1, error: str | None = None) -> None:
        self.counters[name] = self.counters.get(name, 0) + delta
        self.sink.submit(ops.Counter(name, delta, error))

    @property
    def detector_state(self) -> str:
        return self.detector.state

    def status(self) -> dict:
        s = self.session
        return {
            "session_id": s.session_id if s else None,
            "detector_state": self.detector.state,
            "event_id": self.event.event_id if self.event else None,
            "profile_id": self.profile.profile_id,
            "profile_mode": self.profile.mode,
            "spl_allowed": self.spl_allowed,
            "configuration_revision": self.config.revision,
            "timing": self.mapper.describe() if s else None,
            "counters": dict(self.counters),
            "high_water_second": self.high_water,
            "scale_available": self.profile.scale is not None,
            "detection": self.detection_view(),
        }

    def detection_view(self) -> dict:
        """Current baselines and effective thresholds (for local display; cheap: <= 3600 values each)."""
        k = self.next_k if self.next_k is not None else 0
        baselines = {m: rp.value(k) for m, rp in self.detector.baselines.items()}
        rules = []
        for r in self.detector.rules:
            if r.kind == "absolute":
                thr = r.threshold_db
            else:
                b = (self.detector.frozen.baseline if self.detector.frozen else baselines).get(r.metric)
                thr = None if b is None else b + (r.delta_db or 0.0)
            rules.append({"id": r.id, "kind": r.kind, "metric": r.metric, "threshold_db": thr,
                          "consecutive_seconds": r.consecutive_seconds})
        return {"state": self.detector.state, "baselines": baselines, "rules": rules,
                "eligible_seconds": self.detector.baselines["laeq_db"].count(k) if k else 0,
                "event_id": self.event.event_id if self.event else None,
                "event_started_second": self.event.start.start_second if self.event else None}


def _mono_now() -> float:
    import time

    return time.monotonic()
