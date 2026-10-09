"""Download, validate and stage complete configuration revisions (``GET /configuration``).

Steps 1-3 of the transactional apply happen here: fetch the complete document, verify the canonical
SHA-256 and validate it against owner-controlled local inputs, then stage it. The document carries
operational settings only; the measurement chain is local (config/chain.py).
The acquisition process applies a staged revision at a complete measurement boundary and records
the applied/rejected acknowledgment; this module never acknowledges ``applied``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time

from ..acquisition.durability import record_config_ack
from ..contract.configuration import ConfigRejected, LocalInputs, parse_document
from ..store.db import transaction
from ..timeutil import iso_utc
from ..transport.api import NOT_FOUND, OK, ApiClient

log = logging.getLogger(__name__)

CONFIG_PATH = "/api/v1/device/configuration"


def stage(conn: sqlite3.Connection, result: dict, revision: int, sha256: str) -> str:
    """Persist an immutable validated server result as ``staged``. Returns what happened."""
    with transaction(conn):
        existing = conn.execute("SELECT sha256, state FROM configurations WHERE revision=?", (revision,)).fetchone()
        if existing is not None:
            if existing["sha256"] != sha256:
                return "conflict"
            return f"known_{existing['state']}"
        newest = conn.execute("SELECT MAX(revision) r FROM configurations WHERE state IN ('applied','staged')").fetchone()["r"]
        if newest is not None and revision < newest:
            return "stale"
        conn.execute("UPDATE configurations SET state='superseded' WHERE state='staged'")
        conn.execute(
            "INSERT INTO configurations(revision, sha256, document_json, state, received_at) VALUES (?,?,?, 'staged', ?)",
            (revision, sha256, json.dumps(result, sort_keys=True), iso_utc(time.time())),
        )
    return "staged"


def poll(api: ApiClient, conn: sqlite3.Connection, local: LocalInputs) -> tuple[str, int | None]:
    """One configuration poll. Returns (result, desired revision)."""
    out = api.request("GET", CONFIG_PATH)
    if out.kind == NOT_FOUND:
        return "none", None
    if out.kind != OK:
        return f"fetch_{out.kind}:{out.error_code}", None
    body = out.body if isinstance(out.body, dict) else {}
    rev = body.get("revision")
    sha = body.get("sha256")
    if not isinstance(rev, int) or not isinstance(sha, str):
        return "malformed", None
    result = {k: v for k, v in body.items() if k not in ("request_id", "server_received_at")}
    known = conn.execute("SELECT sha256, state FROM configurations WHERE revision=?", (rev,)).fetchone()
    if known is not None and known["sha256"] == sha:
        return f"known_{known['state']}", rev
    try:
        parse_document(result, local)
    except ConfigRejected as exc:
        log.error("configuration revision %s rejected: %s", rev, exc)
        _store_rejected(conn, result, rev, sha)
        record_config_ack(conn, rev, "rejected", None, exc.code, exc.detail, sha if len(sha) == 64 else None)
        return f"rejected:{exc.code}", rev
    outcome = stage(conn, result, rev, sha)
    if outcome == "conflict":
        record_config_ack(conn, rev, "rejected", None, "revision_hash_conflict",
                          "a different document was already received for this revision", sha)
    elif outcome == "stale":
        _store_rejected(conn, result, rev, sha)
        record_config_ack(conn, rev, "rejected", None, "stale_revision",
                          "revision is older than the applied revision; rollbacks must be new revisions", sha)
    return outcome, rev


def _store_rejected(conn: sqlite3.Connection, result: dict, rev: int, sha: str) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT OR IGNORE INTO configurations(revision, sha256, document_json, state, received_at) VALUES (?,?,?, 'rejected', ?)",
            (rev, str(sha)[:64], json.dumps(result, sort_keys=True)[:400_000], iso_utc(time.time())),
        )
