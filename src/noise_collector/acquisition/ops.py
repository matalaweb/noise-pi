"""Durable operations emitted by the acquisition engine, applied in order by a durability sink."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np

from ..evidence.spool import SegmentOpen


@dataclass(frozen=True)
class SessionStart:
    session_id: str
    channel: str
    stream_id: str
    timing_epoch: int
    start_sample: int
    started_mono: float
    started_utc: str | None
    microphone: dict
    pcm_format: dict
    gain: dict
    profile_id: str
    configuration_revision: int
    os_boot_id: str | None
    agent_version: str


@dataclass(frozen=True)
class SessionEnd:
    session_id: str
    end_sample: int
    ended_utc: str | None
    reason: str
    timing: dict


@dataclass(frozen=True)
class Measurement:
    session_id: str
    sequence: int | None
    channel: str
    utc_second: int
    status: str
    omit_reason: str | None
    first_sample: int
    sample_count: int
    timing_trusted: bool
    timing_note: str | None
    wire: dict | None
    diagnostics: dict
    upload: bool


@dataclass(frozen=True)
class Gap:
    session_id: str | None
    channel: str
    cause: str
    start_utc: str | None
    end_utc: str | None
    start_sample: int | None
    end_sample: int | None
    clock_quality: str | None
    detail: dict


@dataclass(frozen=True)
class EventRevisionOp:
    event_id: str
    session_id: str
    channel: str
    start_second: int
    revision: int
    state: str  # open | complete | incomplete
    payload: dict
    uploadable: bool
    termination_reason: str | None
    last_observed_at: str | None
    detail: dict


@dataclass(frozen=True)
class SegmentOpenOp:
    segment: SegmentOpen


@dataclass(frozen=True)
class AudioOp:
    recording_id: str
    first_sample: int
    samples: np.ndarray


@dataclass(frozen=True)
class SegmentCloseOp:
    recording_id: str
    end_sample: int
    reason: str
    incomplete: bool


@dataclass(frozen=True)
class ConfigApplied:
    revision: int
    status: str  # applied | rejected
    applied_at: str | None
    reason_code: str | None
    detail: str | None
    content_hash: str | None = None


@dataclass(frozen=True)
class Counter:
    name: str
    delta: int = 1
    error: str | None = None


Op = SessionStart | SessionEnd | Measurement | Gap | EventRevisionOp | SegmentOpenOp | AudioOp | SegmentCloseOp | ConfigApplied | Counter


class Sink(Protocol):
    def submit(self, op: Op) -> bool:
        """Queue or apply ``op``. Returns False if it could not be accepted (backlog full)."""
        ...
