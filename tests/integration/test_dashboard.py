"""Local live dashboard: read-only, loopback by default, token-gated on the LAN, never audio."""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from conftest import START_UTC, engine_scenario
from noise_collector.acquisition.replay import replay
from noise_collector.contract.examples import example_configuration
from noise_collector.dashboard.server import DashboardError, DashboardServer
from noise_collector.health.status import write_status


@pytest.fixture(scope="module")
def replayed(tmp_path_factory):
    from noise_collector.synth import write_wav

    d = tmp_path_factory.mktemp("dash")
    wav = d / "e.wav"
    write_wav(str(wav), engine_scenario().render())
    replay(str(wav), d / "state", example_configuration(), start_utc=START_UTC)
    return d / "state"


@pytest.fixture
def server(replayed, make_settings, monkeypatch, tmp_path):
    run = tmp_path / "run"
    monkeypatch.setenv("NOISE_COLLECTOR_RUN_DIR", str(run))
    write_status(run / "acquisition-status.json", {
        "microphone_state": "ok", "durable_capture": "ok", "clock": {"synchronized": True},
        "engine": {"profile_mode": "calibrated", "scale_available": True,
                   "detection": {"state": "idle", "baselines": {"laeq_db": 45.0}, "event_id": None,
                                 "rules": [{"id": "baseline_relative", "kind": "relative", "metric": "laeq_db", "threshold_db": 57.0}]}},
    })
    s = make_settings(paths={"state_dir": str(replayed)})
    srv = DashboardServer(s, host="127.0.0.1", port=0)
    srv.handler_cls = srv.httpd.RequestHandlerClass
    srv.httpd.RequestHandlerClass.sse_interval = 0.05
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()


def get(srv, path, headers=None, method="GET"):
    c = http.client.HTTPConnection(*srv.address, timeout=5)
    c.request(method, path, headers=headers or {})
    r = c.getresponse()
    body = r.read()
    return r, body


def test_page_and_api(server):
    r, body = get(server, "/")
    assert r.status == 200 and b"Noise collector" in body
    assert "default-src 'self'" in r.getheader("Content-Security-Policy")
    assert b"cdn" not in body.lower()  # self-contained, works without internet
    r, body = get(server, "/api/measurements?seconds=600")
    data = json.loads(body)
    assert r.status == 200 and len(data["points"]) >= 230
    assert any(p.get("laeq_db") for p in data["points"])
    r, body = get(server, "/api/events?seconds=86400")
    ev = json.loads(body)["events"]
    assert len(ev) == 1 and ev[0]["detection_state"] == "finalized" and ev[0]["summary"]["duration_ms"] > 0
    r, body = get(server, "/api/status")
    st = json.loads(body)
    assert st["microphone_state"] == "ok" and st["detection"]["rules"][0]["threshold_db"] == 57.0


def test_long_window_downsampling_keeps_peaks(server):
    r, body = get(server, "/api/measurements?seconds=86400")
    pts = json.loads(body)["points"]
    assert max(p.get("lafmax_db") or 0 for p in pts) > 60  # the burst survives any bucketing


def test_sse_stream_pushes_points_and_status(server):
    latest = json.loads(get(server, "/api/measurements?seconds=600")[1])["latest"]
    c = http.client.HTTPConnection(*server.address, timeout=5)
    c.request("GET", f"/api/stream?since={latest - 5}")
    r = c.getresponse()
    assert r.getheader("Content-Type") == "text/event-stream"
    buf = b""
    while b"event: status" not in buf or b"event: points" not in buf:
        buf += r.fp.read1(65536)
    points = json.loads(buf.split(b"event: points\ndata: ")[1].split(b"\n\n")[0])
    assert [p["t"] for p in points] == list(range(latest - 4, latest + 1))
    c.close()


def test_read_only_and_no_audio(server):
    assert get(server, "/api/measurements", method="POST")[0].status == 405
    assert get(server, "/recordings/x.wav")[0].status == 404
    assert get(server, "/api/recordings")[0].status == 404


def test_lan_binding_requires_token(make_settings, tmp_path):
    s = make_settings(dashboard={"bind": "0.0.0.0", "port": 0})
    with pytest.raises(DashboardError):
        DashboardServer(s)
    tok = tmp_path / "tok"
    tok.write_text("x" * 40)
    s = make_settings(dashboard={"bind": "0.0.0.0", "port": 0, "access_token_file": str(tok)})
    srv = DashboardServer(s)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert get(srv, "/api/status")[0].status == 401
        assert get(srv, "/api/status", {"Authorization": "Bearer " + "x" * 40})[0].status == 200
        r, _ = get(srv, "/?token=" + "x" * 40)
        assert r.status == 303 and "HttpOnly" in r.getheader("Set-Cookie") and r.getheader("Location") == "/"
        assert get(srv, "/api/status", {"Cookie": "nc_dash=" + "x" * 40})[0].status == 200
        assert get(srv, "/api/status", {"Authorization": "Bearer wrong"})[0].status == 401
    finally:
        srv.shutdown()


def test_lan_without_token_only_by_explicit_opt_in(make_settings):
    s = make_settings(dashboard={"bind": "0.0.0.0", "port": 0, "allow_unauthenticated_lan": True})
    srv = DashboardServer(s)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert get(srv, "/api/status")[0].status == 200
        assert get(srv, "/api/measurements", method="POST")[0].status == 405  # still read-only
        assert get(srv, "/recordings/x.wav")[0].status == 404  # still never audio
    finally:
        srv.shutdown()


def test_stop_event_control(make_settings, monkeypatch):
    import json as _json
    import urllib.request

    from noise_collector.health.control import take_stop_event_request

    s = make_settings(dashboard={"port": 0})
    monkeypatch.setenv("NOISE_COLLECTOR_RUN_DIR", str(s.state_dir / "run"))
    eid = "75be624a-d2a5-4fee-989b-70ba93d7f381"
    (s.state_dir / "run").mkdir(parents=True, exist_ok=True)
    (s.state_dir / "run" / "acquisition-status.json").write_text(_json.dumps({"engine": {"detection": {"state": "active", "event_id": eid}}}))

    def post(srv, path, header=True):
        req = urllib.request.Request(f"http://{srv.address[0]}:{srv.address[1]}{path}", method="POST",
                                     headers={"X-Noise-Collector": "stop-event"} if header else {})
        try:
            return urllib.request.urlopen(req, timeout=5).status
        except urllib.error.HTTPError as e:
            return e.code

    off = DashboardServer(s)
    threading.Thread(target=off.serve_forever, daemon=True).start()
    try:
        assert post(off, f"/api/events/{eid}/stop") == 405  # off by default: read-only
    finally:
        off.shutdown()
    s = make_settings(dashboard={"port": 0, "allow_stop_event": True})
    srv = DashboardServer(s)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert _json.loads(get(srv, "/api/status")[1])["controls"] == {"stop_event": True}
        assert post(srv, f"/api/events/{eid}/stop", header=False) == 403  # cross-site forms cannot set it
        assert post(srv, "/api/events/00000000-0000-4000-8000-000000000000/stop") == 409
        assert post(srv, "/api/measurements") == 405  # still the only write
        assert take_stop_event_request(s.state_dir) is None
        assert post(srv, f"/api/events/{eid}/stop") == 202
        assert take_stop_event_request(s.state_dir) == eid
    finally:
        srv.shutdown()


def test_downsampling_is_max_preserving():
    from noise_collector.dashboard.server import _downsample

    pts = [{"t": i, "ok": True, "lafmax_db": 40.0 + (30 if i == 777 else 0)} for i in range(10_000)]
    out = _downsample(pts, 100)
    assert len(out) == 100 and max(p["lafmax_db"] for p in out) == 70.0
