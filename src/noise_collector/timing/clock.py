"""Host clock-synchronisation status.

On Linux the kernel NTP state is read with ``adjtimex(2)`` (no privileges needed for a read-only
call). ``synchronized`` is False when the kernel reports TIME_ERROR or STA_UNSYNC, which is the
same signal ``timedatectl``'s "System clock synchronized" uses. ``est_error_ms``/``max_error_ms``
are the kernel's estimated/maximum error bounds maintained by chrony/timesyncd. Elsewhere the
state is unknown (None), which the timing model treats as untrusted unless explicitly allowed.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import sys
import time
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ClockStatus:
    synchronized: bool | None
    est_error_ms: float | None = None
    max_error_ms: float | None = None
    offset_ms: float | None = None
    source: str = "unknown"


class ClockSource(Protocol):
    def wall_minus_mono(self) -> float: ...

    def status(self) -> ClockStatus: ...


class _Timex(ctypes.Structure):
    _fields_ = [
        ("modes", ctypes.c_uint),
        ("offset", ctypes.c_long),
        ("freq", ctypes.c_long),
        ("maxerror", ctypes.c_long),
        ("esterror", ctypes.c_long),
        ("status", ctypes.c_int),
        ("constant", ctypes.c_long),
        ("precision", ctypes.c_long),
        ("tolerance", ctypes.c_long),
        ("time_sec", ctypes.c_long),
        ("time_usec", ctypes.c_long),
        ("tick", ctypes.c_long),
        ("ppsfreq", ctypes.c_long),
        ("jitter", ctypes.c_long),
        ("shift", ctypes.c_int),
        ("stabil", ctypes.c_long),
        ("jitcnt", ctypes.c_long),
        ("calcnt", ctypes.c_long),
        ("errcnt", ctypes.c_long),
        ("stbcnt", ctypes.c_long),
        ("tai", ctypes.c_int),
        ("_pad", ctypes.c_int * 11),
    ]


_TIME_ERROR = 5
_STA_UNSYNC = 0x0040
_STA_NANO = 0x2000


class SystemClock:
    """Real host clocks: ``time.time`` vs ``time.monotonic`` (CLOCK_MONOTONIC on Linux)."""

    def __init__(self) -> None:
        self._adjtimex = None
        if sys.platform.startswith("linux"):
            libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
            fn = getattr(libc, "adjtimex", None)
            if fn is not None:
                fn.argtypes = [ctypes.POINTER(_Timex)]
                fn.restype = ctypes.c_int
                self._adjtimex = fn

    def wall_minus_mono(self) -> float:
        # Bracket the wall read with two monotonic reads to bound the sampling error.
        m0 = time.monotonic_ns()
        w = time.time_ns()
        m1 = time.monotonic_ns()
        return (w - (m0 + m1) / 2) / 1e9

    def status(self) -> ClockStatus:
        if self._adjtimex is None:
            return ClockStatus(synchronized=None, source="unavailable")
        tx = _Timex()
        state = self._adjtimex(ctypes.byref(tx))
        if state < 0:
            return ClockStatus(synchronized=None, source="adjtimex_error")
        synced = state != _TIME_ERROR and not (tx.status & _STA_UNSYNC)
        offset = tx.offset / (1e6 if tx.status & _STA_NANO else 1e3)
        return ClockStatus(
            synchronized=synced,
            est_error_ms=tx.esterror / 1000.0,
            max_error_ms=tx.maxerror / 1000.0,
            offset_ms=offset,
            source="adjtimex",
        )


class SimulatedClock:
    """Deterministic clock for replay and fault-injection tests."""

    def __init__(self, wall_minus_mono: float, synchronized: bool | None = True) -> None:
        self.offset = wall_minus_mono
        self.synchronized = synchronized

    def step(self, seconds: float) -> None:
        self.offset += seconds

    def wall_minus_mono(self) -> float:
        return self.offset

    def status(self) -> ClockStatus:
        return ClockStatus(synchronized=self.synchronized, est_error_ms=0.0 if self.synchronized else None, source="simulated")
