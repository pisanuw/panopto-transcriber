"""Batch helpers — iterate a directory of media files and transcribe each one,
skipping anything that already has a transcript in the output directory.
"""
from __future__ import annotations

import time
from pathlib import Path

from ._progress import fmt_duration
from .transcribers.base import Transcriber, TranscriptionResult

MEDIA_EXTENSIONS = {".mp4", ".m4a", ".mp3", ".wav", ".mkv", ".webm", ".mov"}


def find_media(in_dir: Path) -> list[Path]:
    return sorted(
        p for p in in_dir.iterdir()
        if p.is_file() and p.suffix.lower() in MEDIA_EXTENSIONS
    )


def already_transcribed(media: Path, transcript_dir: Path) -> bool:
    txt = transcript_dir / f"{media.stem}.txt"
    return txt.exists() and txt.stat().st_size > 0


def transcribe_directory(
    in_dir: Path,
    transcriber: Transcriber,
    transcript_dir: Path,
) -> list[TranscriptionResult]:
    """Transcribe every media file in `in_dir`, skipping ones already done.

    Prints per-file timing plus a running ETA based on the average so far.
    """
    media = find_media(in_dir)
    if not media:
        print(f"No media files found in {in_dir}")
        return []

    todo = [m for m in media if not already_transcribed(m, transcript_dir)]
    skipped = len(media) - len(todo)
    n = len(todo)
    print(
        f"Found {len(media)} media file(s) in {in_dir}: "
        f"{n} to transcribe, {skipped} already done"
    )
    if n == 0:
        return []

    results: list[TranscriptionResult] = []
    failures = 0
    batch_start = time.monotonic()

    for i, media_path in enumerate(todo, start=1):
        prefix = f"[{i}/{n}]"
        print(f"{prefix} {media_path.name} — transcribing...")
        file_start = time.monotonic()
        try:
            result = transcriber.transcribe(media_path, transcript_dir)
        except Exception as e:  # noqa: BLE001 — surface and continue
            failures += 1
            print(f"{prefix} FAILED in {fmt_duration(time.monotonic() - file_start)}: {e}")
            continue
        results.append(result)

        now = time.monotonic()
        file_elapsed = now - file_start
        total_elapsed = now - batch_start
        avg = total_elapsed / i
        eta = (n - i) * avg
        print(
            f"{prefix} done in {fmt_duration(file_elapsed)}. "
            f"Elapsed: {fmt_duration(total_elapsed)}. "
            f"ETA: {fmt_duration(eta)} (avg {fmt_duration(avg)}/file)"
        )

    total = time.monotonic() - batch_start
    print(
        f"Transcribed {len(results)} file(s) in {fmt_duration(total)}"
        + (f"; {failures} failure(s)" if failures else "")
    )
    return results
