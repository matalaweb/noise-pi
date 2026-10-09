"""Read back ALSA mixer capture controls (gain, switches, AGC) for a card.

Representation (used verbatim in profile ``gain.controls``)::

    {"Mic,0": "capture=32;db=0.00;switch=on"}

Only controls with capture capability are recorded. Any control whose name suggests automatic
gain/level processing is reported so ``doctor`` can require it to be off.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field

AUTO_PATTERNS = re.compile(r"auto\s*gain|agc|automatic|noise\s*(suppress|reduc)|echo", re.I)


@dataclass
class GainReading:
    inspectable: bool
    controls: dict[str, str] = field(default_factory=dict)
    auto_controls: dict[str, str] = field(default_factory=dict)
    error: str | None = None


def parse_scontents(text: str) -> tuple[dict[str, str], dict[str, str]]:
    controls: dict[str, str] = {}
    autos: dict[str, str] = {}
    name = None
    caps = ""
    values: list[str] = []

    def rep_of(value_line: str) -> str:
        vol = re.search(r"Capture\s+(-?\d+)", value_line)
        db = re.search(r"\[(-?\d+(?:\.\d+)?)dB\]", value_line)
        sw = re.search(r"\[(on|off)\]", value_line)
        parts = []
        if vol:
            parts.append(f"capture={vol.group(1)}")
        if db:
            parts.append(f"db={float(db.group(1)):.2f}")
        if sw:
            parts.append(f"switch={sw.group(1)}")
        return ";".join(parts) if parts else value_line.strip()

    def flush() -> None:
        if name is None:
            return
        joined = " ".join(values)
        if values and ("cvolume" in caps or "cswitch" in caps or "Capture" in joined):
            # One representation per channel line; identical channels collapse to one value,
            # differing channels are kept as "left|right" so a mismatch is visible.
            reps = list(dict.fromkeys(rep_of(v) for v in values))
            controls[name] = "|".join(reps)
        if AUTO_PATTERNS.search(name):
            autos[name] = joined.strip()

    for raw in text.splitlines():
        m = re.match(r"Simple mixer control '(.+)',(\d+)", raw)
        if m:
            flush()
            name, caps, values = f"{m.group(1)},{m.group(2)}", "", []
            continue
        line = raw.strip()
        if line.startswith("Capabilities:"):
            caps = line
        elif line.startswith(("Capture channels:", "Playback channels:", "Limits:")):
            continue
        elif re.match(r"^(Mono|Front Left|Front Right|Rear Left|Rear Right|Front Center|Capture)\b[^:]*:", line) and "[" in line:
            values.append(line.split(":", 1)[1].strip())
    flush()
    return controls, autos


def read_gain(card_index: int, timeout: float = 5.0) -> GainReading:
    exe = shutil.which("amixer")
    if exe is None:
        return GainReading(inspectable=False, error="amixer not installed (alsa-utils)")
    try:
        out = subprocess.run([exe, "-c", str(card_index), "scontents"], capture_output=True, text=True, timeout=timeout, check=True)
    except (subprocess.SubprocessError, OSError) as exc:
        return GainReading(inspectable=False, error=f"amixer failed: {exc}"[:200])
    controls, autos = parse_scontents(out.stdout)
    if not controls:
        return GainReading(inspectable=False, auto_controls=autos, error="device exposes no capture mixer controls")
    return GainReading(inspectable=True, controls=controls, auto_controls=autos)


def compare(expected: dict[str, str], reading: GainReading) -> tuple[bool, str | None]:
    """True if every expected control reads back exactly as provisioned."""
    if not expected:
        return True, None
    if not reading.inspectable:
        return False, f"gain not inspectable: {reading.error}"
    diffs = [f"{k}: expected {v!r}, read {reading.controls.get(k)!r}" for k, v in expected.items() if reading.controls.get(k) != v]
    return (not diffs), ("; ".join(diffs) if diffs else None)
