from pathlib import Path

import pytest

from noise_collector.audio.discovery import Ambiguous, NoMatch, list_usb_audio, match, parse_stream_formats
from noise_collector.audio.gain import GainReading, compare, parse_scontents
from noise_collector.config.settings import MicrophoneSelector

STREAM0 = """miniDSP UMIK-1 at usb-0000:01:00.0-1.2, full speed : USB Audio

Capture:
  Status: Stop
  Interface 1
    Altset 1
    Format: S24_3LE
    Channels: 2
    Endpoint: 0x81 (1 IN) (ASYNC)
    Rates: 48000
"""

AMIXER = """Simple mixer control 'Mic',0
  Capabilities: cvolume cvolume-joined cswitch cswitch-joined
  Capture channels: Mono
  Limits: Capture 0 - 32
  Mono: Capture 32 [100%] [0.00dB] [on]
Simple mixer control 'Auto Gain Control',0
  Capabilities: pswitch pswitch-joined
  Playback channels: Mono
  Mono: Playback [off]
"""


def make_card(root: Path, idx: int, usb_path: str, serial: str | None, vid="2752", pid="0007"):
    usbdev = root / "sys/devices/platform/usb1" / usb_path
    iface = usbdev / f"{usb_path}:1.0"
    iface.mkdir(parents=True)
    (usbdev / "idVendor").write_text(vid + "\n")
    (usbdev / "idProduct").write_text(pid + "\n")
    (usbdev / "product").write_text("Umik-1  Gain: 18dB\n")
    if serial:
        (usbdev / "serial").write_text(serial + "\n")
    card = root / "sys/class/sound" / f"card{idx}"
    card.mkdir(parents=True)
    (card / "device").symlink_to(iface)
    (card / "id").write_text("U18dB\n")
    asound = root / "proc/asound" / f"card{idx}"
    asound.mkdir(parents=True)
    (asound / "stream0").write_text(STREAM0)


def test_discovery_matches_serial_then_path_and_refuses_ambiguity(tmp_path):
    make_card(tmp_path, 1, "1-1.2", "7000001")
    make_card(tmp_path, 2, "1-1.3", "7000002")
    make_card(tmp_path, 3, "1-1.4", None, vid="046d", pid="0825")  # webcam
    devs = list_usb_audio(tmp_path / "sys", tmp_path / "proc")
    assert len(devs) == 3
    sel = MicrophoneSelector(usb_vendor_id="2752", usb_product_id="0007", usb_serial="7000002")
    d = match(devs, sel)
    assert d.card_index == 2 and d.usb_path == "1-1.3" and d.alsa_hw == "hw:2,0"
    # path fallback when the serial is not configured
    assert match(devs, MicrophoneSelector(usb_vendor_id="2752", usb_product_id="0007", usb_path="1-1.2")).card_index == 1
    # a different serial on the configured path is refused (replacement microphone)
    with pytest.raises(NoMatch):
        match(devs, MicrophoneSelector(usb_vendor_id="2752", usb_product_id="0007", usb_serial="9999", usb_path="1-1.2"))
    with pytest.raises(NoMatch):
        match(devs, MicrophoneSelector(usb_vendor_id="2752", usb_product_id="0007"))  # selector incomplete


def test_two_identical_serialless_devices_on_ambiguous_selector(tmp_path):
    make_card(tmp_path, 1, "1-1.2", None)
    devs = list_usb_audio(tmp_path / "sys", tmp_path / "proc")
    devs = devs + devs
    with pytest.raises(Ambiguous):
        match(devs, MicrophoneSelector(usb_vendor_id="2752", usb_product_id="0007", usb_path="1-1.2"))


def test_stream_format_parse():
    assert parse_stream_formats(STREAM0) == [{"format": "S24_3LE", "channels": 2, "rates": [48000]}]


def test_amixer_parse_and_compare():
    controls, autos = parse_scontents(AMIXER)
    assert controls == {"Mic,0": "capture=32;db=0.00;switch=on"}
    assert "Auto Gain Control,0" in autos
    ok, _ = compare({"Mic,0": "capture=32;db=0.00;switch=on"}, GainReading(True, controls))
    assert ok
    ok, note = compare({"Mic,0": "capture=20;db=-6.00;switch=on"}, GainReading(True, controls))
    assert not ok and "expected" in note
    ok, note = compare({"Mic,0": "x"}, GainReading(False, error="amixer not installed"))
    assert not ok and "not inspectable" in note
