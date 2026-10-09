"""Deterministic replay: run a file through the real engine and durability layer."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..audio.file_source import Faults, FileSource
from ..contract.configuration import DeviceConfiguration
from ..evidence.spool import recover
from ..store.db import connect, migrate
from ..timing.clock import SimulatedClock
from ..timing.mapper import TimingSettings
from .durability import DirectSink, DurabilityApplier
from .engine import AcquisitionEngine, EngineLocalSettings


@dataclass
class ReplayResult:
    state_dir: Path
    engine_status: dict
    counts: dict

    def to_json(self) -> str:
        return json.dumps({"state_dir": str(self.state_dir), "engine": self.engine_status, "counts": self.counts}, indent=2, default=str)


def replay(
    path: str,
    state_dir: Path,
    config: DeviceConfiguration,
    *,
    start_utc: float = 1_790_000_000.0,
    block_pattern: list[int] | None = None,
    seed: int | None = None,
    faults: Faults | None = None,
    local: EngineLocalSettings | None = None,
    synchronized: bool = True,
    rate_ppm: float = 0.0,
    id_factory=None,
    audio_allowed=None,
) -> ReplayResult:
    state_dir.mkdir(parents=True, exist_ok=True)
    db = state_dir / "collector.db"
    migrate(db)
    conn = connect(db)
    recover(conn, state_dir)
    clock = SimulatedClock(start_utc - 1000.0, synchronized=synchronized)
    src = FileSource(path, start_utc=start_utc, block_pattern=block_pattern, seed=seed, faults=faults, clock=clock, rate_ppm=rate_ppm)
    local = local or EngineLocalSettings(timing=TimingSettings(require_clock_sync=True))
    applier = DurabilityApplier(conn, state_dir)
    engine = AcquisitionEngine(
        config=config,
        local=local,
        fmt=src.fmt,
        microphone={"source": "file", "path": str(path)},
        sink=DirectSink(applier),
        clock=clock,
        id_factory=id_factory,
        audio_allowed=audio_allowed,
    )
    engine.start_stream("replay")
    for blk in src.blocks():
        engine.on_block(blk)
    engine.stop_stream("replay_end")
    applier.close("replay_end")
    counts = summarize(conn)
    status = engine.status()
    conn.close()
    return ReplayResult(state_dir, status, counts)


def summarize(conn) -> dict:
    q = lambda sql: [dict(r) for r in conn.execute(sql)]  # noqa: E731
    return {
        "measurements": q("SELECT status, delivery_state, COUNT(*) n FROM measurements GROUP BY status, delivery_state"),
        "omit_reasons": q("SELECT omit_reason, COUNT(*) n FROM measurements WHERE status='omitted' GROUP BY omit_reason"),
        "events": q("SELECT event_id, state, start_second, latest_revision, termination_reason FROM events ORDER BY start_second"),
        "recordings": q("SELECT recording_id, event_id, segment_number, start_sample, end_sample, sample_count, incomplete, close_reason, state, delivery_state, sha256 FROM recordings ORDER BY created_at"),
        "gaps": q("SELECT cause, start_utc, end_utc, start_sample, end_sample FROM gaps ORDER BY id"),
        "sessions": q("SELECT session_id, start_sample, end_sample, end_reason FROM acquisition_sessions ORDER BY rowid"),
        "counters": q("SELECT name, value FROM health_counters ORDER BY name"),
    }
