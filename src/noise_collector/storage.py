"""Storage quotas and pressure policy.

Logical budgets on the data volume:

* reserve: max(1 GiB, 10% of the volume) kept free for SQLite/WAL growth and orderly recovery
  (capped at 25% on small volumes; ``doctor`` warns when the volume is too small to plan for);
* finalization headroom: room to assemble one maximum segment while its chunks still exist;
* measurement metadata quota: default min(4 GiB, 20% of volume) (30 days of 1 s rows is ~3 GB);
* audio quota: what remains, unless configured explicitly.

Pressure states: ``ok`` -> ``warning`` (prune eligible acknowledged/verified data, report) ->
``audio_stopped`` (no new/extended recordings; measurements continue) -> ``critical`` (free space
below the metadata floor: durable capture can no longer be claimed).
"""

from __future__ import annotations

import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

from .config.settings import StorageSettings
from .evidence.fsutil import dir_size

GIB = 1024**3
MAX_SEGMENT_BYTES = 95 * 1024 * 1024
METADATA_FLOOR_BYTES = 128 * 1024 * 1024


@dataclass
class StorageState:
    volume_total: int
    volume_free: int
    reserve: int
    finalize_headroom: int
    audio_quota: int
    measurement_quota: int
    audio_used: int
    db_bytes: int
    state: str

    def audio_allowed(self, need: int) -> bool:
        return (
            self.state in ("ok", "warning")
            and self.audio_used + need <= self.audio_quota
            and self.volume_free - need - self.finalize_headroom >= self.reserve
        )

    def to_dict(self) -> dict:
        return asdict(self)


def plan(total: int, settings: StorageSettings) -> tuple[int, int, int, int]:
    reserve = settings.reserve_bytes or min(max(GIB, total // 10), total // 4)
    headroom = 2 * MAX_SEGMENT_BYTES
    meas = settings.measurement_quota_bytes or min(4 * GIB, total // 5)
    audio = settings.audio_quota_bytes or max(0, total - reserve - headroom - meas)
    return reserve, headroom, meas, audio


def measure(state_dir: Path, settings: StorageSettings) -> StorageState:
    du = shutil.disk_usage(state_dir)
    reserve, headroom, meas_quota, audio_quota = plan(du.total, settings)
    audio_used = dir_size(state_dir / "spool") + dir_size(state_dir / "recordings")
    db = sum(os.path.getsize(p) for p in state_dir.glob("collector.db*") if p.is_file())
    free = du.free
    if free < METADATA_FLOOR_BYTES:
        state = "critical"
    elif audio_used >= audio_quota or free - headroom < reserve:
        state = "audio_stopped"
    elif audio_used >= settings.warning_fraction * audio_quota or db >= settings.warning_fraction * meas_quota or free < 1.5 * reserve:
        state = "warning"
    else:
        state = "ok"
    return StorageState(du.total, free, reserve, headroom, audio_quota, meas_quota, audio_used, db, state)
