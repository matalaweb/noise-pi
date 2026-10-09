"""Local status files (no network listener). Written to the runtime dir, typically tmpfs."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


def run_dir(state_dir: Path) -> Path:
    return Path(os.environ.get("NOISE_COLLECTOR_RUN_DIR", state_dir / "run"))


def write_status(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = dict(data, written_at=time.time(), written_mono=time.monotonic(), pid=os.getpid())
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, default=str, indent=1))
    os.replace(tmp, path)


def read_status(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    data["age_s"] = time.time() - data.get("written_at", 0)
    return data


def os_boot_id() -> str | None:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return None


def host_health() -> dict:
    """Temperature, throttling/undervoltage flags (when readable) and load."""
    out: dict = {}
    try:
        out["cpu_temp_c"] = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
    except (OSError, ValueError):
        out["cpu_temp_c"] = None
    try:
        # Raspberry Pi firmware exposes throttling via the hwmon/rpi_volt or vcgencmd; read the
        # sysfs flag when present (requires no extra privileges on recent kernels).
        p = next(Path("/sys/devices/platform/soc").glob("soc:firmware/get_throttled"), None)
        out["throttled_flags"] = p.read_text().strip() if p else None
    except OSError:
        out["throttled_flags"] = None
    try:
        out["loadavg"] = os.getloadavg()
    except OSError:
        out["loadavg"] = None
    return out
