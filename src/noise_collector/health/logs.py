"""Structured, bounded, redacted logging."""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
import time
from pathlib import Path

from ..transport.api import redact


class JsonFormatter(logging.Formatter):
    def __init__(self, component: str) -> None:
        super().__init__()
        self.component = component

    def format(self, record: logging.LogRecord) -> str:
        msg = redact(record.getMessage())
        out = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "mono": round(time.monotonic(), 3),
            "level": record.levelname,
            "component": self.component,
            "logger": record.name,
            "msg": msg,
        }
        for key in ("session", "error_code", "request_id"):
            if hasattr(record, key):
                out[key] = getattr(record, key)
        if record.exc_info:
            out["exc"] = redact(self.formatException(record.exc_info))[-2000:]
        return json.dumps(out, default=str)


class RateLimitFilter(logging.Filter):
    """Allow at most ``burst`` identical (logger, msg template) records per ``window`` seconds."""

    def __init__(self, burst: int = 5, window: float = 60.0) -> None:
        super().__init__()
        self.burst, self.window = burst, window
        self.seen: dict[tuple, list[float]] = {}

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.CRITICAL:
            return True
        key = (record.name, record.msg, record.levelno)
        now = time.monotonic()
        times = [t for t in self.seen.get(key, []) if now - t < self.window]
        if len(times) >= self.burst:
            self.seen[key] = times
            return False
        times.append(now)
        self.seen[key] = times
        if len(self.seen) > 2000:
            self.seen.clear()
        return True


def setup(component: str, level: str = "INFO", log_dir: Path | None = None, max_bytes: int = 5 * 1024 * 1024, backups: int = 5) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())
    fmt = JsonFormatter(component)
    rl = RateLimitFilter()
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(fmt)
    h.addFilter(rl)
    root.addHandler(h)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(log_dir / f"{component}.log", maxBytes=max_bytes, backupCount=backups)
        fh.setFormatter(fmt)
        fh.addFilter(rl)
        root.addHandler(fh)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
