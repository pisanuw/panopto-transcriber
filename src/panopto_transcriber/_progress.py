"""Shared helpers for human-readable progress output."""
from __future__ import annotations


def fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m}m{s:02d}s"
    h, rem = divmod(seconds, 3600)
    m, _ = divmod(rem, 60)
    return f"{h}h{m:02d}m"
