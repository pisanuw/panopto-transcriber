"""Detect orphan transcripts — files whose session GUID isn't in the folder.

Scenario this catches: at some earlier run, a Panopto folder transiently
contained sessions that have since been moved out (re-org, course template
cleanup, mis-configured Canvas LTI mapping). Our transcripts persist on
disk in the per-course subdir even though the underlying session is no
longer in that course. Re-runs of `run-courses` won't notice — they only
look forward (new sessions to download) and skip already-transcribed ones.

`verify-transcripts` checks, for each course in courses.yml:
  - list every session GUID currently in the folder (active + archived,
    paginated)
  - list every transcript file in the course's transcript subdir
  - flag transcript files whose embedded GUID isn't in the folder

The GUID is embedded in the filename by yt-dlp via the outtmpl we use:
    `<title> [<guid>].mp4`  →  `<title> [<guid>].txt|.srt`
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import httpx

# Match the trailing `[<guid>]` yt-dlp embeds in every filename it writes.
FILENAME_GUID_RE = re.compile(r"\[([0-9a-fA-F-]{36})\][^/]*$")

# How many session GUIDs to fetch per Data.svc page. The web UI uses 25,
# but larger is fine and saves round-trips on large folders.
PAGE_SIZE = 100


@dataclass(frozen=True)
class FolderSessions:
    active: set[str]      # GUID set — both DeliveryID + SessionID for matching
    archived: set[str]
    active_count: int     # unique session count, for display
    archived_count: int

    def all(self) -> set[str]:
        return self.active | self.archived


def _post_get_sessions(
    panopto_host: str, folder_id: str, cookies: dict[str, str],
    *, page: int, only_archived: bool, timeout: float = 30.0,
) -> dict:
    body = {"queryParameters": {
        "folderID": folder_id, "page": page, "maxResults": PAGE_SIZE,
        "sortColumn": 1, "sortAscending": False,
        "bookmarked": False, "getFolderData": True,
        "isSharedWithMe": False, "isSubscriptionsPage": False,
        "includeArchived": True, "includeArchivedStateCount": True,
        "sessionListOnlyArchived": only_archived, "includePlaylists": False,
    }}
    r = httpx.post(
        f"https://{panopto_host}/Panopto/Services/Data.svc/GetSessions",
        json=body, cookies=cookies, timeout=timeout,
        headers={"Accept": "application/json"},
    )
    r.raise_for_status()
    return r.json()["d"]


def list_folder_sessions(
    panopto_host: str, folder_id: str, cookies: dict[str, str],
) -> FolderSessions:
    """Return every session GUID in the folder (active + archived, paginated).

    Each session row carries both a `SessionID` and a `DeliveryID`. yt-dlp's
    filenames (and thus our transcript filenames) embed the `DeliveryID` —
    that's the GUID in the `Viewer.aspx?id=…` URL. We include both IDs so the
    orphan check is robust to either kind of identifier ever showing up in a
    filename.
    """
    active: set[str] = set()
    archived: set[str] = set()
    counts = {"active": 0, "archived": 0}

    for bucket, dest in (("active", active), ("archived", archived)):
        only_archived = bucket == "archived"
        page = 0
        while True:
            d = _post_get_sessions(
                panopto_host, folder_id, cookies,
                page=page, only_archived=only_archived,
            )
            results = d.get("Results") or []
            counts[bucket] += len(results)
            for s in results:
                for key in ("DeliveryID", "SessionID"):
                    sid = s.get(key)
                    if sid:
                        dest.add(sid.lower())
            total = d.get("TotalNumber") or 0
            if (page + 1) * PAGE_SIZE >= total or len(results) < PAGE_SIZE:
                break
            page += 1

    return FolderSessions(
        active=active, archived=archived,
        active_count=counts["active"], archived_count=counts["archived"],
    )


def extract_guid_from_filename(name: str) -> str | None:
    """Pull the `[<guid>]` chunk out of a yt-dlp/transcript filename."""
    m = FILENAME_GUID_RE.search(name)
    return m.group(1).lower() if m else None


@dataclass(frozen=True)
class TranscriptFile:
    path: Path
    guid: str


def collect_transcripts(course_dir: Path) -> tuple[list[TranscriptFile], list[Path]]:
    """Return (transcripts-with-guids, files-without-guids) from `course_dir`.

    The transcripts list dedupes by GUID, since `.txt` + `.srt` for one
    session share a GUID. The "no GUID" list catches anything that doesn't
    match the yt-dlp filename pattern (e.g., README files the user dropped in).
    """
    seen: dict[str, Path] = {}
    no_guid: list[Path] = []
    for p in sorted(course_dir.iterdir()):
        if not p.is_file():
            continue
        guid = extract_guid_from_filename(p.name)
        if guid is None:
            no_guid.append(p)
            continue
        # Prefer the `.txt` if both exist — that's the human-readable transcript.
        if guid not in seen or p.suffix.lower() == ".txt":
            seen[guid] = p
    return [TranscriptFile(path=p, guid=g) for g, p in seen.items()], no_guid


def all_files_for_guid(course_dir: Path, guid: str) -> list[Path]:
    """Return every file in `course_dir` whose name carries this GUID."""
    return [p for p in course_dir.iterdir()
            if p.is_file() and extract_guid_from_filename(p.name) == guid]
