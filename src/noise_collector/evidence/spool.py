"""Crash-safe event audio: recoverable PCM chunks, immutable finalized WAV segments.

Layout under the state directory::

    spool/<recording_id>/manifest.json         format, sample origin, provenance (atomic write)
    spool/<recording_id>/chunk-000000.pcm      closed chunk (fsynced, renamed, dir fsynced)
    spool/<recording_id>/chunk-000001.partial  active chunk, fsynced at least every sync interval
    recordings/<recording_id>.wav              finalized immutable segment

Chunk files hold headerless little-endian PCM at the evidence bit depth. Contiguity is implied
by chunk order and the manifest's ``start_sample``; recovery truncates a partial chunk to whole
samples and never claims samples that were not written.

Finalization: build ``.wav.tmp``, fsync, verify header/size, hash the bytes on disk, rename,
fsync the directory, then commit the recording row. Chunks are deleted only after that commit.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..audio.pcm import encode_le
from ..store.db import bump_counter, dumps, transaction
from ..timeutil import iso_utc
from . import wav
from .fsutil import atomic_write, fsync_dir, resolve, sha256_file

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SegmentOpen:
    recording_id: str
    event_id: str
    segment_number: int
    session_id: str
    start_sample: int
    capture_started_at: str
    sample_rate: int
    bits: int
    provenance: dict
    uploadable: bool
    container: str = "wav"  # wav | flac (final file format; chunks are always raw PCM)


def media_format(container: str, bits: int, sample_rate: int) -> dict:
    """Wire media description (declaration fields) for a finalized segment."""
    if container == "flac":
        return {"container": "flac", "mime_type": "audio/flac", "codec": "flac", "channels": 1, "sample_rate": sample_rate, "bit_depth": bits}
    return {"container": "wav", "mime_type": "audio/wav", "codec": f"pcm_s{bits}le", "channels": 1, "sample_rate": sample_rate, "bit_depth": bits}


@dataclass
class _Active:
    seg: SegmentOpen
    dir: Path
    next_sample: int
    chunk_index: int = 0
    chunk_fd: int | None = None
    chunk_samples: int = 0
    chunk_start: int = 0
    unsynced_samples: int = 0
    broken: str | None = None
    written: int = 0


@dataclass
class FinalizedSegment:
    recording_id: str
    path: Path
    sample_count: int
    size_bytes: int
    sha256: str
    incomplete: bool
    reason: str | None


@dataclass
class EvidenceWriter:
    conn: sqlite3.Connection
    state_dir: Path
    chunk_seconds: float = 5.0
    sync_interval_s: float = 1.0
    active: dict[str, _Active] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.spool_root = self.state_dir / "spool"
        self.final_root = self.state_dir / "recordings"
        self.spool_root.mkdir(parents=True, exist_ok=True)
        self.final_root.mkdir(parents=True, exist_ok=True)

    def rel(self, p: Path) -> str:
        return str(p.relative_to(self.state_dir))

    # -- open / append -----------------------------------------------------------------------

    def open_segment(self, seg: SegmentOpen) -> None:
        d = self.spool_root / seg.recording_id
        d.mkdir(parents=True, exist_ok=True)
        manifest = {
            "recording_id": seg.recording_id,
            "event_id": seg.event_id,
            "segment_number": seg.segment_number,
            "session_id": seg.session_id,
            "start_sample": seg.start_sample,
            "capture_started_at": seg.capture_started_at,
            "sample_rate": seg.sample_rate,
            "bits": seg.bits,
            "byte_order": "little",
            "provenance": seg.provenance,
            "uploadable": seg.uploadable,
            "container": seg.container,
        }
        atomic_write(d / "manifest.json", json.dumps(manifest, indent=1).encode())
        fsync_dir(self.spool_root)
        fmt = media_format(seg.container, seg.bits, seg.sample_rate)
        with transaction(self.conn):
            self.conn.execute(
                """INSERT INTO recordings(recording_id, event_id, segment_number, session_id, start_sample,
                   capture_started_at, format_json, provenance_json, state, spool_dir, delivery_state, created_at)
                   VALUES (?,?,?,?,?,?,?,?, 'open', ?, 'local_only', ?)""",
                (seg.recording_id, seg.event_id, seg.segment_number, seg.session_id, seg.start_sample,
                 seg.capture_started_at, dumps(fmt), dumps(seg.provenance), self.rel(d), iso_utc(time.time())),
            )
        self.active[seg.recording_id] = _Active(seg=seg, dir=d, next_sample=seg.start_sample, chunk_start=seg.start_sample)

    def append(self, recording_id: str, first_sample: int, samples: np.ndarray) -> None:
        a = self.active.get(recording_id)
        if a is None or a.broken:
            return
        if first_sample > a.next_sample:
            a.broken = "audio_backlog_loss"
            bump_counter(self.conn, "audio_samples_lost", first_sample - a.next_sample, "evidence append gap")
            return
        if first_sample < a.next_sample:
            skip = a.next_sample - first_sample
            if skip >= len(samples):
                return
            samples = samples[skip:]
        rate = a.seg.sample_rate
        chunk_target = int(self.chunk_seconds * rate)
        pos = 0
        while pos < len(samples):
            if a.chunk_fd is None:
                self._open_chunk(a)
            take = min(len(samples) - pos, chunk_target - a.chunk_samples)
            data = encode_le(samples[pos : pos + take], a.seg.bits)
            os.write(a.chunk_fd, data)  # type: ignore[arg-type]
            a.chunk_samples += take
            a.unsynced_samples += take
            a.next_sample += take
            a.written += take
            pos += take
            if a.chunk_samples >= chunk_target:
                self._close_chunk(a)
        if a.chunk_fd is not None and a.unsynced_samples >= self.sync_interval_s * rate:
            os.fsync(a.chunk_fd)
            a.unsynced_samples = 0

    def _chunk_path(self, a: _Active, index: int, partial: bool) -> Path:
        return a.dir / f"chunk-{index:06d}.{'partial' if partial else 'pcm'}"

    def _open_chunk(self, a: _Active) -> None:
        p = self._chunk_path(a, a.chunk_index, True)
        a.chunk_fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
        a.chunk_samples = 0
        a.chunk_start = a.next_sample
        fsync_dir(a.dir)

    def _close_chunk(self, a: _Active) -> None:
        if a.chunk_fd is None:
            return
        os.fsync(a.chunk_fd)
        os.close(a.chunk_fd)
        a.chunk_fd = None
        a.unsynced_samples = 0
        src = self._chunk_path(a, a.chunk_index, True)
        if a.chunk_samples == 0:
            src.unlink(missing_ok=True)
            fsync_dir(a.dir)
            return
        dst = self._chunk_path(a, a.chunk_index, False)
        os.replace(src, dst)
        fsync_dir(a.dir)
        with transaction(self.conn):
            self.conn.execute(
                """INSERT OR REPLACE INTO audio_chunks(recording_id, chunk_index, path, start_sample, sample_count, state, created_at)
                   VALUES (?,?,?,?,?, 'closed', ?)""",
                (a.seg.recording_id, a.chunk_index, self.rel(dst), a.chunk_start, a.chunk_samples, iso_utc(time.time())),
            )
        a.chunk_index += 1

    def sync(self) -> None:
        for a in self.active.values():
            if a.chunk_fd is not None and a.unsynced_samples:
                os.fsync(a.chunk_fd)
                a.unsynced_samples = 0

    # -- close / finalize --------------------------------------------------------------------

    def close_segment(self, recording_id: str, end_sample: int, reason: str | None, incomplete: bool) -> FinalizedSegment | None:
        a = self.active.pop(recording_id, None)
        if a is None:
            return None
        self._close_chunk(a)
        if a.broken:
            incomplete = True
            reason = a.broken if reason in (None, "post_roll_complete") else f"{reason},{a.broken}"
        actual_end = a.next_sample
        if actual_end < end_sample:
            incomplete = True
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE recordings SET state='finalizing', end_sample=?, close_reason=?, incomplete=? WHERE recording_id=?",
                (actual_end, reason, int(incomplete), recording_id),
            )
        return self.finalize(recording_id)

    def finalize(self, recording_id: str) -> FinalizedSegment | None:
        """Build, verify and commit the immutable WAV from the spooled chunks (idempotent)."""
        row = self.conn.execute("SELECT * FROM recordings WHERE recording_id=?", (recording_id,)).fetchone()
        if row is None or row["state"] == "finalized":
            return None
        spool = resolve(self.state_dir, row["spool_dir"])
        manifest = json.loads((spool / "manifest.json").read_text())
        bits, rate = manifest["bits"], manifest["sample_rate"]
        bps = bits // 8
        chunks = _chunk_files(spool)
        total_bytes = 0
        for p in chunks:
            total_bytes += (p.stat().st_size // bps) * bps
        frames = total_bytes // bps
        incomplete = bool(row["incomplete"])
        expected_end = row["end_sample"]
        if expected_end is not None and row["start_sample"] + frames < expected_end:
            incomplete = True
        container = manifest.get("container", "wav")
        final = self.final_root / f"{recording_id}.{container}"
        tmp = final.with_name(final.name + ".tmp")
        if frames == 0:
            with transaction(self.conn):
                self.conn.execute(
                    "UPDATE recordings SET state='failed', sample_count=0, incomplete=1, close_reason=COALESCE(close_reason,'')||',no_audio' WHERE recording_id=?",
                    (recording_id,),
                )
            shutil.rmtree(spool, ignore_errors=True)
            return None
        if container == "flac":
            _write_flac(tmp, chunks, bits, rate, frames)
            size = tmp.stat().st_size
        else:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
            try:
                os.write(fd, wav.header(rate, bits, frames))
                remaining = total_bytes
                for p in chunks:
                    with open(p, "rb") as fh:
                        while remaining > 0 and (buf := fh.read(min(1 << 20, remaining))):
                            os.write(fd, buf)
                            remaining -= len(buf)
                os.fsync(fd)
            finally:
                os.close(fd)
            with open(tmp, "rb") as fh:
                info = wav.read_info(fh)
            size = tmp.stat().st_size
            if info.frames != frames or info.bits != bits or info.sample_rate != rate or size != wav.HEADER_BYTES + total_bytes:
                tmp.unlink(missing_ok=True)
                raise RuntimeError(f"finalized WAV verification failed for {recording_id}")
        digest = sha256_file(tmp)
        os.replace(tmp, final)
        fsync_dir(self.final_root)
        duration_ms = int(round(frames * 1000 / rate))
        delivery = "pending_declaration" if manifest.get("uploadable") else "local_only"
        with transaction(self.conn):
            self.conn.execute(
                """UPDATE recordings SET state='finalized', path=?, size_bytes=?, sha256=?, sample_count=?, end_sample=?,
                   duration_ms=?, incomplete=?, delivery_state=?, finalized_at=? WHERE recording_id=?""",
                (self.rel(final), size, digest, frames, row["start_sample"] + frames, duration_ms, int(incomplete), delivery,
                 iso_utc(time.time()), recording_id),
            )
            self.conn.execute("UPDATE audio_chunks SET state='superseded' WHERE recording_id=?", (recording_id,))
        shutil.rmtree(spool, ignore_errors=True)
        fsync_dir(self.spool_root)
        with transaction(self.conn):
            self.conn.execute("UPDATE audio_chunks SET state='deleted' WHERE recording_id=?", (recording_id,))
        log.info("finalized recording %s frames=%d sha256=%s incomplete=%s", recording_id, frames, digest, incomplete)
        return FinalizedSegment(recording_id, final, frames, size, digest, incomplete, row["close_reason"])

    def close_all(self, reason: str) -> list[FinalizedSegment]:
        out = []
        for rid, a in list(self.active.items()):
            res = self.close_segment(rid, a.next_sample, reason, incomplete=True)
            if res:
                out.append(res)
        return out


def _iter_chunk_samples(chunks: list[Path], bits: int, block: int = 1 << 16):
    bps = bits // 8
    from ..audio.pcm import decode_le

    carry = b""
    for p in chunks:
        with open(p, "rb") as fh:
            while buf := fh.read(block * bps):
                buf = carry + buf
                whole = len(buf) - len(buf) % bps
                carry = buf[whole:]
                if whole:
                    yield decode_le(buf[:whole], bits)
    # a trailing partial sample (torn write) is dropped, never padded


def _write_flac(tmp: Path, chunks: list[Path], bits: int, rate: int, frames: int) -> None:
    """Lossless FLAC via libsndfile, then a full decode compared sample-for-sample with the chunks."""
    import soundfile as sf

    if bits not in (16, 24):
        raise RuntimeError(f"FLAC evidence supports 16/24-bit PCM, not {bits}-bit")
    shift = 32 - bits
    subtype = "PCM_24" if bits == 24 else "PCM_16"
    with sf.SoundFile(str(tmp), "w", samplerate=rate, channels=1, format="FLAC", subtype=subtype) as out:
        for x in _iter_chunk_samples(chunks, bits):
            out.write((x.astype(np.int64) << shift).astype(np.int32))
    fd = os.open(tmp, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    n = 0
    with sf.SoundFile(str(tmp), "r") as dec:
        if dec.samplerate != rate or dec.channels != 1 or dec.frames != frames:
            tmp.unlink(missing_ok=True)
            raise RuntimeError("FLAC header verification failed")
        for x in _iter_chunk_samples(chunks, bits):
            got = dec.read(len(x), dtype="int32")
            if not np.array_equal((got.astype(np.int64) >> shift).astype(np.int32), x):
                tmp.unlink(missing_ok=True)
                raise RuntimeError("FLAC round-trip mismatch")
            n += len(x)
    if n != frames:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("FLAC frame count mismatch")


def _chunk_files(spool: Path) -> list[Path]:
    files = [p for p in spool.iterdir() if p.name.startswith("chunk-") and p.suffix in (".pcm", ".partial")]
    files.sort(key=lambda p: int(p.stem.split("-")[1]))
    # A closed and a partial file can coexist for the same index only if the rename raced a crash;
    # prefer the closed one.
    seen: dict[int, Path] = {}
    for p in files:
        idx = int(p.stem.split("-")[1])
        if idx not in seen or p.suffix == ".pcm":
            seen[idx] = p
    ordered = [seen[i] for i in sorted(seen)]
    for expected, p in enumerate(ordered):
        if int(p.stem.split("-")[1]) != expected:
            # A missing chunk breaks contiguity; keep only the contiguous prefix.
            return ordered[:expected]
    return ordered


def recover(conn: sqlite3.Connection, state_dir: Path) -> list[str]:
    """Reconcile spool, temp files and recording rows after a crash. Returns recovered recording ids."""
    writer = EvidenceWriter(conn, state_dir)
    recovered: list[str] = []
    for tmp in writer.final_root.glob("*.tmp"):
        tmp.unlink()
    rows = conn.execute("SELECT recording_id, state, spool_dir FROM recordings WHERE state IN ('open','finalizing')").fetchall()
    known = {r["recording_id"] for r in conn.execute("SELECT recording_id FROM recordings")}
    for r in rows:
        if r["state"] == "open":
            with transaction(conn):
                conn.execute(
                    "UPDATE recordings SET state='finalizing', incomplete=1, close_reason='process_interrupted' WHERE recording_id=?",
                    (r["recording_id"],),
                )
        if not resolve(state_dir, r["spool_dir"]).exists():
            with transaction(conn):
                conn.execute("UPDATE recordings SET state='failed', close_reason=COALESCE(close_reason,'')||',spool_missing' WHERE recording_id=?", (r["recording_id"],))
            bump_counter(conn, "recordings_unrecoverable", 1, r["recording_id"])
            continue
        if writer.finalize(r["recording_id"]):
            recovered.append(r["recording_id"])
    # Orphan spool directories (manifest written, row never committed).
    for d in writer.spool_root.iterdir():
        if not d.is_dir():
            continue
        rid = d.name
        if rid in known:
            row = conn.execute("SELECT state FROM recordings WHERE recording_id=?", (rid,)).fetchone()
            if row and row["state"] in ("finalized", "failed"):
                shutil.rmtree(d, ignore_errors=True)
            continue
        mpath = d / "manifest.json"
        if not mpath.exists():
            # No manifest means no samples could have been attributed; nothing to recover.
            if not any(d.iterdir()):
                d.rmdir()
            else:
                bump_counter(conn, "spool_orphans_unattributed", 1, str(d))
            continue
        m = json.loads(mpath.read_text())
        ev = conn.execute("SELECT 1 FROM events WHERE event_id=?", (m["event_id"],)).fetchone()
        if ev is None:
            bump_counter(conn, "spool_orphans_unattributed", 1, str(d))
            continue
        fmt = media_format(m.get("container", "wav"), m["bits"], m["sample_rate"])
        with transaction(conn):
            conn.execute(
                """INSERT INTO recordings(recording_id, event_id, segment_number, session_id, start_sample, capture_started_at,
                   format_json, provenance_json, state, incomplete, close_reason, spool_dir, delivery_state, created_at)
                   VALUES (?,?,?,?,?,?,?,?, 'finalizing', 1, 'process_interrupted', ?, 'local_only', ?)""",
                (rid, m["event_id"], m["segment_number"], m["session_id"], m["start_sample"], m["capture_started_at"],
                 dumps(fmt), dumps(m["provenance"]), writer.rel(d), iso_utc(time.time())),
            )
        if writer.finalize(rid):
            recovered.append(rid)
    return recovered
