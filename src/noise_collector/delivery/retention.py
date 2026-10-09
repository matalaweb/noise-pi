"""Safe local retention. Never deletes unacknowledged or unverified originals."""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

from ..config.settings import StorageSettings
from ..evidence.fsutil import resolve
from ..store.db import checkpoint, dumps, transaction
from ..timeutil import iso_utc, parse_iso

log = logging.getLogger(__name__)

DIAGNOSTIC_RETENTION_DAYS = 30


def run(conn: sqlite3.Connection, state_dir: Path, settings: StorageSettings, storage_state: str | None, now: float | None = None,
        *, ack_days: float | None = None, audio_hours: float | None = None) -> dict:
    """``ack_days``/``audio_hours`` come from the applied configuration (bounded locally); the local
    settings are the defaults when no configuration is applied."""
    now = now or time.time()
    ack_days = settings.acknowledged_retention_days if ack_days is None else ack_days
    audio_hours = settings.verified_audio_retention_hours if audio_hours is None else audio_hours
    pressure = storage_state in ("warning", "audio_stopped", "critical")
    out = {"measurements_deleted": 0, "batches_deleted": 0, "diagnostics_deleted": 0, "audio_deleted": 0, "audio_bytes_freed": 0}
    ack_cutoff = int(now - ack_days * 86400)
    with transaction(conn):
        out["measurements_deleted"] = conn.execute(
            "DELETE FROM measurements WHERE delivery_state='acknowledged' AND utc_second < ?", (ack_cutoff,)
        ).rowcount
        out["batches_deleted"] = conn.execute(
            """DELETE FROM outbox_batches WHERE state IN ('acknowledged','superseded') AND acknowledged_at IS NOT NULL
               AND acknowledged_at < ? AND batch_id NOT IN (SELECT batch_id FROM measurements WHERE batch_id IS NOT NULL)""",
            (iso_utc(ack_cutoff),),
        ).rowcount
        diag_days = ack_days if pressure else DIAGNOSTIC_RETENTION_DAYS
        out["diagnostics_deleted"] = conn.execute(
            "DELETE FROM measurements WHERE delivery_state='local_only' AND utc_second < ?", (int(now - diag_days * 86400),)
        ).rowcount
    audio_cutoff = now - (0 if pressure else audio_hours * 3600)
    rows = conn.execute(
        """SELECT recording_id, path, sha256, verified_sha256, verified_at, size_bytes FROM recordings
           WHERE delivery_state='verified' AND local_deleted_at IS NULL AND path IS NOT NULL
           AND verified_sha256 IS NOT NULL AND verified_sha256 = sha256"""
    ).fetchall()
    for r in rows:
        if r["verified_at"] is None or parse_iso(r["verified_at"]).timestamp() > audio_cutoff:
            continue
        p = resolve(state_dir, r["path"])
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        with transaction(conn):
            conn.execute("UPDATE recordings SET local_deleted_at=? WHERE recording_id=?", (iso_utc(now), r["recording_id"]))
            conn.execute(
                "INSERT INTO deletion_receipts(kind, reference, sha256, detail_json, deleted_at) VALUES ('recording_local_copy',?,?,?,?)",
                (r["recording_id"], r["sha256"], dumps({"verified_at": r["verified_at"], "size_bytes": r["size_bytes"],
                                                        "note": "local copy only; cloud original unaffected"}), iso_utc(now)),
            )
        out["audio_deleted"] += 1
        out["audio_bytes_freed"] += r["size_bytes"] or 0
    checkpoint(conn)
    if any(out.values()):
        log.info("retention: %s", out)
    return out
