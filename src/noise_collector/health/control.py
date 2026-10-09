"""Owner control requests from the local dashboard to the acquisition process.

The dashboard never touches capture. It drops a small request file in the runtime directory; the
acquisition loop takes it (once per second), checks that it still names the open event, and acts on
it at an interval boundary. Requests older than ``MAX_AGE_S`` are ignored.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .status import run_dir

STOP_EVENT = "stop-event.json"
MAX_AGE_S = 60.0


def control_dir(state_dir: Path) -> Path:
    return run_dir(state_dir) / "control"


def request_stop_event(state_dir: Path, event_id: str) -> None:
    d = control_dir(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{STOP_EVENT}.{os.getpid()}"
    tmp.write_text(json.dumps({"event_id": event_id, "requested_at": time.time()}))
    os.replace(tmp, d / STOP_EVENT)


def take_stop_event_request(state_dir: Path, now: float | None = None) -> str | None:
    """The requested event id (consuming the request), or None if there is no fresh request."""
    p = control_dir(state_dir) / STOP_EVENT
    try:
        raw = p.read_text()
        p.unlink()
    except FileNotFoundError:
        return None
    try:
        req = json.loads(raw)
        if (now if now is not None else time.time()) - float(req["requested_at"]) > MAX_AGE_S:
            return None
        return str(req["event_id"])
    except (ValueError, KeyError, TypeError):
        return None
