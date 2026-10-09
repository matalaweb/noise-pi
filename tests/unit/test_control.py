"""Owner control requests (local dashboard -> acquisition)."""

import time

from noise_collector.health.control import (
    control_dir,
    request_stop_event,
    take_stop_event_request,
)


def test_stop_request_is_taken_once(tmp_path, monkeypatch):
    monkeypatch.setenv("NOISE_COLLECTOR_RUN_DIR", str(tmp_path / "run"))
    eid = "75be624a-d2a5-4fee-989b-70ba93d7f381"
    request_stop_event(tmp_path, eid)
    assert take_stop_event_request(tmp_path) == eid
    assert take_stop_event_request(tmp_path) is None


def test_stale_or_garbled_requests_are_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("NOISE_COLLECTOR_RUN_DIR", str(tmp_path / "run"))
    request_stop_event(tmp_path, "x")
    assert take_stop_event_request(tmp_path, now=time.time() + 120) is None
    (control_dir(tmp_path) / "stop-event.json").write_text("{not json")
    assert take_stop_event_request(tmp_path) is None
