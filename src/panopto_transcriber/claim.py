"""Dynamic per-course claim files for multi-machine parallel runs.

The strategy: workers share TRANSCRIPT_DIR via NFS (or similar). For each
course, the worker tries to atomically create `<course_dir>/.claim`. If the
create succeeds, the worker owns the course until it releases the lock. If
the file already exists, the worker checks its mtime — claims older than
`stale_after` seconds are assumed orphaned (crashed worker) and forcibly
reclaimed.

A background `Heartbeat` thread touches the claim file's mtime every
`interval` seconds so other workers don't mistakenly steal an active claim.

NFSv3 and NFSv4 both honor `O_CREAT | O_EXCL` semantics, so the atomic
create works without additional locking primitives.
"""
from __future__ import annotations

import os
import socket
import threading
import time
from datetime import datetime
from pathlib import Path

DEFAULT_STALE_SECONDS = 1800  # 30 minutes
DEFAULT_HEARTBEAT_SECONDS = 60


def _worker_signature() -> str:
    return f"{socket.gethostname()}/{os.getpid()}"


def try_claim(course_dir: Path, *, stale_after: int = DEFAULT_STALE_SECONDS) -> Path | None:
    """Atomically claim `course_dir`. Returns the claim file path on success,
    None if another active worker already holds it.
    """
    course_dir.mkdir(parents=True, exist_ok=True)
    claim = course_dir / ".claim"

    if claim.exists():
        try:
            age = time.time() - claim.stat().st_mtime
        except FileNotFoundError:
            age = float("inf")
        if age > stale_after:
            try:
                holder = claim.read_text().strip()
            except OSError:
                holder = "(unreadable)"
            print(f"  Breaking stale claim on {course_dir.name} "
                  f"(age={age:.0f}s, holder={holder})")
            try:
                claim.unlink()
            except FileNotFoundError:
                pass  # another worker raced us; that's fine
        else:
            return None  # held by an active worker

    try:
        # 'x' = O_CREAT | O_EXCL; raises FileExistsError if it already exists.
        with open(claim, "x") as f:
            f.write(f"{_worker_signature()}\t{datetime.now().isoformat()}\n")
    except FileExistsError:
        return None  # another worker just took it

    return claim


def release(claim: Path) -> None:
    """Best-effort remove of the claim file."""
    try:
        claim.unlink()
    except FileNotFoundError:
        pass


class Heartbeat:
    """Context manager: touches `path`'s mtime every `interval` seconds.

    Used to keep a claim file fresh so other workers don't steal it as stale.
    Stops cleanly on context exit; daemon thread so it dies with the process.
    """
    def __init__(self, path: Path, *, interval: int = DEFAULT_HEARTBEAT_SECONDS):
        self.path = path
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "Heartbeat":
        self._thread = threading.Thread(target=self._beat, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _beat(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                os.utime(self.path)
            except OSError:
                pass  # don't crash the worker if heartbeat fails
