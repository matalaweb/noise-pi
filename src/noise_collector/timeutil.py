"""UTC formatting helpers shared by storage and the wire contract."""

from __future__ import annotations

import time
from datetime import datetime, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(ts: float | datetime, ms: bool = True) -> str:
    """RFC 3339 UTC string with millisecond precision and a ``Z`` suffix."""
    if isinstance(ts, datetime):
        dt = ts.astimezone(timezone.utc)
    else:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    if ms:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_second(utc_second: int) -> str:
    return iso_utc(float(utc_second))


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def monotonic() -> float:
    return time.monotonic()
