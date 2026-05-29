"""Persist Canvas/Panopto auth material to local files for manual reuse.

The Canvas token comes straight from the environment (.env). The Panopto cookies
are read out of the user's browser via yt-dlp's cookie helper — the same source
yt-dlp uses for downloading — and written as a ready-to-paste `Cookie:` header.

All files live under `.tokens/` (gitignored) with 0600 permissions.
"""
from __future__ import annotations

from pathlib import Path

from yt_dlp.cookies import YoutubeDLCookieJar, extract_cookies_from_browser

TOKENS_DIR = Path(".tokens")
PANOPTO_COOKIES_FILE = TOKENS_DIR / "panopto_cookies.txt"


def _ensure_dir() -> Path:
    TOKENS_DIR.mkdir(parents=True, exist_ok=True)
    return TOKENS_DIR


def _write_secret(path: Path, content: str) -> None:
    path.write_text(content if content.endswith("\n") else content + "\n")
    path.chmod(0o600)


def save_canvas_token(token: str) -> Path | None:
    """Write the Canvas token to `.tokens/canvas.txt`. No-op if empty."""
    if not token:
        return None
    out = _ensure_dir() / "canvas.txt"
    _write_secret(out, token)
    return out


def save_panopto_cookies(browser: str, profile: str | None, host: str) -> Path | None:
    """Extract Panopto cookies from `browser` and write them under `.tokens/`.

    Produces:
      panopto_cookies.txt  — Netscape-format cookies file (feed to yt-dlp's
                             COOKIES_FILE, or `curl -b`); copy this to a
                             headless server to avoid needing a browser there.
      panopto.txt          — `name=value; name=value` Cookie header string.
    Returns the cookies-file path, or None if no Panopto cookies were found.
    """
    jar = extract_cookies_from_browser(browser, profile=profile)
    matches = [c for c in jar if host in c.domain or "panopto.com" in c.domain]
    if not matches:
        return None

    _ensure_dir()
    netscape = YoutubeDLCookieJar()
    for c in matches:
        netscape.set_cookie(c)
    netscape.save(str(PANOPTO_COOKIES_FILE), ignore_discard=True, ignore_expires=True)
    PANOPTO_COOKIES_FILE.chmod(0o600)

    header = "; ".join(f"{c.name}={c.value}" for c in matches)
    _write_secret(TOKENS_DIR / "panopto.txt", header)

    return PANOPTO_COOKIES_FILE
