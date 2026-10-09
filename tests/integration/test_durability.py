"""Kill-point and storage-failure tests for the evidence spool and SQLite state."""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from noise_collector.acquisition import ops
from noise_collector.acquisition.durability import DurabilityApplier, QueuedSink
from noise_collector.evidence import spool as spool_mod
from noise_collector.evidence.fsutil import resolve, sha256_file
from noise_collector.evidence.spool import EvidenceWriter, SegmentOpen, recover
from noise_collector.store.db import connect, migrate, transaction
from noise_collector.store.lock import InstanceLock, LockHeld

FS = 48000


def setup_db(state: Path) -> sqlite3.Connection:
    migrate(state / "collector.db")
    conn = connect(state / "collector.db")
    with transaction(conn):
        conn.execute(
            """INSERT INTO acquisition_sessions(session_id, channel, stream_id, timing_epoch, start_sample, started_mono, agent_version,
               microphone_json, format_json, gain_json, created_at) VALUES ('s1',0,'st',1,0,0,'t','{}','{}','{}','now')"""
        )
        conn.execute(
            """INSERT INTO events(event_id, session_id, channel, state, start_second, timing_trusted, latest_revision, created_at)
               VALUES ('e1','s1',0,'open',100,1,1,'now')"""
        )
    return conn


def seg(rid="r1", start=1000) -> SegmentOpen:
    return SegmentOpen(rid, "e1", 1, "s1", start, "2026-09-21T00:00:00.000Z", FS, 24, {"p": 1}, uploadable=True)


def samples(n, seed=0):
    return np.random.default_rng(seed).integers(-(1 << 23), 1 << 23, n).astype(np.int32)


def read_wav(state, row):
    data, _ = sf.read(resolve(state, row["path"]), dtype="int32")
    return (data.astype(np.int64) >> 8).astype(np.int32)


def test_chunks_finalize_to_exact_wav(state_dir):
    conn = setup_db(state_dir)
    w = EvidenceWriter(conn, state_dir, chunk_seconds=1.0)
    w.open_segment(seg())
    x = samples(int(3.5 * FS))
    for a in range(0, len(x), 4000):
        w.append("r1", 1000 + a, x[a : a + 4000])
    res = w.close_segment("r1", 1000 + len(x), "post_roll_complete", False)
    row = conn.execute("SELECT * FROM recordings").fetchone()
    assert res.sample_count == len(x) and row["state"] == "finalized" and not row["incomplete"]
    assert np.array_equal(read_wav(state_dir, row), x)
    assert row["sha256"] == sha256_file(resolve(state_dir, row["path"]))
    assert not (state_dir / "spool" / "r1").exists()
    assert {r[0] for r in conn.execute("SELECT state FROM audio_chunks")} == {"deleted"}


def test_crash_with_open_partial_chunk_recovers_complete_samples_only(state_dir):
    conn = setup_db(state_dir)
    w = EvidenceWriter(conn, state_dir, chunk_seconds=1.0)
    w.open_segment(seg())
    x = samples(int(2.3 * FS))
    w.append("r1", 1000, x)
    w.sync()
    # simulate a torn write: one extra partial sample's worth of bytes in the active chunk
    partial = sorted((state_dir / "spool" / "r1").glob("*.partial"))[0]
    with open(partial, "ab") as fh:
        fh.write(b"\x01\x02")
    conn.close()
    conn = connect(state_dir / "collector.db")
    rec = recover(conn, state_dir)
    assert rec == ["r1"]
    row = conn.execute("SELECT * FROM recordings").fetchone()
    assert row["incomplete"] == 1 and row["close_reason"] == "process_interrupted"
    assert row["sample_count"] == len(x) and np.array_equal(read_wav(state_dir, row), x)


@pytest.mark.parametrize("kill_at", ["before_rename", "after_rename_before_commit", "after_commit_before_cleanup"])
def test_kill_during_finalization_reconciles(state_dir, monkeypatch, kill_at):
    conn = setup_db(state_dir)
    w = EvidenceWriter(conn, state_dir, chunk_seconds=1.0)
    w.open_segment(seg())
    x = samples(2 * FS + 17)
    w.append("r1", 1000, x)

    class Killed(Exception):
        pass

    real_replace, real_rmtree = os.replace, spool_mod.shutil.rmtree
    if kill_at == "before_rename":
        def boom(src, dst):
            if str(dst).endswith(".wav"):
                raise Killed()
            return real_replace(src, dst)
        monkeypatch.setattr(spool_mod.os, "replace", boom)
    elif kill_at == "after_rename_before_commit":
        def boom2(src, dst):
            real_replace(src, dst)
            if str(dst).endswith(".wav"):
                raise Killed()
        monkeypatch.setattr(spool_mod.os, "replace", boom2)
    else:
        def boom3(*a, **k):
            raise Killed()
        monkeypatch.setattr(spool_mod.shutil, "rmtree", boom3)
    with pytest.raises(Killed):
        w.close_segment("r1", 1000 + len(x), "post_roll_complete", False)
    monkeypatch.setattr(spool_mod.os, "replace", real_replace)
    monkeypatch.setattr(spool_mod.shutil, "rmtree", real_rmtree)
    conn.close()
    conn = connect(state_dir / "collector.db")
    recover(conn, state_dir)
    rows = conn.execute("SELECT * FROM recordings").fetchall()
    assert len(rows) == 1  # never a duplicated source identity
    row = rows[0]
    assert row["state"] == "finalized" and np.array_equal(read_wav(state_dir, row), x)
    assert row["sha256"] == sha256_file(resolve(state_dir, row["path"]))
    assert not list((state_dir / "recordings").glob("*.tmp"))
    assert not (state_dir / "spool" / "r1").exists()


def test_orphan_spool_manifest_without_row_is_recovered(state_dir):
    conn = setup_db(state_dir)
    w = EvidenceWriter(conn, state_dir, chunk_seconds=1.0)
    w.open_segment(seg())
    w.append("r1", 1000, samples(FS))
    w.sync()
    with transaction(conn):  # simulate crash between manifest write and row commit
        conn.execute("DELETE FROM audio_chunks")
        conn.execute("DELETE FROM recordings")
    assert recover(conn, state_dir) == ["r1"]
    row = conn.execute("SELECT * FROM recordings").fetchone()
    assert row["incomplete"] == 1 and row["sample_count"] == FS


def test_append_gap_breaks_segment_instead_of_stitching(state_dir):
    conn = setup_db(state_dir)
    w = EvidenceWriter(conn, state_dir)
    w.open_segment(seg())
    w.append("r1", 1000, samples(1000))
    w.append("r1", 3000, samples(1000, 1))  # 1000 samples never arrived
    w.close_segment("r1", 4000, "post_roll_complete", False)
    row = conn.execute("SELECT * FROM recordings").fetchone()
    assert row["incomplete"] == 1 and row["sample_count"] == 1000 and "audio_backlog_loss" in row["close_reason"]


def test_queued_sink_retries_storage_failure_in_order(state_dir, monkeypatch):
    setup_db(state_dir).close()
    conn = connect(state_dir / "collector.db", check_same_thread=False)
    applier = DurabilityApplier(conn, state_dir)
    sink = QueuedSink(applier, sync_interval_s=0.05)
    calls = {"n": 0}
    real = applier._on_Counter

    def flaky(op):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise sqlite3.OperationalError("disk I/O error")
        real(op)

    monkeypatch.setattr(applier, "_on_Counter", flaky)
    sink.start()
    for i in range(3):
        assert sink.submit(ops.Counter(f"c{i}"))
    deadline = time.time() + 10
    while sink.backlog()["ops"] and time.time() < deadline:
        time.sleep(0.05)
    assert sink.stop()
    assert sink.failure is None
    names = [r[0] for r in conn.execute("SELECT name FROM health_counters ORDER BY rowid")]
    assert names == ["c0", "c1", "c2"]


def test_queued_sink_rejects_beyond_backlog_limits(state_dir):
    conn = setup_db(state_dir)
    sink = QueuedSink(DurabilityApplier(conn, state_dir), max_ops=2, max_audio_bytes=100)
    assert sink.submit(ops.Counter("a")) and sink.submit(ops.Counter("b"))
    assert not sink.submit(ops.Counter("c"))
    assert sink.backlog()["rejected"] == 1


def test_second_instance_cannot_use_same_state_dir(state_dir):
    with InstanceLock(state_dir, "acquisition"):
        with pytest.raises(LockHeld):
            InstanceLock(state_dir, "acquisition").acquire()
        with InstanceLock(state_dir, "delivery"):  # different role is fine
            pass
    with InstanceLock(state_dir, "acquisition"):
        pass  # released on exit


def test_measurement_commit_is_atomic_with_high_water(state_dir):
    conn = setup_db(state_dir)
    applier = DurabilityApplier(conn, state_dir)
    m = ops.Measurement("s1", 1, 0, 1790000000, "complete", None, 0, 48000, True, None, {"x": 1}, {}, True)
    applier.apply(m)
    assert conn.execute("SELECT value FROM metadata WHERE key='utc_high_water:0'").fetchone()[0] == "1790000000"
    with pytest.raises(sqlite3.IntegrityError):  # duplicate (session, sequence) is refused, nothing half-written
        applier.apply(ops.Measurement("s1", 1, 0, 1790000001, "complete", None, 0, 48000, True, None, {"x": 2}, {}, True))
    assert conn.execute("SELECT value FROM metadata WHERE key='utc_high_water:0'").fetchone()[0] == "1790000000"


def test_migrations_refuse_downgrade_and_backup(state_dir):
    db = state_dir / "collector.db"
    migrate(db)
    conn = connect(db)
    conn.execute("INSERT INTO schema_migrations(version, name, applied_at) VALUES (999, 'future', 'x')")
    conn.close()
    with pytest.raises(RuntimeError, match="newer"):
        migrate(db)


def test_online_backup_includes_wal_content(state_dir, tmp_path):
    from noise_collector.store.db import backup

    conn = setup_db(state_dir)
    with transaction(conn):
        conn.execute("INSERT INTO metadata(key, value) VALUES ('k', 'v')")
    backup(conn, tmp_path / "b.db")
    b = sqlite3.connect(tmp_path / "b.db")
    assert b.execute("SELECT value FROM metadata WHERE key='k'").fetchone()[0] == "v"
