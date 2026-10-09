"""Supervise the acquisition and delivery processes together (``noise-collector run``).

Each child restarts independently with backoff, so a crashed uploader never restarts capture
and a capture restart never restarts a healthy uploader. The systemd watchdog is fed only while
acquisition reports *progressing* work (fresh status file and, when the microphone is connected,
recent durable commits); a hung acquisition child is killed and restarted.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config.settings import Settings
from .health.status import read_status, run_dir, write_status

log = logging.getLogger(__name__)

BACKOFF = [1, 2, 5, 10, 30, 60]
STATUS_STALE_S = 15.0
DURABLE_STALE_S = 20.0
STOP_TIMEOUT_S = 30.0


def sd_notify(msg: str) -> None:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(msg.encode())
    except OSError:
        pass


@dataclass
class Child:
    role: str
    argv: list[str]
    proc: subprocess.Popen | None = None
    restarts: int = 0
    next_start: float = 0.0
    started_at: float = 0.0
    history: list[float] = field(default_factory=list)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


class Supervisor:
    def __init__(self, settings: Settings, config_path: Path | None) -> None:
        self.s = settings
        base = [sys.executable, "-m", "noise_collector.cli"]
        cfg = ["--config", str(config_path)] if config_path else []
        self.children = [Child("acquisition", base + cfg + ["acquire"]), Child("delivery", base + cfg + ["deliver"])]
        if settings.dashboard.enabled:
            # Independent, read-only child: its failures never affect capture or delivery.
            self.children.append(Child("dashboard", base + cfg + ["dashboard"]))
        self.stopping = False
        self.status_path = run_dir(settings.state_dir) / "supervisor-status.json"

    def _start(self, c: Child) -> None:
        c.proc = subprocess.Popen(c.argv, stdin=subprocess.DEVNULL)
        c.started_at = time.monotonic()
        log.info("started %s pid=%s", c.role, c.proc.pid)

    def _stop(self, c: Child, timeout: float = STOP_TIMEOUT_S) -> None:
        if not c.alive():
            return
        assert c.proc is not None
        c.proc.send_signal(signal.SIGTERM)
        try:
            c.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            log.error("%s did not stop within %ss; killing", c.role, timeout)
            c.proc.kill()
            c.proc.wait(5)

    def acquisition_progressing(self) -> tuple[bool, str]:
        st = read_status(run_dir(self.s.state_dir) / "acquisition-status.json")
        if st is None:
            return False, "no acquisition status"
        if st["age_s"] > STATUS_STALE_S:
            return False, f"acquisition status stale ({st['age_s']:.0f}s)"
        if st.get("microphone_state") == "ok":
            age = st.get("last_durable_commit_age_s")
            if age is not None and age > DURABLE_STALE_S and st.get("durable_capture") != "critical":
                return False, f"no durable measurement for {age:.0f}s while microphone ok"
        return True, "ok"

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        for c in self.children:
            self._start(c)
        sd_notify("READY=1")
        grace_until = time.monotonic() + 60
        while not self.stopping:
            now = time.monotonic()
            for c in self.children:
                if not c.alive() and now >= c.next_start:
                    if c.proc is not None:
                        code = c.proc.returncode
                        c.history = [t for t in c.history if now - t < 600] + [now]
                        delay = BACKOFF[min(len(c.history) - 1, len(BACKOFF) - 1)]
                        log.error("%s exited with %s; restarting in %ss", c.role, code, delay)
                        c.next_start = now + delay
                        c.proc = None
                        continue
                    self._start(c)
                    if c.role == "acquisition":
                        grace_until = now + 60
            healthy, why = self.acquisition_progressing()
            acq = self.children[0]
            if not healthy and now > grace_until and acq.alive():
                log.error("acquisition not progressing (%s); restarting it", why)
                self._stop(acq, timeout=STOP_TIMEOUT_S)
                acq.proc = None
                acq.next_start = now + 2
                grace_until = now + 60
            if healthy or now <= grace_until:
                sd_notify("WATCHDOG=1")
            write_status(self.status_path, {"component": "supervisor", "acquisition_healthy": healthy, "reason": why,
                                            "children": {c.role: {"pid": c.proc.pid if c.proc else None,
                                                                  "recent_restarts": len(c.history)} for c in self.children}})
            time.sleep(1.0)
        sd_notify("STOPPING=1")
        for c in self.children:  # signal both first so they shut down in parallel
            if c.alive():
                c.proc.send_signal(signal.SIGTERM)  # type: ignore[union-attr]
        deadline = time.monotonic() + STOP_TIMEOUT_S
        for c in self.children:
            self._stop(c, timeout=max(1.0, deadline - time.monotonic()))
        return 0

    def _on_signal(self, *_a) -> None:
        self.stopping = True
