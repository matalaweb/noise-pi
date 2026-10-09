"""Owner-controlled inputs to configuration parsing (capture format, gain checks, retention bounds).

The measurement chain itself (profile + calibration) is built from the same settings by
``config/chain.py``.
"""

from __future__ import annotations

from ..contract.configuration import CaptureSpec, LocalInputs
from .settings import Settings


def local_inputs(settings: Settings) -> LocalInputs:
    c = settings.capture
    return LocalInputs(
        channel=settings.channel.id,
        capture=CaptureSpec(sample_rate=settings.measurement.sample_rate_hz, container=c.container, valid_bits=c.valid_bits,
                            channels=c.channels, analysis_channel=c.analysis_channel),
        expected_gain_controls=dict(settings.microphone.expected_gain_controls),
        gain_reference_check=settings.microphone.gain_reference_check,
        max_ack_retention_days=settings.storage.max_acknowledged_retention_days,
        max_audio_retention_days=settings.storage.max_verified_audio_retention_days,
        builtin_gain_check=settings.microphone.model == "umik-1",
        recording_locally_enabled=settings.recording.locally_enabled,
    )
