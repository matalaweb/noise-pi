"""Exclusive per-state-directory process locks (flock; released automatically on process death)."""

from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path


class LockHeld(RuntimeError):
    pass


class InstanceLock:
    def __init__(self, state_dir: Path, role: str) -> None:
        self.path = Path(state_dir) / f"{role}.lock"
        self.role = role
        self.fd: int | None = None

    def acquire(self) -> "InstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o640)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = ""
            try:
                holder = os.pread(fd, 4096, 0).decode(errors="replace").strip()
            finally:
                os.close(fd)
            raise LockHeld(f"another {self.role} process holds {self.path}: {holder}") from None
        os.ftruncate(fd, 0)
        os.pwrite(fd, json.dumps({"pid": os.getpid(), "since": time.time()}).encode(), 0)
        self.fd = fd
        return self

    def release(self) -> None:
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None

    def __enter__(self) -> "InstanceLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()
