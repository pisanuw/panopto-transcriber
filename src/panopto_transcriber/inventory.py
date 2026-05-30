"""Count active vs archived Panopto sessions per folder.

Panopto's web UI shows a `TotalNumber` of sessions in each folder. The
underlying endpoint is `Services/Data.svc/GetSessions` — a WCF JSON service
the web app uses internally. It accepts session cookies (the same ones
yt-dlp uses for downloads), so we can call it directly with httpx + cookies
without any OAuth dance.

We don't have a "list archived only" filter, but we do have
`includeArchived` as a boolean. So: query twice (false / true) and subtract
to get the archived count.
"""
from __future__ import annotations

from dataclasses import dataclass
from http.cookiejar import MozillaCookieJar
from pathlib import Path

import httpx
from yt_dlp.cookies import extract_cookies_from_browser


@dataclass(frozen=True)
class FolderCounts:
    active: int
    archived: int

    @property
    def total(self) -> int:
        return self.active + self.archived


def load_panopto_cookies(
    cookies_browser: str,
    cookies_profile: str | None,
    cookies_file: Path | None,
) -> dict[str, str]:
    """Return a {name: value} dict of cookies for *.panopto.com."""
    cookies: dict[str, str] = {}
    if cookies_file:
        jar = MozillaCookieJar(str(cookies_file))
        jar.load(ignore_discard=True, ignore_expires=True)
        source = jar
    else:
        source = extract_cookies_from_browser(cookies_browser, profile=cookies_profile)
    for c in source:
        if c.domain and "panopto.com" in c.domain:
            cookies[c.name] = c.value
    return cookies


def count_folder_sessions(
    panopto_host: str,
    folder_id: str,
    cookies: dict[str, str],
    *,
    timeout: float = 30.0,
) -> FolderCounts:
    """Return active/archived session counts for `folder_id` on `panopto_host`.

    Mirrors the exact `GetSessions` payload Panopto's own web UI uses to
    populate the "This folder contains N archived videos" footer:
    `getFolderData` + `includeArchivedStateCount` return `TotalNumber`
    (active sessions) and `ArchivedCount` in one call.
    """
    url = f"https://{panopto_host}/Panopto/Services/Data.svc/GetSessions"
    body = {"queryParameters": {
        "folderID": folder_id,
        "page": 0,
        "maxResults": 1,  # we only need the counters
        "sortColumn": 1,
        "sortAscending": False,
        "bookmarked": False,
        "getFolderData": True,
        "isSharedWithMe": False,
        "isSubscriptionsPage": False,
        "includeArchived": True,
        "includeArchivedStateCount": True,
        "sessionListOnlyArchived": False,
        "includePlaylists": False,
    }}
    r = httpx.post(
        url,
        json=body,
        cookies=cookies,
        timeout=timeout,
        headers={"Accept": "application/json"},
    )
    r.raise_for_status()
    data = r.json()
    wrapped = data.get("d", data) if isinstance(data, dict) else {}
    if not isinstance(wrapped, dict):
        raise RuntimeError(f"Unexpected GetSessions response shape: {data!r}")
    active = wrapped.get("TotalNumber")
    archived = wrapped.get("ArchivedCount")
    if active is None:
        raise RuntimeError(
            f"GetSessions response missing TotalNumber: keys={list(wrapped)}"
        )
    return FolderCounts(active=int(active), archived=int(archived or 0))
