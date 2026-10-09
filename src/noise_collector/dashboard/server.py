"""Optional local live dashboard: read-only levels, events and health. Never audio.

* Separate process; reads SQLite through a read-only connection and the runtime status files,
  so it cannot block or alter capture, storage or delivery.
* Read-only, except one optional owner control (``allow_stop_event``): ``POST
  /api/events/<id>/stop`` drops a request the acquisition process acts on (health/control.py).
* Binds to loopback by default (reach it through an SSH tunnel). Binding any other address
  requires an access token file; requests then need ``Authorization: Bearer <token>`` or a
  one-time ``?token=`` that sets an HttpOnly, SameSite=Strict cookie. The owner may opt out
  explicitly with ``allow_unauthenticated_lan`` (trusted private network only).
* Live updates: Server-Sent Events push every newly committed one-second measurement (1 Hz, the
  instrument's measurement resolution) together with detector/microphone state.
* No third-party assets: the page is self-contained (strict CSP), so it works on a LAN without
  internet access.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import re
import secrets
import sqlite3
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..config.settings import Settings
from ..health.control import request_stop_event
from ..health.status import read_status, run_dir
from ..store.db import connect

log = logging.getLogger(__name__)
STOP_PATH = re.compile(r"/api/events/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/stop")

METRICS = ("laeq_db", "lafmax_db", "lceq_db", "low_frequency_leq_db", "rms_dbfs")
MAX_HISTORY_SECONDS = 7 * 86400
MAX_POINTS = 4000
CSP = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"


class DashboardError(RuntimeError):
    pass


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in ("localhost",)


class DataSource:
    """All reads the dashboard performs. Short read-only queries; one connection per thread."""

    def __init__(self, db_path: Path, state_dir: Path) -> None:
        self.db_path = db_path
        self.state_dir = state_dir
        self._local = threading.local()

    def conn(self) -> sqlite3.Connection | None:
        c = getattr(self._local, "conn", None)
        if c is None:
            if not self.db_path.exists():
                return None
            c = connect(self.db_path, readonly=True)
            self._local.conn = c
        return c

    def latest_second(self) -> int | None:
        c = self.conn()
        if c is None:
            return None
        row = c.execute("SELECT MAX(utc_second) FROM measurements").fetchone()
        return row[0]

    def measurements(self, since: int, until: int | None = None, limit: int = MAX_POINTS) -> list[dict]:
        """Per-second points after ``since`` (exclusive). Downsampled by max over buckets when long."""
        c = self.conn()
        if c is None:
            return []
        until = until if until is not None else 2**62
        rows = c.execute(
            """SELECT utc_second, status, omit_reason, wire_json, diagnostics_json, delivery_state, timing_trusted
               FROM measurements WHERE utc_second > ? AND utc_second <= ? ORDER BY utc_second""",
            (since, until),
        ).fetchall()
        points = [self._point(r) for r in rows]
        if len(points) > limit:
            points = _downsample(points, limit)
        return points

    @staticmethod
    def _point(r: sqlite3.Row) -> dict:
        p: dict = {"t": r["utc_second"], "ok": r["status"] == "complete"}
        if r["wire_json"]:
            w = json.loads(r["wire_json"])
            for m in METRICS:
                p[m] = w.get(m)
            p["flags"] = w.get("quality_flags", [])
        else:
            p["omit"] = r["omit_reason"]
        p["upload"] = r["delivery_state"]
        if not r["timing_trusted"]:
            p["untrusted_time"] = True
        return p

    def events(self, since: int, limit: int = 200) -> list[dict]:
        c = self.conn()
        if c is None:
            return []
        out = []
        for e in c.execute(
            """SELECT e.event_id, e.state, e.start_second, e.termination_reason, r.payload_json, r.delivery_state
               FROM events e JOIN event_revisions r ON r.event_id = e.event_id AND r.revision = e.latest_revision
               WHERE e.start_second >= ? ORDER BY e.start_second DESC LIMIT ?""",
            (since, limit),
        ):
            p = json.loads(e["payload_json"])
            out.append({
                "event_id": e["event_id"],
                "state": e["state"],
                "detection_state": p.get("detection_state"),
                "started_at": p.get("started_at"),
                "ended_at": p.get("ended_at"),
                "start_second": e["start_second"],
                "trigger": p.get("detection"),
                "summary": p.get("summary"),
                "quality_flags": p.get("quality_flags", []),
                "recording": p.get("recording"),
                "upload": e["delivery_state"],
            })
        return out

    def status(self) -> dict:
        rd = run_dir(self.state_dir)
        acq = read_status(rd / "acquisition-status.json") or {}
        dl = read_status(rd / "delivery-status.json") or {}
        eng = acq.get("engine") or {}
        storage = acq.get("storage") or {}
        return {
            "now": time.time(),
            "acquisition_age_s": acq.get("age_s"),
            "microphone_state": acq.get("microphone_state"),
            "durable_capture": acq.get("durable_capture"),
            "latest_capture_error": acq.get("latest_capture_error"),
            "profile_mode": eng.get("profile_mode"),
            "scale_available": eng.get("scale_available"),
            "configuration_revision": eng.get("configuration_revision"),
            "detection": eng.get("detection"),
            "clock": acq.get("clock"),
            "host": acq.get("host"),
            "storage": {k: storage.get(k) for k in ("state", "volume_free", "volume_total", "audio_used", "audio_quota")},
            "delivery": {
                "auth_blocked": dl.get("auth_blocked"),
                "last_heartbeat_success": dl.get("last_heartbeat_success"),
                "last_batch_ack": dl.get("last_batch_ack"),
                "queued_measurements": (dl.get("backlog") or {}).get("queued_measurements"),
                "age_s": dl.get("age_s"),
            },
        }


def _downsample(points: list[dict], limit: int) -> list[dict]:
    """Max-preserving bucketing so short loud events stay visible in long views."""
    bucket = -(-len(points) // limit)
    out = []
    for i in range(0, len(points), bucket):
        chunk = points[i : i + bucket]
        p = {"t": chunk[0]["t"], "ok": any(c["ok"] for c in chunk), "bucket_s": len(chunk)}
        for m in METRICS:
            vals = [c.get(m) for c in chunk if c.get(m) is not None]
            p[m] = max(vals) if vals else None
        out.append(p)
    return out


def _page() -> bytes:
    return resources.files("noise_collector.dashboard").joinpath("page.html").read_bytes()


class Handler(BaseHTTPRequestHandler):
    server_version = "noise-collector-dashboard"
    data: DataSource
    token: str | None
    allow_stop_event: bool = False
    sse_interval: float = 1.0

    def log_message(self, fmt: str, *args) -> None:  # keep logs bounded and token-free
        log.debug("dashboard %s %s", self.address_string(), fmt % args)

    # -- auth --------------------------------------------------------------------------------

    def _authorized(self, query: dict) -> tuple[bool, str | None]:
        if self.token is None:
            return True, None
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].strip(), self.token):
            return True, None
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "nc_dash" and hmac.compare_digest(v, self.token):
                return True, None
        q = query.get("token", [None])[0]
        if q and hmac.compare_digest(q, self.token):
            return True, f"nc_dash={self.token}; HttpOnly; SameSite=Strict; Path=/"
        return False, None

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, extra: dict | None = None) -> None:
        self._send(200, json.dumps(obj, default=str).encode(), "application/json", extra)

    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        q = parse_qs(u.query)
        ok, cookie = self._authorized(q)
        if not ok:
            self._send(HTTPStatus.UNAUTHORIZED, b"unauthorized", "text/plain", {"WWW-Authenticate": "Bearer"})
            return
        extra = {"Set-Cookie": cookie} if cookie else None
        try:
            if u.path == "/":
                if cookie:  # drop the token from the address bar after it set the cookie
                    self._send(HTTPStatus.SEE_OTHER, b"", "text/plain", {"Location": "/", **(extra or {})})
                    return
                self._send(200, _page(), "text/html; charset=utf-8")
            elif u.path == "/api/status":
                self._json({**self.data.status(), "controls": {"stop_event": self.allow_stop_event}}, extra)
            elif u.path == "/api/measurements":
                latest = self.data.latest_second() or int(time.time())
                seconds = min(MAX_HISTORY_SECONDS, max(60, int(q.get("seconds", ["600"])[0])))
                self._json({"latest": latest, "points": self.data.measurements(latest - seconds)}, extra)
            elif u.path == "/api/events":
                seconds = min(MAX_HISTORY_SECONDS, max(60, int(q.get("seconds", ["86400"])[0])))
                latest = self.data.latest_second() or int(time.time())
                self._json({"events": self.data.events(latest - seconds)}, extra)
            elif u.path == "/api/stream":
                self._stream(int(q.get("since", ["0"])[0] or 0))
            else:
                self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except ValueError:
            self._send(HTTPStatus.BAD_REQUEST, b"bad request", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        """The only write: ``POST /api/events/<id>/stop`` when ``allow_stop_event`` is on."""
        u = urlparse(self.path)
        m = STOP_PATH.fullmatch(u.path)
        if not (self.allow_stop_event and m):
            self._read_only()
            return
        ok, _ = self._authorized({})
        if not ok:
            self._send(HTTPStatus.UNAUTHORIZED, b"unauthorized", "text/plain", {"WWW-Authenticate": "Bearer"})
            return
        # A custom header cannot be sent cross-site without a CORS preflight this server never grants,
        # so other web pages open in the owner's browser cannot trigger the stop.
        if self.headers.get("X-Noise-Collector") != "stop-event":
            self._send(HTTPStatus.FORBIDDEN, b"missing X-Noise-Collector header", "text/plain")
            return
        event_id = m.group(1)
        if (self.data.status().get("detection") or {}).get("event_id") != event_id:
            self._send(HTTPStatus.CONFLICT, json.dumps({"error": "not the open event"}).encode(), "application/json")
            return
        request_stop_event(self.data.state_dir, event_id)
        log.info("owner requested stop of event %s from %s", event_id, self.address_string())
        self._send(HTTPStatus.ACCEPTED, json.dumps({"event_id": event_id, "status": "stop requested"}).encode(), "application/json")

    def _read_only(self) -> None:
        self._send(HTTPStatus.METHOD_NOT_ALLOWED, b"read-only", "text/plain", {"Allow": "GET"})

    def do_PUT(self) -> None:  # noqa: N802
        self._read_only()

    do_DELETE = do_PATCH = do_PUT  # noqa: N815

    def _stream(self, since: int) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        last = since or (self.data.latest_second() or 0)
        last_status = 0.0
        stop = self.server.stop_event  # type: ignore[attr-defined]
        while not stop.is_set():
            pts = self.data.measurements(last, limit=10_000)
            if pts:
                last = pts[-1]["t"]
                self.wfile.write(f"event: points\ndata: {json.dumps(pts)}\n\n".encode())
            now = time.monotonic()
            if now - last_status >= 1.0:
                last_status = now
                status = {**self.data.status(), "controls": {"stop_event": self.allow_stop_event}}
                self.wfile.write(f"event: status\ndata: {json.dumps(status, default=str)}\n\n".encode())
            self.wfile.flush()
            stop.wait(self.sse_interval)


class DashboardServer:
    def __init__(self, settings: Settings, *, host: str | None = None, port: int | None = None) -> None:
        d = settings.dashboard
        self.host = host or d.bind
        self.port = d.port if port is None else port
        token = None
        if d.access_token_file:
            token = Path(d.access_token_file).read_text().strip()
            if len(token) < 24:
                raise DashboardError("dashboard access token must be at least 24 characters")
        if not _is_loopback(self.host) and token is None:
            if not d.allow_unauthenticated_lan:
                raise DashboardError(f"refusing to bind {self.host} without dashboard.access_token_file "
                                     "(or an explicit dashboard.allow_unauthenticated_lan = true)")
            log.warning("dashboard bound to %s without an access token (allow_unauthenticated_lan): anyone on the network can view it",
                        self.host)
        handler = type("BoundHandler", (Handler,), {"data": DataSource(settings.db_path, settings.state_dir), "token": token,
                                                    "allow_stop_event": d.allow_stop_event})
        self.httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self.httpd.daemon_threads = True
        self.httpd.stop_event = threading.Event()  # type: ignore[attr-defined]

    @property
    def address(self) -> tuple[str, int]:
        return self.httpd.server_address[:2]  # type: ignore[return-value]

    def serve_forever(self) -> None:
        log.info("dashboard listening on http://%s:%s", *self.address)
        self.httpd.serve_forever(poll_interval=0.5)

    def shutdown(self) -> None:
        self.httpd.stop_event.set()  # type: ignore[attr-defined]
        self.httpd.shutdown()
        self.httpd.server_close()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def main(settings: Settings) -> int:
    import signal

    srv = DashboardServer(settings)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=srv.shutdown, daemon=True).start())
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
    return 0
