"""Stable USB microphone discovery.

ALSA card numbers and PortAudio indices can change across reboots and replugs, so the device is
resolved from USB identity: vendor:product plus serial when the device reports one, with the
physical USB port path (e.g. ``1-1.2``) as a configured fallback. Exactly one device must match;
the system default, webcams, and second similar microphones are never chosen implicitly.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path

from ..config.settings import MicrophoneSelector


class DiscoveryError(RuntimeError):
    pass


class NoMatch(DiscoveryError):
    pass


class Ambiguous(DiscoveryError):
    pass


@dataclass(frozen=True)
class UsbAudioDevice:
    card_index: int
    card_id: str
    name: str
    usb_path: str | None
    vendor_id: str | None
    product_id: str | None
    serial: str | None
    manufacturer: str | None
    product: str | None
    stream_info: str | None

    @property
    def alsa_hw(self) -> str:
        return f"hw:{self.card_index},0"

    def identity(self) -> dict:
        d = asdict(self)
        d.pop("stream_info")
        return d


def _read(p: Path) -> str | None:
    try:
        return p.read_text().strip() or None
    except (FileNotFoundError, PermissionError, NotADirectoryError):
        return None


def list_usb_audio(sysfs: Path = Path("/sys"), procfs: Path = Path("/proc")) -> list[UsbAudioDevice]:
    out: list[UsbAudioDevice] = []
    sound = sysfs / "class" / "sound"
    if not sound.exists():
        return out
    for card in sorted(sound.glob("card*")):
        m = re.fullmatch(r"card(\d+)", card.name)
        if not m:
            continue
        idx = int(m.group(1))
        try:
            iface = (card / "device").resolve()
        except OSError:
            continue
        usbdev = iface.parent
        vendor = _read(usbdev / "idVendor")
        if vendor is None:
            continue  # not a USB audio device
        asound = procfs / "asound" / f"card{idx}"
        out.append(
            UsbAudioDevice(
                card_index=idx,
                card_id=_read(asound / "id") or _read(card / "id") or f"card{idx}",
                name=_read(card / "id") or "",
                usb_path=usbdev.name,
                vendor_id=vendor,
                product_id=_read(usbdev / "idProduct"),
                serial=_read(usbdev / "serial"),
                manufacturer=_read(usbdev / "manufacturer"),
                product=_read(usbdev / "product"),
                stream_info=_read(asound / "stream0"),
            )
        )
    return out


def match(devices: list[UsbAudioDevice], sel: MicrophoneSelector) -> UsbAudioDevice:
    """Resolve exactly one device: USB serial (when real), else physical port path, else (model
    preset only) the single connected device of that model. Never a default or a similar device."""
    from . import umik1

    if not sel.is_configured():
        raise NoMatch("microphone selector requires usb_vendor_id + usb_product_id (or model) and usb_serial, usb_path or model")
    vid, pid = sel.ids()
    cands = [d for d in devices if (d.vendor_id or "").lower() == vid.lower() and (d.product_id or "").lower() == pid.lower()]
    if sel.model == "umik-1":
        cands = [d for d in cands if umik1.is_umik1(d.vendor_id, d.product_id, d.product)]
    wanted_serial = umik1.real_serial(sel.usb_serial)
    chosen: list[UsbAudioDevice] = []
    if wanted_serial:
        chosen = [d for d in cands if umik1.real_serial(d.serial) == wanted_serial]
    if not chosen and sel.usb_path:
        by_path = [d for d in cands if d.usb_path == sel.usb_path]
        # A device on the fallback path that reports a *real* different serial is someone else's.
        chosen = [d for d in by_path if not (wanted_serial and umik1.real_serial(d.serial) and umik1.real_serial(d.serial) != wanted_serial)]
    elif not chosen and not wanted_serial and not sel.usb_path and sel.model:
        chosen = cands
        if len(chosen) > 1:
            raise Ambiguous(f"{len(chosen)} {sel.model} microphones connected and no usb_path configured; set [microphone] usb_path "
                            f"(paths: {[d.usb_path for d in chosen]})")
    if not chosen:
        raise NoMatch(f"no USB audio device matches {sel.model_dump()} (found {[d.identity() for d in devices]})")
    if len(chosen) > 1:
        raise Ambiguous(f"{len(chosen)} devices match the selector; refine usb_serial/usb_path")
    return chosen[0]


def parse_stream_formats(stream_info: str | None) -> list[dict]:
    """Parse ``/proc/asound/cardN/stream0`` capture alt-settings into format/channels/rates."""
    if not stream_info:
        return []
    sections = stream_info.split("Capture:", 1)
    if len(sections) < 2:
        return []
    out: list[dict] = []
    cur: dict = {}
    for line in sections[1].splitlines():
        line = line.strip()
        if line.startswith("Playback:"):
            break
        if line.startswith("Interface") and cur:
            out.append(cur)
            cur = {}
        elif line.startswith("Format:"):
            cur["format"] = line.split(":", 1)[1].strip()
        elif line.startswith("Channels:"):
            cur["channels"] = int(line.split(":", 1)[1].strip())
        elif line.startswith("Rates:"):
            cur["rates"] = [int(r) for r in re.findall(r"\d+", line.split(":", 1)[1])]
    if cur:
        out.append(cur)
    return out


def portaudio_index(dev: UsbAudioDevice) -> int:
    """Find the PortAudio ALSA input device for the matched card (refresh PortAudio first)."""
    import sounddevice as sd

    sd._terminate()
    sd._initialize()
    hostapis = sd.query_hostapis()
    hits = []
    for i, d in enumerate(sd.query_devices()):
        api = hostapis[d["hostapi"]]["name"]
        if api != "ALSA" or d["max_input_channels"] < 1:
            continue
        if f"(hw:{dev.card_index},0)" in d["name"] or f"hw:CARD={dev.card_id},DEV=0" in d["name"]:
            hits.append(i)
    if len(hits) != 1:
        raise DiscoveryError(f"expected one PortAudio ALSA device for {dev.alsa_hw}, found {hits}")
    return hits[0]
