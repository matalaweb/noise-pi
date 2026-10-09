"""Startup reconciliation for the acquisition process (runs before capture starts)."""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
from pathlib import Path

from ..contract.configuration import DeviceConfiguration
from ..evidence.spool import recover as recover_evidence
from ..store.db import dumps, transaction
from ..timeutil import iso_second, iso_utc, parse_iso

log = logging.getLogger(__name__)


def _energy(values: list[float]) -> float | None:
    if not values:
        return None
    return 10 * math.log10(sum(10 ** (v / 10) for v in values) / len(values))


def recover_state(conn: sqlite3.Connection, state_dir: Path, config: DeviceConfiguration | None) -> dict:
    report: dict = {"recordings_recovered": recover_evidence(conn, state_dir), "sessions_closed": [], "events_interrupted": []}
    for s in conn.execute("SELECT session_id FROM acquisition_sessions WHERE end_reason IS NULL").fetchall():
        last = conn.execute(
            "SELECT MAX(first_sample + sample_count) e, MAX(utc_second) k FROM measurements WHERE session_id=?",
            (s["session_id"],),
        ).fetchone()
        with transaction(conn):
            conn.execute(
                "UPDATE acquisition_sessions SET end_reason='process_interrupted', end_sample=?, ended_utc=? WHERE session_id=?",
                (last["e"], iso_second(last["k"] + 1) if last["k"] is not None else None, s["session_id"]),
            )
        report["sessions_closed"].append(s["session_id"])
    for ev in conn.execute("SELECT * FROM events WHERE state='open'").fetchall():
        last = conn.execute(
            "SELECT MAX(utc_second) k FROM measurements WHERE session_id=? AND utc_second >= ?",
            (ev["session_id"], ev["start_second"]),
        ).fetchone()["k"]
        last_observed = (last + 1) if last is not None else ev["start_second"]
        with transaction(conn):
            conn.execute(
                "UPDATE events SET state='interrupted', termination_reason='process_interrupted', last_observed_at=?, finalized_at=? WHERE event_id=?",
                (iso_second(last_observed), iso_utc(time.time()), ev["event_id"]),
            )
            if ev["timing_trusted"]:
                _queue_interrupted_revision(conn, ev, last_observed)
        report["events_interrupted"].append(ev["event_id"])
    if report["recordings_recovered"] or report["sessions_closed"] or report["events_interrupted"]:
        log.warning("startup recovery: %s", report)
    return report


def _queue_interrupted_revision(conn: sqlite3.Connection, ev: sqlite3.Row, last_observed: int) -> None:
    """Finalize an event whose process died: ended at the last *observed* second, flagged
    ``incomplete_interval`` + ``processing_error``. Never a fabricated normal end."""
    latest = conn.execute(
        "SELECT payload_json FROM event_revisions WHERE event_id=? ORDER BY revision DESC LIMIT 1", (ev["event_id"],)
    ).fetchone()
    payload = json.loads(latest["payload_json"])
    if payload.get("detection_state") == "finalized":
        return
    last_observed = max(last_observed, ev["start_second"])
    rows = conn.execute(
        "SELECT wire_json FROM measurements WHERE session_id=? AND status='complete' AND wire_json IS NOT NULL AND utc_second >= ? AND utc_second < ?",
        (ev["session_id"], ev["start_second"], last_observed),
    ).fetchall()
    recs = [json.loads(r["wire_json"]) for r in rows]

    def vals(key: str) -> list[float]:
        return [r[key] for r in recs if r.get(key) is not None]

    lafmax = vals("lafmax_db")
    segs = conn.execute(
        "SELECT capture_started_at, duration_ms FROM recordings WHERE event_id=? AND state='finalized' ORDER BY segment_number",
        (ev["event_id"],),
    ).fetchall()
    rec = dict(payload.get("recording") or {})
    if segs:
        last = segs[-1]
        rec["ended_at"] = iso_utc(parse_iso(last["capture_started_at"]).timestamp() + (last["duration_ms"] or 0) / 1000)
        rec["expected_segments"] = len(segs)
        rec["expected"] = True
    else:
        rec.update(expected=False, ended_at=None, expected_segments=None)
    payload.update(
        revision=ev["latest_revision"] + 1,
        sent_at=iso_utc(time.time()),
        detection_state="finalized",
        ended_at=iso_second(last_observed),
        recording=rec,
        summary={
            "laeq_db": _energy(vals("laeq_db")),
            "lafmax_db": max(lafmax) if lafmax else None,
            "lceq_db": _energy(vals("lceq_db")),
            "lcpeak_db": None,
            "low_frequency_leq_db": _energy(vals("low_frequency_leq_db")),
            "rms_dbfs": _energy(vals("rms_dbfs")),
            "duration_ms": (last_observed - ev["start_second"]) * 1000,
        },
        quality_flags=sorted(set(payload.get("quality_flags", [])) | {"incomplete_interval", "processing_error"}),
    )
    conn.execute(
        "INSERT INTO event_revisions(event_id, revision, payload_json, delivery_state, created_at) VALUES (?,?,?, 'pending', ?)",
        (ev["event_id"], payload["revision"], dumps(payload), iso_utc(time.time())),
    )
    conn.execute("UPDATE events SET latest_revision=? WHERE event_id=?", (payload["revision"], ev["event_id"]))
