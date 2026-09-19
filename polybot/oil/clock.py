from __future__ import annotations

import socket
import time
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def instant(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timezone required")
    return result.astimezone(timezone.utc)


def stamp() -> dict:
    return {
        "utc": utc_now(), "monotonic_ns": time.monotonic_ns(),
        "host_id": socket.gethostname(),
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "clock_uncertainty_ms": None,
    }


def seconds(after: str, before: str) -> float:
    return (instant(after) - instant(before)).total_seconds()
