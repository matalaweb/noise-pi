"""Register the device's measurement chain with the server (``POST /api/v1/device/provenance``).

The acquisition process queues the profile/calibration records it measures with
(``config/chain.py``). They must be registered before any measurement or event referencing them is
sent, so the control lane calls ``register`` first and holds measurement/event delivery while
anything is pending. Registration is idempotent on the server (same id + same content = no-op), so a
re-registration after ``unknown_provenance`` is always safe.
"""

from __future__ import annotations

import json
import logging
import sqlite3

from ..store.db import transaction
from ..timeutil import iso_utc
from ..transport.api import AUTH, CONFLICT, INVALID, OK, TOO_LARGE, ApiClient, Outcome

log = logging.getLogger(__name__)

P_PROVENANCE = "/api/v1/device/provenance"
MAX_PER_KIND = 8
LIST_KEY = {"measurement_profile": "measurement_profiles", "calibration": "calibrations"}


def pending_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM provenance_records WHERE state='pending'").fetchone()[0])


def summary(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT state, COUNT(*) n FROM provenance_records GROUP BY state").fetchall()
    out: dict = {r["state"]: r["n"] for r in rows}
    err = conn.execute("SELECT last_error FROM provenance_records WHERE state='rejected' ORDER BY created_at DESC LIMIT 1").fetchone()
    if err is not None:
        out["last_rejection"] = err["last_error"]
    return out


def reregister_all(conn: sqlite3.Connection) -> None:
    """The server reported an unknown profile/calibration: register everything again."""
    with transaction(conn):
        conn.execute("UPDATE provenance_records SET state='pending', next_attempt_at=0 WHERE state='registered'")


def register(api: ApiClient, conn: sqlite3.Connection, now: float, delay) -> tuple[str, Outcome | None]:
    """Send due pending records in one request. Returns (result, outcome) for the caller's bookkeeping.

    ``delay(attempts, outcome)`` computes the retry delay for transient failures.
    """
    rows = conn.execute(
        "SELECT * FROM provenance_records WHERE state='pending' AND next_attempt_at <= ? ORDER BY created_at, record_id", (now,)
    ).fetchall()
    if not rows:
        return "idle", None
    body: dict = {"schema_version": 1, "sent_at": iso_utc(now), "measurement_profiles": [], "calibrations": []}
    sent = []
    for r in rows:
        lst = body[LIST_KEY[r["kind"]]]
        if len(lst) < MAX_PER_KIND:
            lst.append(json.loads(r["payload_json"]))
            sent.append(r)
    out = api.request("POST", P_PROVENANCE, content=json.dumps(body, separators=(",", ":")).encode())
    ids = [r["record_id"] for r in sent]
    marks = ",".join("?" * len(ids))
    if out.kind == OK:
        echoed = set()
        if isinstance(out.body, dict):
            for key in LIST_KEY.values():
                echoed.update(str(i.get("id", "")).lower() for i in out.body.get(key, []) if isinstance(i, dict))
        done = [i for i in ids if i in echoed]
        missing = [i for i in ids if i not in echoed]
        if missing:
            log.warning("provenance registration acknowledged without ids %s; will retry", missing)
        with transaction(conn):
            for i in done:
                conn.execute("UPDATE provenance_records SET state='registered', registered_at=?, attempts=attempts+1, last_status=?, "
                             "last_error=NULL WHERE record_id=?", (iso_utc(now), out.status, i))
            for i in missing:
                conn.execute("UPDATE provenance_records SET attempts=attempts+1, next_attempt_at=? WHERE record_id=?", (now + 60, i))
        if done:
            log.info("registered measurement chain records %s", done)
        return "registered" if not missing else "partial", out
    if out.kind == AUTH:
        return "auth", out
    error = f"{out.status} {out.error_code or out.kind}"
    if out.details:
        error += " " + json.dumps(out.details, sort_keys=True)[:1500]
    if out.kind in (CONFLICT, INVALID, TOO_LARGE):
        # Permanent for this content: needs a local fix (a changed chain gets new ids and registers).
        log.error("measurement chain registration rejected: %s", error)
        with transaction(conn):
            conn.execute(f"UPDATE provenance_records SET state='rejected', attempts=attempts+1, last_status=?, last_error=? "
                         f"WHERE record_id IN ({marks})", (out.status, error[:2000], *ids))
        return f"rejected:{out.error_code}", out
    with transaction(conn):
        for r in sent:
            conn.execute("UPDATE provenance_records SET attempts=attempts+1, next_attempt_at=?, last_status=?, last_error=? WHERE record_id=?",
                         (now + delay(r["attempts"], out), out.status, error[:2000], r["record_id"]))
    return f"retry:{out.kind}", out
