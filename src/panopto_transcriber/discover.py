"""Auto-populate `panopto_folder` GUIDs in courses.yml by driving the LTI launch.

For each course in the YAML whose `panopto_folder` is still empty, we:
  1. Ask Canvas if the course has a Panopto tab in its visible nav. If not,
     the course is silently skipped — many courses simply don't use Panopto.
  2. Ask Canvas for a `sessionless_launch` URL — Canvas signs an LTI 1.x
     launch payload and embeds a one-time token in the returned URL.
  3. Drive Playwright (with the user's Chrome cookies pre-loaded into the
     context) to that URL. Canvas's auto-submitting form POSTs to Panopto,
     Panopto sets its session cookie, and the final page URL contains
     `folderID=<GUID>` — which we extract.
  4. Rewrite courses.yml line-by-line so existing comments are preserved.

Playwright is an optional dep (uv extra `discover`); the import is lazy so
the rest of the CLI keeps working without it.
"""
from __future__ import annotations

import re
from pathlib import Path

import httpx
from yt_dlp.cookies import extract_cookies_from_browser

# Matches `folderID=<GUID>` whether the GUID is bare, wrapped in literal
# quotes, or URL-encoded as %22 (which Panopto's embed iframe URL uses,
# e.g. `…#folderID=%22f77faf32-…-b01f01628e04%22`).
PANOPTO_FOLDER_RE = re.compile(
    r'folderID=(?:%22|"|\')?([0-9a-fA-F-]{36})', re.IGNORECASE
)


def find_panopto_tab_url(canvas_url: str, token: str, course_id: int) -> str | None:
    """Return the web URL of the course's visible Panopto tab, or None.

    Uses `/api/v1/courses/:id/tabs` so courses where the tool is installed but
    the tab is hidden are treated as "no Panopto" — matching what an
    instructor's actual nav looks like. Returns the tab's `full_url`, which
    Playwright can navigate to directly (with Chrome cookies) to trigger the
    LTI launch exactly like a user clicking the tab.
    """
    r = httpx.get(
        f"{canvas_url.rstrip('/')}/api/v1/courses/{course_id}/tabs",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    r.raise_for_status()
    for tab in r.json():
        if tab.get("hidden"):
            continue
        label = (tab.get("label") or "").lower()
        if "panopto" not in label:
            continue
        return tab.get("full_url") or tab.get("html_url")
    return None


# Year 2100 in unix seconds — anything beyond this is almost certainly garbage
# (a Chrome microsecond value yt-dlp didn't normalize, or an int overflow).
_MAX_REASONABLE_EXPIRES = 4_102_444_800


def _coerce_expires(raw) -> float:
    """Return a Playwright-valid `expires` field: -1 or a sane positive int."""
    if not raw:
        return -1.0
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return -1.0
    if v <= 0 or v > _MAX_REASONABLE_EXPIRES:
        return -1.0
    return float(v)


def chrome_cookies_for_playwright(profile: str | None) -> list[dict]:
    """Pull Chrome cookies via yt-dlp's helper, convert to Playwright's dict format.

    We feed everything in (Canvas, UW SSO, Panopto) because the LTI launch
    redirect chain touches all three origins. Cookies with malformed names,
    domains, or out-of-range `expires` are dropped or downgraded to session
    cookies so Playwright's strict validator doesn't reject the whole batch.
    """
    jar = extract_cookies_from_browser("chrome", profile=profile)
    out: list[dict] = []
    for c in jar:
        if not c.name or not c.domain:
            continue
        out.append(
            {
                "name": c.name,
                "value": c.value or "",
                "domain": c.domain,
                "path": c.path or "/",
                "expires": _coerce_expires(c.expires),
                "httpOnly": False,  # yt-dlp doesn't reliably expose this
                "secure": bool(c.secure),
                # 'Lax' is the broadly-compatible default; cross-site LTI
                # POSTs that need 'None' will fall back to a fresh login the
                # first time, which Playwright can do interactively if --headed.
                "sameSite": "Lax",
            }
        )
    return out


def extract_folder_id_from_page(page) -> str | None:
    """Look at every frame's URL + the rendered HTML for a `folderID=<GUID>`."""
    for frame in page.frames:
        m = PANOPTO_FOLDER_RE.search(frame.url or "")
        if m:
            return m.group(1)
    try:
        html = page.content()
    except Exception:
        return None
    m = PANOPTO_FOLDER_RE.search(html)
    return m.group(1) if m else None


def update_yaml_in_place(yaml_path: Path, updates: dict[int, str]) -> int:
    """Replace `panopto_folder: ""` with `panopto_folder: "<guid>"` per canvas_id.

    Walks the file line by line, tracking which `canvas_id` block we're in,
    so existing comments, formatting, and key order are preserved. Returns
    the number of lines actually modified.
    """
    canvas_id_re = re.compile(r"^\s+(?:-\s+)?canvas_id:\s*(\d+)\s*$")
    blank_folder_re = re.compile(r'^(\s+panopto_folder:\s*)""(\s*#.*)?$')

    current_id: int | None = None
    modified = 0
    out_lines: list[str] = []

    for line in yaml_path.read_text().splitlines():
        m_id = canvas_id_re.match(line)
        if m_id:
            current_id = int(m_id.group(1))
        else:
            m_blank = blank_folder_re.match(line)
            if m_blank and current_id is not None and current_id in updates:
                guid = updates[current_id]
                trailing = m_blank.group(2) or ""
                line = f'{m_blank.group(1)}"{guid}"{trailing}'
                modified += 1
        out_lines.append(line)

    yaml_path.write_text("\n".join(out_lines) + "\n")
    return modified
