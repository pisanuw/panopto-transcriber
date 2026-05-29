"""Persist Canvas/Panopto auth material to local files for manual reuse.

The Canvas token comes straight from the environment (.env). The Panopto cookies
are read out of the user's browser via yt-dlp's cookie helper — the same source
yt-dlp uses for downloading — and written as a ready-to-paste `Cookie:` header.

All files live under `.tokens/` (gitignored) with 0600 permissions.
"""
from __future__ import annotations

from pathlib import Path

from yt_dlp.cookies import extract_cookies_from_browser

TOKENS_DIR = Path(".tokens")


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

    Produces two files:
      panopto.txt          — `name=value; name=value` header you can drop into curl
      panopto_cookies.tsv  — name<TAB>value<TAB>domain<TAB>path, one per cookie
    Returns the header file, or None if no Panopto cookies were found.
    """
    jar = extract_cookies_from_browser(browser, profile=profile)
    matches = [c for c in jar if host in c.domain or "panopto.com" in c.domain]
    if not matches:
        return None

    out_dir = _ensure_dir()
    header = "; ".join(f"{c.name}={c.value}" for c in matches)
    header_path = out_dir / "panopto.txt"
    _write_secret(header_path, header)

    tsv = "\n".join(f"{c.name}\t{c.value}\t{c.domain}\t{c.path}" for c in matches)
    _write_secret(out_dir / "panopto_cookies.tsv", tsv)

    return header_path
