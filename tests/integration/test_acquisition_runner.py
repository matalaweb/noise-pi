"""The live acquisition process loop with a simulated PortAudio device (no hardware).

The fake stream calls the real ``AlsaCapture._callback`` from a producer thread with packed
24-bit frames, ADC/current stream times and overflow flags, exercising the real capture
buffer, decode path, watchdog, reconnect logic, engine and durability thread.
"""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from noise_collector.acquisition import runner as runner_mod
from noise_collector.audio import alsa_source
from noise_collector.audio.discovery import UsbAudioDevice
from noise_collector.audio.gain import GainReading
from noise_collector.audio.pcm import encode_le
from noise_collector.contract.examples import configuration_result
from noise_collector.store.db import connect, migrate, transaction
from noise_collector.synth import Burst, Scenario, to_pcm

FS = 48000
DEV = UsbAudioDevice(1, "U18dB", "U18dB", "1-1.2", "2752", "0007", "7000001", "miniDSP", "Umik-1", None)


class FakeStream:
    """Delivers audio through the real callback at ``speed`` x real time, with ADC timestamps."""

    def __init__(self, capture, samples, speed=20.0, block=480, stop_after=None):
        self.capture, self.samples, self.speed, self.block = capture, samples, speed, block
        self.active = True
        self.latency = 0.05
        self.stop_after = stop_after
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self):
        n = 0
        t0 = 5000.0
        while self.active and n + self.block <= len(self.samples):
            if self.stop_after is not None and n >= self.stop_after:
                return  # device unplugged: callbacks simply stop
            raw = encode_le(self.samples[n : n + self.block], 24)
            ti = SimpleNamespace(inputBufferAdcTime=t0 + n / FS, currentTime=t0 + (n + self.block) / FS)
            self.capture._callback(raw, self.block, ti, SimpleNamespace(input_overflow=False))
            n += self.block
            time.sleep(self.block / FS / self.speed)
        self.active = False

    def abort(self, ignore_errors=True):
        self.active = False

    def close(self, ignore_errors=True):
        self.active = False


@pytest.fixture
def runner_env(make_settings, monkeypatch):
    s = make_settings(timing={"require_clock_sync": False}, capture={"watchdog_s": 1.0, "reconnect_delays_s": [0.2]})
    migrate(s.db_path)
    res = configuration_result(1, mode="uncalibrated", detection={"baseline_relative": {"baseline_window_seconds": 60}})
    res = {k: v for k, v in res.items() if k not in ("request_id", "server_received_at")}
    conn = connect(s.db_path)
    with transaction(conn):
        conn.execute("INSERT INTO configurations(revision, sha256, document_json, state, received_at) VALUES (1,?,?, 'staged', 'now')",
                     (res["sha256"], json.dumps(res)))
    sc = Scenario(duration_s=60, background_dbfs=-60, bursts=[Burst(25, 8, "engine_like", -30)], seed=3)
    samples = to_pcm(sc.render())
    plugs = {"count": 0}
    streams = []

    monkeypatch.setattr(runner_mod, "list_usb_audio", lambda: [DEV])
    monkeypatch.setattr(runner_mod, "portaudio_index", lambda d: 3)
    monkeypatch.setattr(runner_mod, "read_gain", lambda idx: GainReading(False, error="no mixer (simulated)"))
    monkeypatch.setattr(alsa_source.AlsaCapture, "check", lambda self: None)

    def start(self):
        plugs["count"] += 1
        stop_after = 20 * FS if plugs["count"] == 1 else None  # first connection: unplug after 20 s
        offset = 0 if plugs["count"] == 1 else 30 * FS
        self.stream = FakeStream(self, samples[offset:], stop_after=stop_after)
        streams.append(self.stream)
        self.last_callback_mono = time.monotonic()

    monkeypatch.setattr(alsa_source.AlsaCapture, "start", start)
    return s, conn, streams, plugs


def test_runner_captures_reconnects_and_persists(runner_env):
    s, conn, streams, plugs = runner_env
    r = runner_mod.AcquisitionRunner(s)
    th = threading.Thread(target=r.run, daemon=True)
    th.start()
    deadline = time.time() + 30
    while time.time() < deadline and not (plugs["count"] >= 2 and streams and not streams[-1].active):
        time.sleep(0.1)
    time.sleep(1.5)
    r.request_stop()
    th.join(30)
    assert not th.is_alive()
    sessions = conn.execute("SELECT * FROM acquisition_sessions ORDER BY rowid").fetchall()
    assert len(sessions) >= 2 and sessions[0]["end_reason"] == "device_lost"
    assert json.loads(sessions[0]["microphone_json"])["serial"] == "7000001"
    gaps = conn.execute("SELECT cause FROM gaps").fetchall()
    assert gaps and gaps[0]["cause"] == "device_lost"
    n = conn.execute("SELECT COUNT(*) FROM measurements WHERE status='complete'").fetchone()[0]
    assert n >= 40
    assert conn.execute("SELECT state FROM configurations WHERE revision=1").fetchone()[0] == "applied"
    assert conn.execute("SELECT status FROM config_acknowledgments").fetchone()[0] == "applied"
    status = json.loads((s.state_dir / "run" / "acquisition-status.json").read_text())
    assert status["microphone_state"] == "stopped"
    # sequences restart per session and never repeat inside one
    dup = conn.execute("SELECT session_id, sequence, COUNT(*) c FROM measurements WHERE sequence IS NOT NULL GROUP BY 1,2 HAVING c>1").fetchall()
    assert dup == []
    # timestamps came from the ADC path, not the fallback
    flags = [json.loads(x[0])["quality_flags"] for x in conn.execute("SELECT wire_json FROM measurements WHERE status='complete'")]
    assert not any("timestamp_fallback" in f for f in flags)
    assert np.isfinite(status["uptime_s"])
