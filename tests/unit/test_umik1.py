"""miniDSP UMIK-1 support: identity, format, gain rules, calibration file, sensitivity estimate.

Device strings, descriptors and mixer layouts are taken from published UMIK-1 captures (see
docs/umik1.md); hardware confirmation on the purchased unit is gate H1 in docs/hardware-validation.md.
"""

from pathlib import Path

import numpy as np
import pytest

from noise_collector.audio import umik1
from noise_collector.audio.discovery import Ambiguous, NoMatch, list_usb_audio, match, parse_stream_formats
from noise_collector.audio.gain import GainReading, parse_scontents
from noise_collector.config.settings import MicrophoneSelector
from noise_collector.dsp.calibration import parse_calibration_file

STREAM_STEREO = """miniDSP Umik-1  Gain: 18dB at usb-0000:01:00.0-1.2.1, full speed : USB Audio

Capture:
  Status: Stop
  Interface 1
    Altset 1
    Format: S24_3LE
    Channels: 2
    Endpoint: 0x81 (1 IN) (SYNC)
    Rates: 48000
    Bits: 24
"""
STREAM_MONO = STREAM_STEREO.replace("Channels: 2", "Channels: 1")

AMIXER_STEREO = """Simple mixer control 'Mic',0
  Capabilities: cvolume cswitch cswitch-joined
  Capture channels: Front Left - Front Right
  Limits: Capture 0 - 127
  Front Left: Capture 127 [100%] [0.00dB] [on]
  Front Right: Capture 127 [100%] [0.00dB] [on]
"""
AMIXER_MONO_LOW = """Simple mixer control 'Mic',0
  Capabilities: cvolume cvolume-joined cswitch cswitch-joined
  Capture channels: Mono
  Limits: Capture 0 - 127
  Mono: Capture 100 [79%] [-13.50dB] [on]
"""
CAL_90 = b'"Sens Factor =-0.837dB, AGain =18dB, SERNO: 7103946"\r\n10.054\t-1.70\r\n20.0\t-0.8\r\n1000.0\t0.0\r\n20000.0\t-2.1\r\n'
CAL_OLD = b'"Sens Factor =-4.5312dB, SERNO: 7000581"\n10.0\t-3.1\n1000.0\t0.0\n'


def make_umik(root: Path, idx: int, usb_path: str, gain: int = 18, serial: str = "000-0000", stream: str = STREAM_STEREO):
    usbdev = root / "sys/devices/platform/usb1" / usb_path
    iface = usbdev / f"{usb_path}:1.0"
    iface.mkdir(parents=True)
    (usbdev / "idVendor").write_text("2752\n")
    (usbdev / "idProduct").write_text("0007\n")
    (usbdev / "manufacturer").write_text("miniDSP\n")
    (usbdev / "product").write_text(f"Umik-1  Gain: {gain}dB\n")
    (usbdev / "serial").write_text(serial + "\n")
    card = root / "sys/class/sound" / f"card{idx}"
    card.mkdir(parents=True)
    (card / "device").symlink_to(iface)
    (card / "id").write_text(f"U{gain}dB\n")
    asound = root / "proc/asound" / f"card{idx}"
    asound.mkdir(parents=True)
    (asound / "id").write_text(f"U{gain}dB\n")
    (asound / "stream0").write_text(stream)


def test_identity_strings():
    assert umik1.is_umik1("2752", "0007", "Umik-1  Gain: 18dB")
    assert not umik1.is_umik1("2752", "0016", "USBStreamer")  # same vendor, other product
    assert umik1.analog_gain_db("Umik-1  Gain: 18dB") == 18
    assert umik1.analog_gain_db("Umik-1  Gain:  0dB") == 0
    assert umik1.real_serial("000-0000") is None and umik1.real_serial("1") is None
    assert umik1.real_serial("7103946") == "7103946"


def test_discovery_umik_by_model_and_path(tmp_path):
    make_umik(tmp_path, 3, "1-1.2")
    devs = list_usb_audio(tmp_path / "sys", tmp_path / "proc")
    d = match(devs, MicrophoneSelector(model="umik-1"))
    assert d.card_index == 3 and d.card_id == "U18dB"
    assert parse_stream_formats(d.stream_info) == [{"format": "S24_3LE", "channels": 2, "rates": [48000]}]
    # the placeholder serial never identifies a microphone
    with pytest.raises(NoMatch):
        match(devs, MicrophoneSelector(model="umik-1", usb_serial="7103946"))
    assert match(devs, MicrophoneSelector(model="umik-1", usb_serial="000-0000")).card_index == 3


def test_two_umiks_need_usb_path(tmp_path):
    make_umik(tmp_path, 1, "1-1.2")
    make_umik(tmp_path, 2, "1-1.3", gain=12)
    devs = list_usb_audio(tmp_path / "sys", tmp_path / "proc")
    with pytest.raises(Ambiguous, match="usb_path"):
        match(devs, MicrophoneSelector(model="umik-1"))
    assert match(devs, MicrophoneSelector(model="umik-1", usb_path="1-1.3")).card_id == "U12dB"


def test_mixer_rules():
    controls, _ = parse_scontents(AMIXER_STEREO)
    assert controls == {"Mic,0": "capture=127;db=0.00;switch=on"}
    assert umik1.mixer_check(GainReading(True, controls)) == (True, None)
    low, _ = parse_scontents(AMIXER_MONO_LOW)
    ok, note = umik1.mixer_check(GainReading(True, low))
    assert not ok and "sset Mic 100%" in note
    muted, _ = parse_scontents(AMIXER_STEREO.replace("[0.00dB] [on]", "[0.00dB] [off]"))
    assert not umik1.mixer_check(GainReading(True, muted))[0]
    assert not umik1.mixer_check(GainReading(False, error="amixer missing"))[0]


def test_analog_gain_must_match_calibration_file():
    controls, _ = parse_scontents(AMIXER_STEREO)
    assert umik1.gain_check(GainReading(True, controls), "Umik-1  Gain: 18dB", 18.0)[0]
    ok, note = umik1.gain_check(GainReading(True, controls), "Umik-1  Gain: 12dB", 18.0)
    assert not ok and "AGain 18" in note
    assert umik1.gain_check(GainReading(True, controls), "Umik-1  Gain: 12dB", None)[0]  # older files lack AGain


def test_calibration_files_parse_and_orientation():
    new = umik1.cal_header(parse_calibration_file(CAL_90).header)
    assert new == umik1.CalHeader(-0.837, 18.0, "7103946")
    old = umik1.cal_header(parse_calibration_file(CAL_OLD).header)
    assert old == umik1.CalHeader(-4.5312, None, "7000581")
    assert umik1.is_ninety_degree_file("7103946_90deg.txt")
    assert not umik1.is_ninety_degree_file("7090123.txt")  # a serial containing "90" is not a 90-degree file
    cal = parse_calibration_file(CAL_90)
    assert cal.freqs_hz[0] == pytest.approx(10.054) and cal.phase_deg is None


def test_sensitivity_estimate_rew_convention():
    # REW shows "Full scale SPL = 124.84 dB" for Sens Factor -0.837 (docs/umik1.md).
    est = umik1.sensitivity_estimate(-0.837)
    assert est["full_scale_spl_db"] == pytest.approx(124.837)
    assert est["sensitivity_dbfs_at_94db"] == pytest.approx(-30.837)
    assert est["calibration_state"] == "estimated"
    # a 94 dB tone at that reading maps back to 94 dB SPL through the collector's scale formula
    from noise_collector.dsp.calibration import P0, scale_from_sensitivity

    scale = scale_from_sensitivity(est["sensitivity_dbfs_at_94db"], 94.0)
    rms = 10 ** (est["sensitivity_dbfs_at_94db"] / 20)
    assert 20 * np.log10(rms * scale / P0) == pytest.approx(94.0)
    # the digital mixer gain shifts the reading 1:1
    assert umik1.sensitivity_estimate(-0.837, -13.5)["sensitivity_dbfs_at_94db"] == pytest.approx(-44.337)


def test_umik_report(tmp_path, monkeypatch):
    from noise_collector import umik_report

    make_umik(tmp_path, 4, "1-1.2.1")
    monkeypatch.setattr(umik_report, "list_usb_audio", lambda: list_usb_audio(tmp_path / "sys", tmp_path / "proc"))
    monkeypatch.setattr(umik_report, "read_gain", lambda idx: GainReading(True, parse_scontents(AMIXER_STEREO)[0]))
    cal = tmp_path / "7103946_90deg.txt"
    cal.write_bytes(CAL_90)
    rep = umik_report.report(str(cal))
    dev = rep["devices"][0]
    assert dev["analog_gain_db"] == 18 and dev["usb_serial_is_placeholder"] and dev["mixer_ok"]
    assert dev["suggested_settings"]["capture"]["channels"] == 2 and dev["suggested_settings"]["microphone"]["usb_path"] == "1-1.2.1"
    assert rep["calibration_file"]["orientation"] == "90deg" and rep["calibration_file"]["serial"] == "7103946"
    toml = rep["collector_toml"]
    assert toml["microphone"] == {"model": "umik-1"}
    assert toml["calibration"] == {"state": "estimated", "frequency_response_file": "/etc/noise-collector/7103946_90deg.txt"}
    assert rep["estimate"]["sensitivity_dbfs_at_94db"] == pytest.approx(-30.837)


# Captured from a newer-revision unit (bcdDevice 1.23) on a Raspberry Pi 4B, Debian 13, 2026-10-09.
STREAM_NEW_REV = """miniDSP Ltd. UMIK-1 at usb-0000:01:00.0-1.2, full speed : USB Audio

Capture:
  Status: Stop
  Interface 1
    Altset 1
    Format: S24_3LE
    Channels: 1
    Endpoint: 0x86 (6 IN) (ASYNC)
    Rates: 48000
    Bits: 24
    Channel map: MONO
"""
AMIXER_NEW_REV = """Simple mixer control 'Mic',0
  Capabilities: cvolume cvolume-joined cswitch cswitch-joined
  Capture channels: Mono
  Limits: Capture 0 - 127
  Mono: Capture 127 [100%] [0.00dB] [on]
"""


def test_newer_revision_without_gain_in_product_string(tmp_path):
    root = tmp_path
    usbdev = root / "sys/devices/platform/usb1/1-1.2"
    (usbdev / "1-1.2:1.0").mkdir(parents=True)
    for name, value in {"idVendor": "2752", "idProduct": "0007", "manufacturer": "miniDSP Ltd.", "product": "UMIK-1", "serial": "1"}.items():
        (usbdev / name).write_text(value + "\n")
    (root / "sys/class/sound/card3").mkdir(parents=True)
    (root / "sys/class/sound/card3/device").symlink_to(usbdev / "1-1.2:1.0")
    (root / "proc/asound/card3").mkdir(parents=True)
    (root / "proc/asound/card3/id").write_text("UMIK1\n")
    (root / "proc/asound/card3/stream0").write_text(STREAM_NEW_REV)
    d = match(list_usb_audio(root / "sys", root / "proc"), MicrophoneSelector(model="umik-1", usb_path="1-1.2"))
    assert d.card_id == "UMIK1" and umik1.is_umik1(d.vendor_id, d.product_id, d.product)
    assert umik1.analog_gain_db(d.product) is None and umik1.real_serial(d.serial) is None
    assert parse_stream_formats(d.stream_info) == [{"format": "S24_3LE", "channels": 1, "rates": [48000]}]
    controls, _ = parse_scontents(AMIXER_NEW_REV)
    ok, note = umik1.gain_check(GainReading(True, controls), d.product, 18.0)
    assert ok and "does not report its analog gain" in note
    assert umik1.gain_check(GainReading(True, controls), d.product, None) == (True, None)
