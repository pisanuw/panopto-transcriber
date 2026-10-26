"""Minimal Canvas REST client — enough to enumerate the user's courses.

Canvas paginates via `Link` headers (RFC 5988); we follow `rel="next"` until
the server stops sending it. Authentication is a bearer token (Canvas's
"personal access token", set in `.env` as `CANVAS_TOKEN`).
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import yaml


@dataclass(frozen=True)
class Course:
    id: int
    name: str
    course_code: str
    term: str | None
    workflow_state: str


@dataclass(frozen=True)
class CourseEntry:
    """One row from a `courses.yml` mapping file."""
    canvas_id: int | None
    name: str | None
    code: str | None
    term: str | None
    panopto_folder: str  # empty string means "skip"
    out_dir: str | None  # explicit per-course transcript subdir; None → auto-derive


def _paginate(client: httpx.Client, url: str, params: list[tuple[str, str]]) -> Iterator[dict]:
    next_url: str | None = url
    next_params: list[tuple[str, str]] | None = params
    while next_url:
        # httpx's stub types `params` invariantly, so a concrete
        # list[tuple[str, str]] isn't accepted as the wider value-type union.
        r = client.get(next_url, params=next_params)  # type: ignore[arg-type]
        r.raise_for_status()
        body = r.json()
        if not isinstance(body, list):
            raise RuntimeError(
                f"Canvas returned non-list response at {next_url}: {body!r}"
            )
        yield from body
        next_link = r.links.get("next")
        next_url = next_link["url"] if next_link else None
        next_params = None  # subsequent URLs already carry the query string


def list_courses(
    canvas_url: str,
    token: str,
    *,
    enrollment_state: str | None = "active",
) -> list[Course]:
    """Return courses the token's user is enrolled in.

    `enrollment_state` filters by Canvas enrollment state ('active',
    'invited_or_pending', 'completed'); pass None to list everything.
    """
    if not token:
        raise RuntimeError("CANVAS_TOKEN is empty; set it in .env first.")

    params: list[tuple[str, str]] = [
        ("per_page", "100"),
        ("include[]", "term"),
    ]
    if enrollment_state:
        params.append(("enrollment_state", enrollment_state))

    base = canvas_url.rstrip("/") + "/api/v1/courses"
    headers = {"Authorization": f"Bearer {token}"}

    with httpx.Client(headers=headers, timeout=30) as client:
        items = list(_paginate(client, base, params))

    courses: list[Course] = []
    for c in items:
        if "id" not in c or "name" not in c:
            # Restricted/blocked enrollments come back without a name; skip.
            continue
        term = (c.get("term") or {}).get("name")
        courses.append(
            Course(
                id=c["id"],
                name=c["name"],
                course_code=c.get("course_code", ""),
                term=term,
                workflow_state=c.get("workflow_state", ""),
            )
        )
    return courses


def load_courses_yaml(path: Path) -> list[CourseEntry]:
    """Parse the courses.yml file written by `list-courses --out`."""
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict) or "courses" not in data:
        raise RuntimeError(f"{path}: expected a top-level 'courses:' list")
    raw_courses = data["courses"]
    if not isinstance(raw_courses, list):
        raise RuntimeError(f"{path}: 'courses' must be a list of entries")

    entries: list[CourseEntry] = []
    for i, raw in enumerate(raw_courses):
        if not isinstance(raw, dict):
            raise RuntimeError(f"{path}: courses[{i}] must be a mapping, got {raw!r}")
        folder = str(raw.get("panopto_folder") or "").strip()
        entries.append(
            CourseEntry(
                canvas_id=raw.get("canvas_id"),
                name=raw.get("name"),
                code=raw.get("code"),
                term=raw.get("term"),
                panopto_folder=folder,
                out_dir=raw.get("out_dir"),
            )
        )
    return entries
