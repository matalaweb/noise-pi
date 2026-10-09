"""Download, validate and stage complete configuration revisions (``GET /configuration``).

Steps 1-3 of the transactional apply happen here: fetch the complete document + provenance,
verify the canonical SHA-256 and translate it against owner-controlled local inputs, then stage it.
The acquisition process applies a staged revision at a complete measurement boundary and records
the applied/rejected acknowledgment; this module never acknowledges ``applied``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from pathlib import Path

from ..acquisition.durability import record_config_ack
from ..contract.configuration import ConfigRejected, LocalInputs, Provenance, translate
from ..dsp.calibration import parse_calibration_file
from ..evidence.fsutil import atomic_write
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
        for prof in result.get("provenance", {}).get("measurement_profiles", []):
            conn.execute(
                "INSERT OR IGNORE INTO profiles(profile_id, sha256, document_json, received_at) VALUES (?,?,?,?)",
                (prof["id"], prof["content_hash"], json.dumps(prof, sort_keys=True), iso_utc(time.time())),
            )
    return "staged"


class AssetPending(Exception):
    """A referenced calibration file could not be downloaded yet (transient)."""


MAX_ASSET_BYTES = 4 * 1024 * 1024


def fetch_calibration_files(api: ApiClient, conn: sqlite3.Connection, result: dict, asset_dir: Path) -> list[str]:
    """Download and SHA-256-verify every frequency-response file referenced by the provenance.

    Files are stored by hash under ``asset_dir`` (immutable; never re-downloaded once verified).
    Only paths on the API origin are fetched, with the device token. Returns downloaded names.
    """
    try:
        prov = Provenance.model_validate(result.get("provenance") or {})
    except Exception as exc:
        raise ConfigRejected("malformed_configuration", f"provenance: {exc}"[:500]) from exc
    fetched = []
    for cal in prov.calibrations:
        for att in cal.frequency_response_files():
            dest = asset_dir / att.sha256
            if dest.exists() and hashlib.sha256(dest.read_bytes()).hexdigest() == att.sha256:
                continue
            if att.byte_size > MAX_ASSET_BYTES or not att.download_path.startswith("/api/v1/device/"):
                raise ConfigRejected("asset_not_permitted", f"{att.filename}: size or path not permitted")
            out, data = api.get_bytes(att.download_path, max_bytes=MAX_ASSET_BYTES)
            if out.kind != OK or data is None:
                raise AssetPending(f"{att.filename}: {out.kind} {out.error_code}")
            if hashlib.sha256(data).hexdigest() != att.sha256:
                raise ConfigRejected("asset_hash_mismatch", att.filename)
            try:
                parse_calibration_file(data)
            except ValueError as exc:
                raise ConfigRejected("invalid_calibration_file", f"{att.filename}: {exc}") from exc
            atomic_write(dest, data)
            with transaction(conn):
                conn.execute(
                    "INSERT OR REPLACE INTO profile_assets(sha256, profile_id, filename, path, size_bytes, received_at) VALUES (?,?,?,?,?,?)",
                    (att.sha256, cal.id, att.filename, f"profiles/{att.sha256}", len(data), iso_utc(time.time())),
                )
            fetched.append(att.filename)
    return fetched


def _check_immutable_profiles(conn: sqlite3.Connection, result: dict) -> None:
    for prof in result.get("provenance", {}).get("measurement_profiles", []):
        prev = conn.execute("SELECT sha256 FROM profiles WHERE profile_id=?", (prof.get("id"),)).fetchone()
        if prev is not None and prev["sha256"] != prof.get("content_hash"):
            raise ConfigRejected("profile_mutated", f"profile {prof.get('id')} content hash changed under the same id")


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
        _check_immutable_profiles(conn, result)
        if local.asset_dir is not None:
            fetch_calibration_files(api, conn, result, Path(local.asset_dir))
        translate(result, local)
    except AssetPending as exc:
        return f"asset_retry:{exc}", rev
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
