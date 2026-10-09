"""Container health check: healthy only while acquisition reports progressing work."""

from __future__ import annotations

import sys

from .config.settings import load_settings
from .supervisor import Supervisor


def main() -> int:
    s = load_settings()
    ok, why = Supervisor(s, None).acquisition_progressing()
    print(why)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
