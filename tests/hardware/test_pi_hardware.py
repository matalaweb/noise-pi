"""Hardware checks on the Pi with the real microphone (skipped elsewhere).

    NOISE_COLLECTOR_CONFIG=/etc/noise-collector/collector.toml pytest -m hardware tests/hardware

Stop the service first (the microphone is exclusive). These are smoke checks. The acceptance
procedures (72 h soak, outage, unplug/replug, calibration, timing) are in
docs/hardware-validation.md and need a person and reference equipment.
"""

from __future__ import annotations

import os
import sys
import time

import numpy as np
import pytest

pytestmark = [pytest.mark.hardware,
              pytest.mark.skipif(not (sys.platform.startswith("linux") and os.environ.get("NOISE_COLLECTOR_CONFIG")),
                                 reason="requires the Pi, the microphone and NOISE_COLLECTOR_CONFIG")]


@pytest.fixture(scope="module")
def setup():
    from noise_collector.audio.discovery import list_usb_audio, match, portaudio_index
    from noise_collector.config.settings import load_settings

    s = load_settings()
    dev = match(list_usb_audio(), s.microphone)
    return s, dev, portaudio_index(dev)


def test_device_identity_and_native_format(setup):
    from noise_collector.audio.discovery import parse_stream_formats

    _, dev, _ = setup
    fmts = parse_stream_formats(dev.stream_info)
    assert any(48000 in f.get("rates", []) for f in fmts), fmts


def test_adc_timestamps_and_sustained_capture(setup):
    from noise_collector.audio.alsa_source import AlsaCapture
    from noise_collector.audio.pcm import PcmFormat

    _, dev, idx = setup
    s, _, _ = setup
    cap = AlsaCapture(idx, PcmFormat("int24", 24, s.capture.channels, 48000, s.capture.analysis_channel))
    cap.check()
    cap.start()
    got, sources, t0 = 0, set(), time.monotonic()
    try:
        while time.monotonic() - t0 < 30:
            for b in cap.blocks():
                got += len(b.samples)
                sources.add(b.ts_source)
            time.sleep(0.05)
    finally:
        cap.stop()
    elapsed = time.monotonic() - t0
    assert abs(got / elapsed - 48000) / 48000 < 0.01
    assert cap.buffer.overflow_frames == 0 and cap.buffer.driver_overflows == 0
    print(f"timestamp sources: {sources}; callback offset spread: {cap.offset_spread_ms()} ms; "
          f"channel mismatch blocks {cap.channel_mismatch_blocks}/{cap.channel_blocks} (max diff {cap.channel_max_diff})")


def test_capture_is_not_digital_silence(setup):
    from noise_collector.audio.alsa_source import AlsaCapture
    from noise_collector.audio.pcm import PcmFormat

    s, _, idx = setup
    cap = AlsaCapture(idx, PcmFormat("int24", 24, s.capture.channels, 48000, s.capture.analysis_channel))
    cap.start()
    xs = []
    try:
        time.sleep(3)
        xs = [b.samples for b in cap.blocks()]
    finally:
        cap.stop()
    x = np.concatenate(xs)
    assert x.min() != x.max(), "constant input: check gain, mute switch and the device"


def test_umik1_identity_gain_and_format(setup):
    from noise_collector.audio import umik1
    from noise_collector.audio.discovery import parse_stream_formats
    from noise_collector.audio.gain import read_gain

    s, dev, _ = setup
    if not umik1.is_umik1(dev.vendor_id, dev.product_id, dev.product):
        pytest.skip("not a UMIK-1")
    print(f"product {dev.product!r} usb serial {dev.serial!r} path {dev.usb_path}")
    fmts = parse_stream_formats(dev.stream_info)
    assert any(f["format"] == "S24_3LE" and 48000 in f["rates"] and f["channels"] == s.capture.channels for f in fmts), fmts
    ok, note = umik1.mixer_check(read_gain(dev.card_index))
    assert ok, note
