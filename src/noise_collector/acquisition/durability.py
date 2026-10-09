"""Apply engine operations durably (SQLite + evidence spool), in order.

``DirectSink`` applies synchronously (replay, tests). ``QueuedSink`` runs a single durability
thread behind a bounded backlog so the DSP thread never waits on SQLite or disk. Backlog limits
are by op count and by audio bytes; an op that does not fit is rejected and the engine records
the loss. If storage fails, the worker retries the same op (preserving order) and reports a
critical state until storage recovers; it never reports a measurement as stored before commit.
"""

from __future__ import annotations

import logging
import queue
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from ..contract.models import ConfigAck
from ..evidence.spool import EvidenceWriter
from ..store.db import bump_counter, dumps, set_meta, transaction
from ..timeutil import iso_utc
from . import ops

log = logging.getLogger(__name__)


def high_water_key(channel: int) -> str:
    return f"utc_high_water:{channel}"


class DurabilityApplier:
    def __init__(self, conn: sqlite3.Connection, state_dir: Path) -> None:
        self.conn = conn
        self.writer = EvidenceWriter(conn, state_dir)
        self.last_measurement_commit_mono: float | None = None
        self.last_measurement_second: int | None = None

    def apply(self, op: ops.Op) -> None:
        handler = getattr(self, f"_on_{type(op).__name__}")
        handler(op)

    def _on_SessionStart(self, op: ops.SessionStart) -> None:
        now = iso_utc(time.time())
        with transaction(self.conn):
            prev = self.conn.execute(
                "SELECT * FROM acquisition_sessions WHERE channel=? ORDER BY rowid DESC LIMIT 1", (op.channel,)
            ).fetchone()
            self.conn.execute(
                """INSERT INTO acquisition_sessions(session_id, channel, stream_id, timing_epoch, start_sample, started_mono,
                   started_utc, os_boot_id, agent_version, microphone_json, format_json, gain_json, profile_id,
                   configuration_revision, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (op.session_id, op.channel, op.stream_id, op.timing_epoch, op.start_sample, op.started_mono, op.started_utc,
                 op.os_boot_id, op.agent_version, dumps(op.microphone), dumps(op.pcm_format), dumps(op.gain), op.profile_id,
                 op.configuration_revision, now),
            )
            if prev is not None:
                same_stream = prev["stream_id"] == op.stream_id
                self.conn.execute(
                    """INSERT INTO gaps(session_id, channel, start_utc, end_utc, start_sample, end_sample, cause, clock_quality,
                       detail_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (op.session_id, op.channel, prev["ended_utc"], op.started_utc,
                     prev["end_sample"] if same_stream else None, op.start_sample if same_stream else None,
                     prev["end_reason"] or "process_interrupted", None,
                     dumps({"previous_session": prev["session_id"], "same_stream": same_stream}), now),
                )

    def _on_SessionEnd(self, op: ops.SessionEnd) -> None:
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE acquisition_sessions SET end_sample=?, ended_utc=?, end_reason=?, timing_json=? WHERE session_id=?",
                (op.end_sample, op.ended_utc, op.reason, dumps(op.timing), op.session_id),
            )

    def _on_Measurement(self, op: ops.Measurement) -> None:
        with transaction(self.conn):
            self.conn.execute(
                """INSERT INTO measurements(session_id, sequence, channel, utc_second, status, omit_reason, first_sample,
                   sample_count, timing_trusted, timing_note, wire_json, diagnostics_json, delivery_state, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (op.session_id, op.sequence, op.channel, op.utc_second, op.status, op.omit_reason, op.first_sample,
                 op.sample_count, int(op.timing_trusted), op.timing_note,
                 dumps(op.wire) if op.wire is not None else None, dumps(op.diagnostics),
                 "pending" if op.upload else "local_only", iso_utc(time.time())),
            )
            if op.upload:
                set_meta(self.conn, high_water_key(op.channel), str(op.utc_second))
        self.last_measurement_commit_mono = time.monotonic()
        self.last_measurement_second = op.utc_second

    def _on_Gap(self, op: ops.Gap) -> None:
        with transaction(self.conn):
            self.conn.execute(
                """INSERT INTO gaps(session_id, channel, start_utc, end_utc, start_sample, end_sample, cause, clock_quality,
                   detail_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (op.session_id, op.channel, op.start_utc, op.end_utc, op.start_sample, op.end_sample, op.cause,
                 op.clock_quality, dumps(op.detail), iso_utc(time.time())),
            )

    def _on_EventRevisionOp(self, op: ops.EventRevisionOp) -> None:
        now = iso_utc(time.time())
        with transaction(self.conn):
            exists = self.conn.execute("SELECT 1 FROM events WHERE event_id=?", (op.event_id,)).fetchone()
            if exists is None:
                self.conn.execute(
                    """INSERT INTO events(event_id, session_id, channel, state, start_second, timing_trusted, latest_revision,
                       detail_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (op.event_id, op.session_id, op.channel, op.state, op.start_second, int(op.uploadable), op.revision,
                     dumps(op.detail), now),
                )
            else:
                self.conn.execute(
                    """UPDATE events SET state=?, latest_revision=?, termination_reason=?, last_observed_at=?, detail_json=?,
                       finalized_at=CASE WHEN ? != 'open' THEN ? ELSE finalized_at END WHERE event_id=?""",
                    (op.state, op.revision, op.termination_reason, op.last_observed_at, dumps(op.detail), op.state, now, op.event_id),
                )
            self.conn.execute(
                """INSERT INTO event_revisions(event_id, revision, payload_json, delivery_state, created_at)
                   VALUES (?,?,?,?,?)""",
                (op.event_id, op.revision, dumps(op.payload), "pending" if op.uploadable else "local_only", now),
            )

    def _on_SegmentOpenOp(self, op: ops.SegmentOpenOp) -> None:
        self.writer.open_segment(op.segment)

    def _on_AudioOp(self, op: ops.AudioOp) -> None:
        self.writer.append(op.recording_id, op.first_sample, op.samples)

    def _on_SegmentCloseOp(self, op: ops.SegmentCloseOp) -> None:
        self.writer.close_segment(op.recording_id, op.end_sample, op.reason, op.incomplete)

    def _on_ConfigApplied(self, op: ops.ConfigApplied) -> None:
        record_config_ack(self.conn, op.revision, op.status, op.applied_at, op.reason_code, op.detail, op.content_hash)

    def _on_Counter(self, op: ops.Counter) -> None:
        bump_counter(self.conn, op.name, op.delta, op.error)

    def sync(self) -> None:
        self.writer.sync()

    def close(self, reason: str) -> None:
        self.writer.close_all(reason)


def record_config_ack(conn: sqlite3.Connection, revision: int, status: str, applied_at: str | None,
                      reason_code: str | None, detail: str | None, content_hash: str | None = None) -> None:
    """Persist applied/rejected state and queue exactly one acknowledgment per (revision, status).

    Wire ``reason`` (required when rejected, max 2000): ``"<code>: <detail>"`` for rejections, the
    application notes for an applied revision. ``content_hash`` is the server's configuration sha256.
    """
    with transaction(conn):
        if status == "applied":
            conn.execute("UPDATE configurations SET state='superseded' WHERE state='applied' AND revision != ?", (revision,))
        conn.execute(
            "UPDATE configurations SET state=?, applied_at=?, reason_code=?, detail=? WHERE revision=?",
            (status, applied_at, reason_code, detail, revision),
        )
        if content_hash is None:
            row = conn.execute("SELECT sha256 FROM configurations WHERE revision=?", (revision,)).fetchone()
            content_hash = row["sha256"] if row and len(row["sha256"] or "") == 64 else None
        existing = conn.execute(
            "SELECT 1 FROM config_acknowledgments WHERE revision=? AND status=?", (revision, status)
        ).fetchone()
        if existing is None:
            reason = f"{reason_code}: {detail}" if status == "rejected" else detail
            if status == "rejected" and not reason:
                reason = reason_code or "rejected"
            payload = ConfigAck(revision=revision, status=status, reason=(reason or None) and reason[:2000],  # type: ignore[arg-type]
                                applied_at=applied_at, content_hash=content_hash).model_dump()
            conn.execute(
                """INSERT INTO config_acknowledgments(ack_id, revision, status, payload_json, delivery_state, created_at)
                   VALUES (?,?,?,?, 'pending', ?)""",
                (str(uuid.uuid4()), revision, status, dumps(payload), iso_utc(time.time())),
            )


class DirectSink:
    def __init__(self, applier: DurabilityApplier) -> None:
        self.applier = applier

    def submit(self, op: ops.Op) -> bool:
        self.applier.apply(op)
        return True


class ListSink:
    """Collects ops in memory (unit tests)."""

    def __init__(self) -> None:
        self.ops: list[ops.Op] = []

    def submit(self, op: ops.Op) -> bool:
        self.ops.append(op)
        return True

    def of(self, kind: type) -> list:
        return [o for o in self.ops if isinstance(o, kind)]


class QueuedSink:
    def __init__(self, applier: DurabilityApplier, *, max_ops: int = 20000, max_audio_bytes: int = 48 * 1024 * 1024,
                 sync_interval_s: float = 0.5) -> None:
        self.applier = applier
        self.q: queue.Queue = queue.Queue()
        self.max_ops = max_ops
        self.max_audio_bytes = max_audio_bytes
        self.sync_interval_s = sync_interval_s
        self._lock = threading.Lock()
        self.pending_ops = 0
        self.pending_audio_bytes = 0
        self.rejected = 0
        self.failure: str | None = None
        self.failure_since: float | None = None
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name="durability", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def submit(self, op: ops.Op) -> bool:
        nbytes = op.samples.nbytes if isinstance(op, ops.AudioOp) else 0
        with self._lock:
            if self.pending_ops >= self.max_ops or (nbytes and self.pending_audio_bytes + nbytes > self.max_audio_bytes):
                self.rejected += 1
                return False
            self.pending_ops += 1
            self.pending_audio_bytes += nbytes
        self.q.put(op)
        return True

    def _run(self) -> None:
        last_sync = time.monotonic()
        while True:
            try:
                op = self.q.get(timeout=self.sync_interval_s)
            except queue.Empty:
                op = None
            if op is not None:
                if op is _STOP:
                    break
                self._apply_with_retry(op)
                nbytes = op.samples.nbytes if isinstance(op, ops.AudioOp) else 0
                with self._lock:
                    self.pending_ops -= 1
                    self.pending_audio_bytes -= nbytes
            if time.monotonic() - last_sync >= self.sync_interval_s:
                try:
                    self.applier.sync()
                except OSError as exc:
                    self._fail(exc)
                last_sync = time.monotonic()

    def _apply_with_retry(self, op: ops.Op) -> None:
        delay = 0.5
        while True:
            try:
                self.applier.apply(op)
                if self.failure:
                    log.warning("durable storage recovered after: %s", self.failure)
                self.failure = None
                self.failure_since = None
                return
            except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError) as exc:
                self._fail(exc)
                if self._stop.is_set():
                    log.error("dropping op during shutdown after storage failure: %s", type(op).__name__)
                    return
                time.sleep(delay)
                delay = min(delay * 2, 10.0)

    def _fail(self, exc: BaseException) -> None:
        if self.failure is None:
            self.failure_since = time.monotonic()
            log.error("durable storage failure: %s", exc)
        self.failure = f"{type(exc).__name__}: {exc}"[:300]

    def backlog(self) -> dict:
        with self._lock:
            return {"ops": self.pending_ops, "audio_bytes": self.pending_audio_bytes, "rejected": self.rejected,
                    "failure": self.failure}

    def stop(self, timeout: float = 20.0) -> bool:
        self._stop.set()
        self.q.put(_STOP)
        self.thread.join(timeout)
        return not self.thread.is_alive()


_STOP = object()
