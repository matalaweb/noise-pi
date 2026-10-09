"""Immutable measurement batch outbox.

A batch is created once, in one transaction that stores the exact serialized request body and
moves its records to ``batched``. Retries resend those bytes unchanged (same batch_id, sent_at,
schema_version, sequence identities). A batch becomes ``acknowledged`` only after a durable
success response naming the batch and committed counts. Lost acknowledgments replay safely.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid

from ..contract.models import BatchResult
from ..store.db import bump_counter, transaction
from ..timeutil import iso_utc

MAX_RECORDS = 300
MAX_BYTES = 1024 * 1024 - 4096  # stay below 1 MiB after serialization
SENDING_LEASE_S = 180.0
CURRENT_WINDOW_S = 180


def serialize_batch(batch_id: str, sent_at: str, records: list[dict]) -> str:
    return json.dumps({"schema_version": 1, "batch_id": batch_id, "sent_at": sent_at, "records": records},
                      separators=(",", ":"), sort_keys=True)


def expire_old(conn: sqlite3.Connection, now: float, window_days: float) -> int:
    """Mark data older than the server's backfill window; it needs an owner-enabled import."""
    cutoff = int(now - window_days * 86400 + 3600)
    with transaction(conn):
        n = conn.execute(
            "UPDATE measurements SET delivery_state='expired_for_automatic_upload' WHERE delivery_state='pending' AND utc_second < ?",
            (cutoff,),
        ).rowcount
        conn.execute(
            "UPDATE outbox_batches SET state='expired_for_automatic_upload' WHERE state='pending' AND last_second < ?", (cutoff,)
        )
        conn.execute(
            """UPDATE measurements SET delivery_state='expired_for_automatic_upload' WHERE delivery_state='batched'
               AND batch_id IN (SELECT batch_id FROM outbox_batches WHERE state='expired_for_automatic_upload')"""
        )
    if n:
        bump_counter(conn, "measurements_expired_for_automatic_upload", n)
    return n


def build_batches(conn: sqlite3.Connection, now: float, *, batch_seconds: int = 30, max_records: int = MAX_RECORDS,
                  max_bytes: int = MAX_BYTES, force: bool = False) -> list[str]:
    """Create batches from pending measurements once the oldest is ``batch_seconds`` old (or a full batch exists)."""
    created: list[str] = []
    while True:
        head = conn.execute(
            "SELECT MIN(utc_second) oldest, COUNT(*) n FROM measurements WHERE delivery_state='pending'"
        ).fetchone()
        if not head["n"]:
            return created
        if not force and head["n"] < max_records and now - head["oldest"] < batch_seconds:
            return created
        rows = conn.execute(
            """SELECT id, utc_second, wire_json FROM measurements WHERE delivery_state='pending'
               ORDER BY utc_second, session_id, sequence LIMIT ?""",
            (max_records,),
        ).fetchall()
        batch_id = str(uuid.uuid4())
        sent_at = iso_utc(now)
        chosen: list[sqlite3.Row] = []
        records: list[dict] = []
        for r in rows:
            rec = json.loads(r["wire_json"])
            trial = serialize_batch(batch_id, sent_at, records + [rec])
            if len(trial.encode()) > max_bytes:
                if not records:
                    with transaction(conn):
                        conn.execute("UPDATE measurements SET delivery_state='quarantined' WHERE id=?", (r["id"],))
                    bump_counter(conn, "measurements_quarantined_oversize", 1)
                break
            records.append(rec)
            chosen.append(r)
        if not chosen:
            continue
        payload = serialize_batch(batch_id, sent_at, records)
        _insert_batch(conn, batch_id, payload, chosen, now)
        created.append(batch_id)
        if len(chosen) < max_records and not force:
            # Remaining pending rows are younger than the batching window.
            continue


def _insert_batch(conn: sqlite3.Connection, batch_id: str, payload: str, rows: list[sqlite3.Row], now: float,
                  replaces: str | None = None) -> None:
    with transaction(conn):
        conn.execute(
            """INSERT INTO outbox_batches(batch_id, payload, record_count, byte_size, first_second, last_second, state,
               created_at, replaces_batch_id) VALUES (?,?,?,?,?,?, 'pending', ?, ?)""",
            (batch_id, payload, len(rows), len(payload.encode()), rows[0]["utc_second"], rows[-1]["utc_second"],
             iso_utc(now), replaces),
        )
        conn.executemany(
            "UPDATE measurements SET delivery_state='batched', batch_id=? WHERE id=?", [(batch_id, r["id"]) for r in rows]
        )


def reset_leases(conn: sqlite3.Connection, now: float, all_leases: bool = False) -> int:
    with transaction(conn):
        if all_leases:
            return conn.execute("UPDATE outbox_batches SET state='pending', lease_expires_at=NULL WHERE state='sending'").rowcount
        return conn.execute(
            "UPDATE outbox_batches SET state='pending', lease_expires_at=NULL WHERE state='sending' AND lease_expires_at < ?",
            (now,),
        ).rowcount


def next_batch(conn: sqlite3.Connection, now: float, prefer_current: bool) -> sqlite3.Row | None:
    """Current (recent) batches first to keep the live stream fresh, then the oldest backlog."""
    if prefer_current:
        row = conn.execute(
            """SELECT * FROM outbox_batches WHERE state='pending' AND next_attempt_at <= ? AND last_second >= ?
               ORDER BY first_second DESC LIMIT 1""",
            (now, int(now) - CURRENT_WINDOW_S),
        ).fetchone()
        if row is not None:
            return row
    return conn.execute(
        "SELECT * FROM outbox_batches WHERE state='pending' AND next_attempt_at <= ? ORDER BY first_second LIMIT 1", (now,)
    ).fetchone()


def lease(conn: sqlite3.Connection, batch_id: str, now: float) -> bool:
    with transaction(conn):
        n = conn.execute(
            "UPDATE outbox_batches SET state='sending', lease_expires_at=?, attempts=attempts+1 WHERE batch_id=? AND state='pending'",
            (now + SENDING_LEASE_S, batch_id),
        ).rowcount
    return n == 1


def validate_ack(row: sqlite3.Row, ack: BatchResult) -> str | None:
    """A 2xx counts only if it names this batch and accounts for every record (new or duplicate)."""
    if ack.batch_id.lower() != row["batch_id"]:
        return "ack_batch_id_mismatch"
    if ack.record_count != row["record_count"] or ack.inserted_count + ack.duplicate_count != ack.record_count:
        return "ack_count_mismatch"
    return None


def expire_batch(conn: sqlite3.Connection, batch_id: str, error: str | None) -> None:
    """Server says outside its backfill window: keep the data, stop automatic posting."""
    with transaction(conn):
        conn.execute(
            "UPDATE outbox_batches SET state='expired_for_automatic_upload', lease_expires_at=NULL, last_error=? WHERE batch_id=?",
            (error, batch_id),
        )
        conn.execute("UPDATE measurements SET delivery_state='expired_for_automatic_upload' WHERE batch_id=?", (batch_id,))
    bump_counter(conn, "batches_outside_backfill_window", 1, error)


def mark_acknowledged(conn: sqlite3.Connection, batch_id: str, response: dict, now: float) -> None:
    with transaction(conn):
        conn.execute(
            """UPDATE outbox_batches SET state='acknowledged', acknowledged_at=?, response_json=?, lease_expires_at=NULL,
               last_status=200, last_error=NULL WHERE batch_id=?""",
            (iso_utc(now), json.dumps(response, sort_keys=True), batch_id),
        )
        conn.execute("UPDATE measurements SET delivery_state='acknowledged' WHERE batch_id=?", (batch_id,))


def mark_retry(conn: sqlite3.Connection, batch_id: str, delay: float, now: float, status: int | None, error: str | None,
               count_attempt: bool = True) -> None:
    with transaction(conn):
        conn.execute(
            f"""UPDATE outbox_batches SET state='pending', next_attempt_at=?, lease_expires_at=NULL, last_status=?, last_error=?
                {'' if count_attempt else ', attempts=attempts-1'} WHERE batch_id=?""",
            (now + delay, status, error, batch_id),
        )


def quarantine(conn: sqlite3.Connection, batch_id: str, status: int | None, error: str | None) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE outbox_batches SET state='quarantined', lease_expires_at=NULL, last_status=?, last_error=? WHERE batch_id=?",
            (status, error, batch_id),
        )
        conn.execute("UPDATE measurements SET delivery_state='quarantined' WHERE batch_id=?", (batch_id,))
    bump_counter(conn, "batches_quarantined", 1, f"{status} {error}")


def split_oversized(conn: sqlite3.Connection, batch_id: str, now: float) -> list[str]:
    """After a confirmed 413: quarantine the envelope and rebuild smaller batches around the same records."""
    with transaction(conn):
        conn.execute(
            "UPDATE outbox_batches SET state='quarantined', lease_expires_at=NULL, last_status=413, last_error='too_large' WHERE batch_id=?",
            (batch_id,),
        )
    rows = conn.execute(
        "SELECT id, utc_second, wire_json FROM measurements WHERE batch_id=? ORDER BY utc_second, session_id, sequence", (batch_id,)
    ).fetchall()
    if len(rows) <= 1:
        with transaction(conn):
            conn.execute("UPDATE measurements SET delivery_state='quarantined' WHERE batch_id=?", (batch_id,))
        bump_counter(conn, "measurements_quarantined_oversize", len(rows))
        return []
    half = (len(rows) + 1) // 2
    out = []
    for part in (rows[:half], rows[half:]):
        new_id = str(uuid.uuid4())
        payload = serialize_batch(new_id, iso_utc(now), [json.loads(r["wire_json"]) for r in part])
        _insert_batch(conn, new_id, payload, part, now, replaces=batch_id)
        out.append(new_id)
    bump_counter(conn, "batches_split_after_413", 1)
    return out


def backlog(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        """SELECT COUNT(*) n, MIN(utc_second) oldest FROM measurements WHERE delivery_state IN ('pending','batched')"""
    ).fetchone()
    by_state = {r["state"]: r["n"] for r in conn.execute("SELECT state, COUNT(*) n FROM outbox_batches GROUP BY state")}
    return {"queued_measurements": row["n"], "oldest_pending_second": row["oldest"], "batches": by_state, "at": time.time()}
