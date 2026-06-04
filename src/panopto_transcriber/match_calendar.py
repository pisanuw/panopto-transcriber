"""Match orphan transcripts to courses using a Google Calendar export.

For each transcript file:
  1. Extract the recording timestamp from the filename (yt-dlp embeds it).
  2. Look up calendar events on that day, expanding RRULEs properly via
     `recurring_ical_events`.
  3. Filter to events whose SUMMARY contains a 3-digit course number
     (e.g. "343", "343a", "CSS 343 B", "Midterm 343"). Office hours,
     meetings, etc. don't have a code so they're ignored.
  4. Map the (number, optional section letter, term) to a course in
     `courses.yml` and move the transcript to that course's subdir.

If the calendar event omits the section letter (e.g. just "343"), the match
only succeeds when exactly one section with that number was taught that
quarter; otherwise the transcript is left alone and reported as ambiguous.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import icalendar
import recurring_ical_events

from .canvas import CourseEntry

LA_TZ = ZoneInfo("America/Los_Angeles")

# Matches a 3-digit course code, optionally followed by a section letter.
# Examples that match: "343", "343a", "CSS 343 B", "Midterm 343 review".
CODE_RE = re.compile(r"\b(\d{3})\s*([A-Za-z])?\b")

# Recording filename formats we've seen in the wild:
#   Full:        "Monday_April_3_2023_at_10_31_35_AM"
#   Doubled:     "Tuesday__October_10__2023_at_11_01_57_AM"  (Panopto template glitch)
#   Abbreviated: "Tue_Jan_03_2023_10_56_39_AM"               (older / different upload path)
# Both groups: weekday, month, day, year, hour, min, sec, AM/PM marker.
FILENAME_TS_FULL_RE = re.compile(
    r"^[A-Z][a-z]+_+([A-Z][a-z]+)_+(\d{1,2})_+(\d{4})_+at_+(\d{1,2})_+(\d{2})_+(\d{2})_+([AP])M"
)
FILENAME_TS_ABBR_RE = re.compile(
    r"^[A-Z][a-z]{2}_+([A-Z][a-z]{2})_+(\d{1,2})_+(\d{4})_+(\d{1,2})_+(\d{2})_+(\d{2})_+([AP])M"
)

_FULL_MONTHS = ["January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November", "December"]
MONTH_NUM = {m: i + 1 for i, m in enumerate(_FULL_MONTHS)}
MONTH_NUM.update({m[:3]: i + 1 for i, m in enumerate(_FULL_MONTHS)})


def parse_filename_datetime(name: str) -> datetime | None:
    """Pull a Pacific-time datetime out of a yt-dlp filename."""
    for rx in (FILENAME_TS_FULL_RE, FILENAME_TS_ABBR_RE):
        m = rx.match(name)
        if m:
            mon, day, year, hh, mm, ss, ampm = m.groups()
            month_num = MONTH_NUM.get(mon)
            if month_num is None:
                continue
            h = int(hh) % 12
            if ampm == "P":
                h += 12
            return datetime(
                int(year), month_num, int(day), h, int(mm), int(ss),
                tzinfo=LA_TZ,
            )
    return None


def date_to_term_label(d: date) -> str:
    """Map a date to a UW quarter label like 'Spring 2023'.

    Uses approximate UW quarter boundaries — close enough to handle the
    Winter/Spring boundary (late March) and Summer/Autumn boundary (late
    September) that month-bucket logic gets wrong.
        Winter: Jan 1   - Mar 20
        Spring: Mar 21  - Jun 15
        Summer: Jun 16  - Sep 21
        Autumn: Sep 22  - Dec 31
    """
    md = (d.month, d.day)
    if md < (3, 21):
        return f"Winter {d.year}"
    if md < (6, 16):
        return f"Spring {d.year}"
    if md < (9, 22):
        return f"Summer {d.year}"
    return f"Autumn {d.year}"


@dataclass(frozen=True)
class ClassEvent:
    start: datetime
    end: datetime
    summary: str
    code_number: str
    section: str | None  # uppercase letter, or None


def _ensure_aware(dt) -> datetime | None:
    """Force a datetime to Pacific time. Skip whole-day date values."""
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=LA_TZ)
    return dt


def expand_class_events(
    ics_path: Path, start: date, end: date
) -> list[ClassEvent]:
    """Return every class-like calendar event in [start, end)."""
    cal = icalendar.Calendar.from_ical(ics_path.read_bytes())
    out: list[ClassEvent] = []
    for ev in recurring_ical_events.of(cal).between(start, end):
        summary = str(ev.get("SUMMARY", "") or "").strip()
        if not summary:
            continue
        m = CODE_RE.search(summary)
        if not m:
            continue
        s = _ensure_aware(ev.get("DTSTART").dt)
        if s is None:
            continue
        end_field = ev.get("DTEND")
        e = _ensure_aware(end_field.dt) if end_field else None
        if e is None:
            e = s + timedelta(hours=1)
        out.append(ClassEvent(
            start=s, end=e, summary=summary,
            code_number=m.group(1),
            section=m.group(2).upper() if m.group(2) else None,
        ))
    return out


def build_course_index(entries: list[CourseEntry]):
    """Index courses.yml by (number, section, term).

    Only includes entries with `panopto_folder` set — entries without a
    folder can't receive transcripts anyway, and including them inflates the
    section count for the ambiguity check (e.g. a "Repository for CSS 342-343"
    that shares the term with the real section would force every 342 lecture
    to be flagged as ambiguous).
    """
    by_key: dict[tuple[str, str, str], CourseEntry] = {}
    by_num_term: dict[tuple[str, str], list[CourseEntry]] = {}
    for e in entries:
        if not e.code or not e.panopto_folder:
            continue
        m = CODE_RE.search(e.code)
        if not m:
            continue
        num = m.group(1)
        section = (m.group(2) or "").upper()
        term = e.term or ""
        by_key[(num, section, term)] = e
        by_num_term.setdefault((num, term), []).append(e)
    return by_key, by_num_term


def pick_course(
    event: ClassEvent,
    term_label: str,
    by_key,
    by_num_term,
) -> tuple[CourseEntry | None, str]:
    """Return (course, reason). course is None if unmatchable; reason is
    a short tag like 'exact', 'unique-section', 'ambiguous', 'no-course'.
    """
    if event.section:
        c = by_key.get((event.code_number, event.section, term_label))
        if c:
            return c, "exact"
        # Calendar says e.g. CSS 343 B, but YAML doesn't have that section
        # for that term — skip rather than guess.
        return None, "section-not-in-yaml"
    # Section unknown in calendar — only match if exactly one section
    # exists in YAML for this (number, term).
    options = by_num_term.get((event.code_number, term_label), [])
    if len(options) == 1:
        return options[0], "unique-section"
    if len(options) == 0:
        return None, "no-course"
    return None, "ambiguous"
