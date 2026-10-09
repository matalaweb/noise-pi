"""SQLite connection management, migrations, and backup.

* WAL journal, foreign keys on, ``synchronous=FULL`` for durability-sensitive commits, bounded
  busy timeout. Each process/thread opens its own connection; transactions stay short.
* Migrations are versioned SQL files applied in order inside a transaction before capture
  starts. A pre-migration online backup is written next to the database so a failed upgrade can
  be rolled back (docs/operations.md).
* Backups use SQLite's online backup API, which includes committed WAL content. Never copy only
  the ``.db`` file of a live database.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from collections.abc import Iterator
from importlib import resources
from pathlib import Path

from ..timeutil import iso_utc

BUSY_TIMEOUT_MS = 5000


def _migrations() -> list[tuple[int, str, str]]:
    out = []
    pkg = resources.files("noise_collector.store.migrations")
    for entry in pkg.iterdir():
        name = entry.name
        if name.endswith(".sql") and name[:4].isdigit():
            out.append((int(name[:4]), name, entry.read_text()))
    return sorted(out)


LATEST_SCHEMA = max(v for v, _, _ in _migrations())


def connect(path: Path | str, *, readonly: bool = False, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open a connection. ``check_same_thread=False`` only for a connection handed to exactly one other thread."""
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    else:
        conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None,
                               check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    if not readonly:
        _enable_wal(conn)
        conn.execute("PRAGMA synchronous=FULL")
    return conn


def _enable_wal(conn: sqlite3.Connection) -> None:
    """Switching a new database to WAL takes an exclusive lock the busy handler does not wait for
    (acquisition and delivery open the fresh database at the same moment), so retry it here."""
    deadline = time.monotonic() + BUSY_TIMEOUT_MS / 1000
    while True:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or time.monotonic() > deadline:
                raise
            time.sleep(0.02)


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection, immediate: bool = True) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def schema_version(conn: sqlite3.Connection) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)")
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return int(row[0] or 0)


def migrate(path: Path, *, backup_dir: Path | None = None) -> int:
    """Apply pending migrations. Returns the resulting schema version."""
    conn = connect(path)
    try:
        current = schema_version(conn)
        if current > LATEST_SCHEMA:
            raise RuntimeError(
                f"database schema {current} is newer than this agent ({LATEST_SCHEMA}); refuse to downgrade"
            )
        pending = [m for m in _migrations() if m[0] > current]
        if pending and current > 0 and backup_dir is not None:
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup(conn, backup_dir / f"pre-migration-v{current}-{int(time.time())}.db")
        for version, name, sql in pending:
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Acquisition and delivery start together and both migrate: re-check under the
                # write lock so the second one skips what the first just applied.
                if schema_version(conn) >= version:
                    conn.execute("COMMIT")
                    continue
                for stmt in _split_sql(sql):
                    conn.execute(stmt)
                conn.execute(
                    "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?,?,?)",
                    (version, name, iso_utc(time.time())),
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return schema_version(conn)
    finally:
        conn.close()


def _split_sql(sql: str) -> list[str]:
    lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


def backup(conn: sqlite3.Connection, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    out = sqlite3.connect(str(dest))
    try:
        conn.backup(out)
    finally:
        out.close()


def checkpoint(conn: sqlite3.Connection) -> tuple[int, int, int]:
    row = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
    return tuple(row)  # type: ignore[return-value]


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO metadata(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def bump_counter(conn: sqlite3.Connection, name: str, delta: int = 1, error: str | None = None) -> None:
    conn.execute(
        """INSERT INTO health_counters(name, value, updated_at, last_error) VALUES (?,?,?,?)
           ON CONFLICT(name) DO UPDATE SET value=value+excluded.value, updated_at=excluded.updated_at,
           last_error=COALESCE(excluded.last_error, last_error)""",
        (name, delta, iso_utc(time.time()), error),
    )


def counters(conn: sqlite3.Connection) -> dict[str, int]:
    return {r["name"]: r["value"] for r in conn.execute("SELECT name, value FROM health_counters")}


def dumps(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))
