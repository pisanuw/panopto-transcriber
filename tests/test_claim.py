"""Regression tests for the cross-machine claim/lockfile coordination.

`claim.try_claim` is what stops two workers (potentially on different hosts
over NFS) from processing the same course. The contract under test:
  * the first claim on a course succeeds and writes a signature file;
  * a second claim while the first is active is refused (returns None);
  * a claim whose mtime is older than `stale_after` is reclaimed;
  * `release` removes the file and is safe to call twice.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from panopto_transcriber.claim import Heartbeat, release, try_claim


def test_first_claim_succeeds_and_writes_signature(tmp_path: Path) -> None:
    claim = try_claim(tmp_path / "course")
    assert claim is not None
    assert claim.exists()
    # Signature is "<host>/<pid>\t<iso-timestamp>".
    contents = claim.read_text()
    assert str(os.getpid()) in contents


def test_second_active_claim_is_refused(tmp_path: Path) -> None:
    course = tmp_path / "course"
    first = try_claim(course)
    assert first is not None
    # A second worker reaching the same active course gets nothing.
    assert try_claim(course) is None


def test_stale_claim_is_reclaimed(tmp_path: Path) -> None:
    course = tmp_path / "course"
    first = try_claim(course, stale_after=1)
    assert first is not None
    # Backdate the claim well past the stale window.
    old = time.time() - 3600
    os.utime(first, (old, old))
    reclaimed = try_claim(course, stale_after=1)
    assert reclaimed is not None
    assert reclaimed == first


def test_fresh_claim_within_window_is_not_reclaimed(tmp_path: Path) -> None:
    course = tmp_path / "course"
    assert try_claim(course, stale_after=10_000) is not None
    # Still fresh -> a peer must not steal it.
    assert try_claim(course, stale_after=10_000) is None


def test_release_allows_reclaim(tmp_path: Path) -> None:
    course = tmp_path / "course"
    claim = try_claim(course)
    assert claim is not None
    release(claim)
    assert not claim.exists()
    # After release the course is free again.
    assert try_claim(course) is not None


def test_release_is_idempotent(tmp_path: Path) -> None:
    claim = try_claim(tmp_path / "course")
    assert claim is not None
    release(claim)
    release(claim)  # second call must not raise


def test_heartbeat_refreshes_mtime(tmp_path: Path) -> None:
    claim = try_claim(tmp_path / "course")
    assert claim is not None
    old = time.time() - 3600
    os.utime(claim, (old, old))
    before = claim.stat().st_mtime
    with Heartbeat(claim, interval=0):  # interval=0 -> beats immediately
        # Give the daemon thread a moment to touch the file.
        deadline = time.time() + 2
        while claim.stat().st_mtime <= before and time.time() < deadline:
            time.sleep(0.01)
    assert claim.stat().st_mtime > before
