"""The live acquisition loop with a simulated stereo miniDSP UMIK-1 (no hardware).

Stereo S24_3LE frames go through the real PortAudio callback path, decoding, channel selection,
channel-identity monitoring, UMIK-1 gain verification (mixer + analog gain vs calibration file)
and the durability layer.
"""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

from noise_collector.acquisition import runner as runner_mod
from noise_collector.audio import alsa_source
from noise_collector.audio.discovery import UsbAudioDevice
from noise_collector.audio.gain import GainReading, parse_scontents
from noise_collector.audio.pcm import encode_le
from noise_collector.contract.examples import configuration_result
from noise_collector.store.db import connect, migrate, transaction
from noise_collector.synth import Burst, Scenario, to_pcm

FS = 48000
STEREO = """Capture:
  Interface 1
    Altset 1
    Format: S24_3LE
    Channels: 2
    Endpoint: 0x81 (1 IN) (SYNC)
    Rates: 48000
"""
MIXER_0DB = parse_scontents("""Simple mixer control 'Mic',0
  Capabilities: cvolume cswitch
  Capture channels: Front Left - Front Right
  Front Left: Capture 127 [100%] [0.00dB] [on]
  Front Right: Capture 127 [100%] [0.00dB] [on]
""")[0]
CAL = b'"Sens Factor =-0.837dB, AGain =18dB, SERNO: 7103946"\n10.0\t-1.7\n1000.0\t0.0\n20000.0\t-2.1\n'


def umik(gain: int = 18) -> UsbAudioDevice:
    return UsbAudioDevice(2, f"U{gain}dB", f"U{gain}dB", "1-1.2", "2752", "0007", "000-0000", "miniDSP", f"Umik-1  Gain: {gain}dB", STEREO)


class StereoStream:
    def __init__(self, capture, frames, speed=40.0, block=480):
        self.capture, self.frames, self.speed, self.block = capture, frames, speed, block
        self.active, self.latency = True, 0.05
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        n, t0 = 0, 7000.0
        while self.active and n + self.block <= len(self.frames):
            raw = encode_le(self.frames[n : n + self.block].reshape(-1), 24)
            ti = SimpleNamespace(inputBufferAdcTime=t0 + n / FS, currentTime=t0 + (n + self.block) / FS)
            self.capture._callback(raw, self.block, ti, SimpleNamespace(input_overflow=False))
            n += self.block
            time.sleep(self.block / FS / self.speed)
        self.active = False

    def abort(self, ignore_errors=True):
        self.active = False

    def close(self, ignore_errors=True):
        self.active = False


def setup(make_settings, monkeypatch, device, channels=2, with_file=True):
    s = make_settings(timing={"require_clock_sync": False}, capture={"channels": channels, "watchdog_s": 1.0, "reconnect_delays_s": [0.2]},
                      microphone={"model": "umik-1", "usb_serial": None, "usb_vendor_id": None, "usb_product_id": None,
                                  "microphone_model": None, "gain_reference_check": None},
                      calibration={"state": "estimated", "sensitivity_dbfs_at_94db": None, "reference_method": None})
    if with_file:
        f = s.state_dir.parent / "7103946_90deg.txt"
        f.write_bytes(CAL)
        s.calibration.frequency_response_file = f
    else:
        s.calibration.sensitivity_dbfs_at_94db = -30.837
    migrate(s.db_path)
    res = configuration_result(1, mode="estimated", detection={"baseline_relative": {"baseline_window_seconds": 60}})
    res = {k: v for k, v in res.items() if k not in ("request_id", "server_received_at")}
    conn = connect(s.db_path)
    with transaction(conn):
        conn.execute("INSERT INTO configurations(revision, sha256, document_json, state, received_at) VALUES (1,?,?, 'staged', 'now')",
                     (res["sha256"], json.dumps(res)))
    mono = to_pcm(Scenario(duration_s=120, background_dbfs=-50, bursts=[Burst(20, 6, "engine_like", -25)], seed=5).render())
    frames = mono.reshape(-1, 1).repeat(2, axis=1)  # a UMIK-1 carries the same signal on both channels
    monkeypatch.setattr(runner_mod, "list_usb_audio", lambda: [device])
    monkeypatch.setattr(runner_mod, "portaudio_index", lambda d: 7)
    monkeypatch.setattr(runner_mod, "read_gain", lambda idx: GainReading(True, dict(MIXER_0DB)))
    monkeypatch.setattr(alsa_source.AlsaCapture, "check", lambda self: None)

    def start(self):
        assert self.fmt.channels == channels and self.fmt.container == "int24"
        self.stream = StereoStream(self, frames)
        self.last_callback_mono = time.monotonic()

    monkeypatch.setattr(alsa_source.AlsaCapture, "start", start)
    return s, conn


def run_for(s, seconds, until_rows=0):
    """Run the loop for ``seconds``, or longer (up to 20 s) until ``until_rows`` complete rows exist."""
    r = runner_mod.AcquisitionRunner(s)
    th = threading.Thread(target=r.run, daemon=True)
    th.start()
    time.sleep(seconds)
    conn = connect(s.db_path)
    deadline = time.time() + 20
    while time.time() < deadline and conn.execute("SELECT COUNT(*) FROM measurements WHERE status='complete'").fetchone()[0] < until_rows:
        time.sleep(0.2)
    status = json.loads((s.state_dir / "run" / "acquisition-status.json").read_text())
    r.request_stop()
    th.join(30)
    return status


def test_stereo_umik_captures_channel_zero_with_verified_gain(make_settings, monkeypatch):
    s, conn = setup(make_settings, monkeypatch, umik(18))
    status = run_for(s, 2.5, until_rows=30)
    assert status["microphone_state"] == "ok", status["latest_capture_error"]
    cap = status["capture_buffer"]
    assert cap["channel_blocks"] > 50 and cap["channel_mismatch_blocks"] == 0
    assert status["device"]["umik1"] == {"analog_gain_db": 18, "usb_serial_is_placeholder": True}
    assert status["engine"]["spl_allowed"] is True and status["engine"]["scale_available"] is True
    rows = [json.loads(r[0]) for r in conn.execute("SELECT wire_json FROM measurements WHERE status='complete'")]
    assert len(rows) >= 30 and all(r["laeq_db"] is not None and r["quality_flags"] == [] for r in rows)
    session = conn.execute("SELECT format_json, gain_json FROM acquisition_sessions").fetchone()
    assert json.loads(session["format_json"])["channels"] == 2
    assert json.loads(session["gain_json"])["observed"]["analog_gain_db"] == "18"


def test_umik_gain_setting_must_match_calibration_file(make_settings, monkeypatch):
    s, conn = setup(make_settings, monkeypatch, umik(12))  # the file says AGain 18 dB
    status = run_for(s, 2.0)
    assert status["microphone_state"] == "gain_mismatch"
    rows = [json.loads(r[0]) for r in conn.execute("SELECT wire_json FROM measurements WHERE status='complete'")]
    assert rows and all(r["laeq_db"] is None and r["null_reasons"]["laeq_db"] == "gain_mismatch"
                        and "invalid_calibration" in r["quality_flags"] for r in rows)
    assert all(r["rms_dbfs"] is not None for r in rows)


def test_wrong_channel_count_is_a_format_mismatch_not_a_capture(make_settings, monkeypatch):
    s, conn = setup(make_settings, monkeypatch, umik(18), channels=1)
    status = run_for(s, 1.0)
    assert status["microphone_state"] == "format_mismatch"
    assert "set [capture] channels" in status["latest_capture_error"]
    assert conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == 0
