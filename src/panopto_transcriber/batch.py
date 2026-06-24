"""Batch helpers — iterate a directory of media files and transcribe each one,
skipping anything that already has a transcript in the output directory.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from ._progress import fmt_duration
from .downloader import (
    _enumerate_folder,
    _expired_session_error,
    _extract_filepath,
    _folder_url,
    _viewer_url,
    _ydl_opts,
)
from .transcribers.base import Transcriber, TranscriptionResult

logger = logging.getLogger(__name__)

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
        logger.info(f"No media files found in {in_dir}")
        return []

    todo = [m for m in media if not already_transcribed(m, transcript_dir)]
    skipped = len(media) - len(todo)
    n = len(todo)
    logger.info(
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
        logger.info(f"{prefix} {media_path.name} — transcribing...")
        file_start = time.monotonic()
        try:
            result = transcriber.transcribe(media_path, transcript_dir)
        except Exception as e:  # noqa: BLE001 — surface and continue
            failures += 1
            logger.error(f"{prefix} FAILED in {fmt_duration(time.monotonic() - file_start)}: {e}")
            continue
        results.append(result)

        now = time.monotonic()
        file_elapsed = now - file_start
        total_elapsed = now - batch_start
        avg = total_elapsed / i
        eta = (n - i) * avg
        logger.info(
            f"{prefix} done in {fmt_duration(file_elapsed)}. "
            f"Elapsed: {fmt_duration(total_elapsed)}. "
            f"ETA: {fmt_duration(eta)} (avg {fmt_duration(avg)}/file)"
        )

    total = time.monotonic() - batch_start
    logger.info(
        f"Transcribed {len(results)} file(s) in {fmt_duration(total)}"
        + (f"; {failures} failure(s)" if failures else "")
    )
    return results


def run_folder_streaming(
    folder_or_url: str,
    out_dir: Path,
    transcript_dir: Path,
    transcriber: Transcriber,
    *,
    panopto_host: str,
    cookies_browser: str,
    cookies_profile: str | None,
    cookies_file: Path | None,
    delete_media: bool,
) -> list[TranscriptionResult]:
    """For each session in `folder_or_url`: download → transcribe → optionally delete.

    Designed for disk-constrained machines: only one media file lives on disk
    at a time when `delete_media=True`. Already-downloaded sessions (per the
    yt-dlp archive) are skipped at the download step; already-transcribed
    media (per a matching `.txt` in `transcript_dir`) skips the transcribe
    step and still gets deleted when `delete_media=True`.
    """
    folder_url = _folder_url(panopto_host, folder_or_url)
    out_dir.mkdir(parents=True, exist_ok=True)
    transcript_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"Listing folder {folder_url} ...")
    entries = _enumerate_folder(
        folder_url, out_dir, cookies_browser, cookies_profile, cookies_file, panopto_host
    )
    if not entries:
        logger.error("Folder is empty or could not be enumerated.")
        return []

    n = len(entries)
    mode = "delete-after-transcribe" if delete_media else "keep-media"
    logger.info(f"Folder contains {n} session(s). Streaming mode ({mode}).")

    opts = _ydl_opts(out_dir, cookies_browser, cookies_profile, cookies_file)

    results: list[TranscriptionResult] = []
    failures = 0
    batch_start = time.monotonic()

    for i, entry in enumerate(entries, start=1):
        session_url = entry.get("url") or _viewer_url(panopto_host, entry.get("id", ""))
        title = entry.get("title") or entry.get("id") or session_url
        prefix = f"[{i}/{n}]"
        logger.info(f"{prefix} {title}")

        session_start = time.monotonic()

        media_path: Path | None = None
        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(session_url, download=True)
            media_path = _extract_filepath(info or {})
        except DownloadError as e:
            msg = str(e)
            if "cookies" in msg.lower() or "login" in msg.lower() or "403" in msg:
                raise _expired_session_error(
                    panopto_host, cookies_browser, cookies_file, msg
                ) from e
            failures += 1
            logger.error(f"{prefix} download FAILED: {msg}")
            continue

        if not media_path or not media_path.exists():
            logger.info(f"{prefix} already in download archive; skipping")
            continue

        if already_transcribed(media_path, transcript_dir):
            logger.info(f"{prefix} transcript already exists, skipping transcribe step")
        else:
            try:
                result = transcriber.transcribe(media_path, transcript_dir)
                results.append(result)
            except Exception as e:  # noqa: BLE001 — keep going on per-session failures
                failures += 1
                logger.error(f"{prefix} transcription FAILED: {e}")
                continue

        if delete_media:
            try:
                media_path.unlink()
                logger.info(f"{prefix} deleted {media_path.name}")
            except OSError as e:
                logger.error(f"{prefix} could not delete {media_path}: {e}")

        now = time.monotonic()
        elapsed = now - session_start
        total_elapsed = now - batch_start
        avg = total_elapsed / i
        eta = (n - i) * avg
        logger.info(
            f"{prefix} done in {fmt_duration(elapsed)}. "
            f"Elapsed: {fmt_duration(total_elapsed)}. "
            f"ETA: {fmt_duration(eta)} (avg {fmt_duration(avg)}/session)"
        )

    total = time.monotonic() - batch_start
    logger.info(
        f"Streamed {len(results)} session(s) in {fmt_duration(total)}"
        + (f"; {failures} failure(s)" if failures else "")
    )
    return results
