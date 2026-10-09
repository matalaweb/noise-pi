"""Live ALSA capture through sounddevice/PortAudio.

The PortAudio callback (``_callback``) only copies the raw input bytes into the preallocated
``CaptureBuffer`` and records the ADC/current stream times, ``time.monotonic()`` and the overflow
flag. Decoding, timestamp conversion, DSP, logging and storage happen on the consumer thread.

Timestamps: PortAudio's ALSA host API reports stream time on CLOCK_MONOTONIC on Linux, the same
clock as ``time.monotonic()``. We still measure ``host_mono - currentTime`` rather than assume it:
the offset is fixed from the first callback of the stream (changing it mid-stream would step every
timestamp) and its spread over later callbacks is tracked as a diagnostic. If
``inputBufferAdcTime`` is 0 (unsupported), the block start is estimated as host receive time minus
the block duration and the configured latency, and flagged ``fallback``. This must be validated on
the actual Pi/ALSA/microphone combination (docs/hardware-validation.md).
"""

from __future__ import annotations

import statistics
import time

import numpy as np
from collections.abc import Iterator

from ..acquisition.engine import CapturedBlock
from .pcm import PcmFormat, decode_frames, select_channel
from .ring import CaptureBuffer


class CaptureError(RuntimeError):
    pass


class AlsaCapture:
    def __init__(self, pa_index: int, fmt: PcmFormat, *, latency="high", buffer_seconds: float = 2.0,
                 fallback_latency_ms: float = 0.0, wall_minus_mono=None) -> None:
        self.pa_index = pa_index
        self.fmt = fmt
        self.latency = latency
        self.buffer = CaptureBuffer(int(buffer_seconds * fmt.sample_rate), fmt.bytes_per_frame)
        self.fallback_latency_s = fallback_latency_ms / 1000.0
        self.wall_minus_mono = wall_minus_mono
        self.stream = None
        self.last_callback_mono = 0.0
        self._offsets: list[float] = []
        self.stream_to_mono: float | None = None
        self.callback_errors = 0
        # Multi-channel units that carry one microphone on every channel (UMIK-1): verify, never average.
        self.channel_blocks = 0
        self.channel_mismatch_blocks = 0
        self.channel_max_diff = 0

    def check(self) -> None:
        import sounddevice as sd

        try:
            sd.check_input_settings(device=self.pa_index, channels=self.fmt.channels, dtype=self.fmt.container,
                                    samplerate=self.fmt.sample_rate)
        except Exception as exc:  # sounddevice raises PortAudioError / ValueError
            raise CaptureError(f"format not supported: {exc}") from exc

    def _callback(self, indata, frames, time_info, status) -> None:  # real-time: copy only
        try:
            mono = time.monotonic()
            self.last_callback_mono = mono
            self.buffer.push(indata, frames, time_info.inputBufferAdcTime, time_info.currentTime, mono,
                             1 if status.input_overflow else 0)
        except Exception:  # never raise into PortAudio; the watchdog sees stalled progress
            self.callback_errors += 1

    def start(self) -> None:
        import sounddevice as sd

        self.stream = sd.RawInputStream(
            device=self.pa_index,
            channels=self.fmt.channels,
            dtype=self.fmt.container,
            samplerate=self.fmt.sample_rate,
            blocksize=0,
            latency=self.latency,
            callback=self._callback,
        )
        self.stream.start()
        self.last_callback_mono = time.monotonic()

    def stop(self) -> None:
        if self.stream is not None:
            try:
                self.stream.abort(ignore_errors=True)
                self.stream.close(ignore_errors=True)
            finally:
                self.stream = None

    @property
    def active(self) -> bool:
        return self.stream is not None and bool(self.stream.active)

    def stalled(self, watchdog_s: float) -> bool:
        return self.stream is None or not self.stream.active or time.monotonic() - self.last_callback_mono > watchdog_s

    def offset_spread_ms(self) -> float | None:
        """Spread of callback-entry offsets around the fixed stream->monotonic offset (diagnostic)."""
        if len(self._offsets) < 2:
            return None
        return (statistics.quantiles(self._offsets, n=20)[-1] - min(self._offsets)) * 1000

    def latency_s(self) -> float:
        return float(self.stream.latency) if self.stream is not None else 0.0

    def blocks(self) -> Iterator[CapturedBlock]:
        """Drain available blocks (non-blocking)."""
        fs = self.fmt.sample_rate
        while (raw := self.buffer.pop()) is not None:
            frames = decode_frames(raw.data, self.fmt)
            samples = select_channel(frames, self.fmt)
            if self.fmt.channels > 1 and len(frames):
                self.channel_blocks += 1
                other = frames[:, 1 if self.fmt.analysis_channel == 0 else 0]
                if not np.array_equal(samples, other):
                    self.channel_mismatch_blocks += 1
                    self.channel_max_diff = max(self.channel_max_diff, int(np.max(np.abs(samples.astype(np.int64) - other))))
            observed = raw.host_mono - raw.current_time
            if self.stream_to_mono is None:
                self.stream_to_mono = observed
            if len(self._offsets) < 256:
                self._offsets.append(observed - self.stream_to_mono)
            offset = self.stream_to_mono
            if raw.adc_time > 0:
                mono = raw.adc_time + offset
                source = "adc"
            else:
                mono = raw.host_mono - raw.frames / fs - self.fallback_latency_s
                source = "fallback"
            yield CapturedBlock(
                samples=samples,
                first_sample=raw.first_frame,
                mono_time=mono,
                ts_source=source,
                wall_minus_mono=self.wall_minus_mono() if self.wall_minus_mono else 0.0,
                lost_before=raw.lost_before,
                driver_overflow=bool(raw.status_flags & CaptureBuffer.STATUS_INPUT_OVERFLOW),
            )
