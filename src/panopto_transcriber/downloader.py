"""Panopto downloader backed by yt-dlp.

We don't have OAuth API access, so we piggy-back on the user's existing
browser SSO session: yt-dlp's `cookiesfrombrowser` option reads cookies
directly from Chrome/Safari/Firefox and uses them to fetch the video.

Supports both single sessions (Viewer.aspx?id=<guid>) and folders
(Sessions/List.aspx?folderID=<guid>) — yt-dlp's Panopto extractor
enumerates folder contents automatically.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from pathlib import Path

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from ._progress import fmt_duration

logger = logging.getLogger(__name__)

GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
ARCHIVE_FILENAME = ".yt-dlp-archive.txt"


def _is_guid(s: str) -> bool:
    try:
        uuid.UUID(s)
    except ValueError:
        return False
    return bool(GUID_RE.match(s))


def _viewer_url(panopto_host: str, session_or_url: str) -> str:
    if _is_guid(session_or_url):
        return f"https://{panopto_host}/Panopto/Pages/Viewer.aspx?id={session_or_url}"
    return session_or_url


def _folder_url(panopto_host: str, folder_or_url: str) -> str:
    if _is_guid(folder_or_url):
        return f"https://{panopto_host}/Panopto/Pages/Sessions/List.aspx?folderID={folder_or_url}"
    return folder_or_url


def _ydl_opts(
    out_dir: Path,
    cookies_browser: str,
    cookies_profile: str | None,
    cookies_file: Path | None = None,
) -> dict:
    opts: dict = {
        "outtmpl": str(out_dir / "%(title)s [%(id)s].%(ext)s"),
        "format": "bestvideo*+bestaudio/best",
        "merge_output_format": "mp4",
        "quiet": False,
        "noprogress": False,
        "writesubtitles": False,
        "restrictfilenames": True,
        "ignoreerrors": False,
        "download_archive": str(out_dir / ARCHIVE_FILENAME),
    }
    if cookies_file:
        opts["cookiefile"] = str(cookies_file)
    else:
        cookies_tuple: tuple = (cookies_browser,)
        if cookies_profile:
            cookies_tuple = (cookies_browser, cookies_profile)
        opts["cookiesfrombrowser"] = cookies_tuple
    return opts


def _expired_session_error(
    host: str, browser: str, cookies_file: Path | None, underlying: str
) -> RuntimeError:
    if cookies_file:
        hint = (
            f"the cookies in {cookies_file} have expired. "
            f"Re-export them on a machine signed in to https://{host} "
            f"(uv run panopto-transcriber dump-tokens), "
            f"then copy the file back to {cookies_file}."
        )
    else:
        hint = (
            f"your browser session may have expired. "
            f"Open https://{host} in {browser}, sign in, then retry."
        )
    return RuntimeError(
        f"Panopto download failed — {hint}\nUnderlying error: {underlying}"
    )


def _extract_filepath(entry: dict) -> Path | None:
    if not entry:
        return None
    downloads = entry.get("requested_downloads") or []
    if downloads and downloads[0].get("filepath"):
        return Path(downloads[0]["filepath"])
    fn = entry.get("_filename") or entry.get("filepath")
    return Path(fn) if fn else None


def _find_archived_session(out_dir: Path, session_or_url: str) -> Path | None:
    """If yt-dlp's archive already lists this session, find the saved file by GUID."""
    guid = session_or_url if _is_guid(session_or_url) else None
    if guid is None:
        m = re.search(r"id=([0-9a-fA-F-]{36})", session_or_url)
        guid = m.group(1) if m else None
    if guid is None:
        return None
    matches = list(out_dir.glob(f"*[[]{guid}].*"))
    return matches[0] if matches else None


def download_session(
    session_or_url: str,
    out_dir: Path,
    *,
    panopto_host: str,
    cookies_browser: str = "chrome",
    cookies_profile: str | None = None,
    cookies_file: Path | None = None,
) -> Path:
    """Download a single Panopto session and return the saved file path.

    If the session is already recorded in `<out_dir>/.yt-dlp-archive.txt`,
    yt-dlp skips the download; we then locate the existing file by GUID and
    return its path so the caller can still hand it to a transcriber.
    """
    url = _viewer_url(panopto_host, session_or_url)
    out_dir.mkdir(parents=True, exist_ok=True)

    with YoutubeDL(_ydl_opts(out_dir, cookies_browser, cookies_profile, cookies_file)) as ydl:
        try:
            info = ydl.extract_info(url, download=True)
        except DownloadError as e:
            msg = str(e)
            if "cookies" in msg.lower() or "login" in msg.lower() or "403" in msg:
                raise _expired_session_error(
                    panopto_host, cookies_browser, cookies_file, msg
                ) from e
            raise

    path = _extract_filepath(info or {})
    if path:
        return path

    # yt-dlp reported nothing → almost always means "already in archive".
    existing = _find_archived_session(out_dir, session_or_url)
    if existing:
        logger.info(f"Already downloaded: {existing}")
        return existing
    raise RuntimeError(
        "yt-dlp skipped download (already in archive) but no matching file "
        f"was found in {out_dir}. Remove the GUID from "
        f"{out_dir / ARCHIVE_FILENAME} to force a re-download."
    )


def _enumerate_folder(
    folder_url: str,
    out_dir: Path,
    cookies_browser: str,
    cookies_profile: str | None,
    cookies_file: Path | None,
    panopto_host: str,
) -> list[dict]:
    """List sessions in a Panopto folder without downloading anything."""
    enum_opts = dict(
        _ydl_opts(out_dir, cookies_browser, cookies_profile, cookies_file),
        extract_flat="in_playlist",
        quiet=True,
        noprogress=True,
    )
    with YoutubeDL(enum_opts) as ydl:
        try:
            info = ydl.extract_info(folder_url, download=False)
        except DownloadError as e:
            msg = str(e)
            if "cookies" in msg.lower() or "login" in msg.lower() or "403" in msg:
                raise _expired_session_error(
                    panopto_host, cookies_browser, cookies_file, msg
                ) from e
            raise
    return (info or {}).get("entries") or []


def download_folder(
    folder_or_url: str,
    out_dir: Path,
    *,
    panopto_host: str,
    cookies_browser: str = "chrome",
    cookies_profile: str | None = None,
    cookies_file: Path | None = None,
) -> list[Path]:
    """Download every session in a Panopto folder.

    Already-downloaded sessions are skipped via yt-dlp's download archive
    (`.yt-dlp-archive.txt` in `out_dir`). Prints per-session timing plus a
    running ETA based on the average so far.
    """
    folder_url = _folder_url(panopto_host, folder_or_url)
    out_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"Listing folder {folder_url} ...")
    entries = _enumerate_folder(
        folder_url, out_dir, cookies_browser, cookies_profile, cookies_file, panopto_host
    )
    if not entries:
        logger.error("Folder is empty or could not be enumerated.")
        return []

    n = len(entries)
    logger.info(f"Folder contains {n} session(s). Starting downloads.")

    opts = _ydl_opts(out_dir, cookies_browser, cookies_profile, cookies_file)
    opts["ignoreerrors"] = True

    paths: list[Path] = []
    failures = 0
    batch_start = time.monotonic()

    for i, entry in enumerate(entries, start=1):
        session_url = entry.get("url") or _viewer_url(panopto_host, entry.get("id", ""))
        title = entry.get("title") or entry.get("id") or session_url
        prefix = f"[{i}/{n}]"
        logger.info(f"{prefix} {title}")

        file_start = time.monotonic()
        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(session_url, download=True)
        except DownloadError as e:
            msg = str(e)
            if "cookies" in msg.lower() or "login" in msg.lower() or "403" in msg:
                raise _expired_session_error(
                    panopto_host, cookies_browser, cookies_file, msg
                ) from e
            failures += 1
            logger.error(f"{prefix} FAILED in {fmt_duration(time.monotonic() - file_start)}: {msg}")
            continue

        now = time.monotonic()
        file_elapsed = now - file_start
        total_elapsed = now - batch_start
        avg = total_elapsed / i
        eta = (n - i) * avg

        path = _extract_filepath(info or {})
        if path and path.exists():
            paths.append(path)
            status = f"saved ({fmt_duration(file_elapsed)})"
        else:
            status = f"cached/skipped ({fmt_duration(file_elapsed)})"

        logger.info(
            f"{prefix} {status}. "
            f"Elapsed: {fmt_duration(total_elapsed)}. "
            f"ETA: {fmt_duration(eta)} (avg {fmt_duration(avg)}/session)"
        )

    total = time.monotonic() - batch_start
    logger.info(
        f"Downloaded {len(paths)} new file(s) in {fmt_duration(total)}"
        + (f"; {failures} failure(s)" if failures else "")
    )
    return paths
