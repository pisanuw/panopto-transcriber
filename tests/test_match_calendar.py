"""Regression tests for the filename date parser, UW quarter boundaries, and
course matching in `match_calendar`.

The date parser is fed real yt-dlp filenames (three observed formats), and the
quarter-boundary logic decides which term a recording belongs to. Both are
pure functions with fiddly edge cases (12-hour clock, doubled separators,
late-March/late-September boundaries), which is exactly what regression tests
are for.
"""
from __future__ import annotations

from datetime import date

import pytest

from panopto_transcriber.canvas import CourseEntry
from panopto_transcriber.match_calendar import (
    ClassEvent,
    build_course_index,
    date_to_term_label,
    parse_filename_datetime,
    pick_course,
)

# ---- parse_filename_datetime -------------------------------------------------

def test_parse_full_format() -> None:
    dt = parse_filename_datetime("Monday_April_3_2023_at_10_31_35_AM [abc].mp4")
    assert dt is not None
    assert (dt.year, dt.month, dt.day) == (2023, 4, 3)
    assert (dt.hour, dt.minute, dt.second) == (10, 31, 35)


def test_parse_doubled_separator_format() -> None:
    # Panopto template glitch doubles the underscores.
    dt = parse_filename_datetime("Tuesday__October_10__2023_at_11_01_57_AM.mp4")
    assert dt is not None
    assert (dt.year, dt.month, dt.day) == (2023, 10, 10)
    assert (dt.hour, dt.minute, dt.second) == (11, 1, 57)


def test_parse_abbreviated_format() -> None:
    dt = parse_filename_datetime("Tue_Jan_03_2023_10_56_39_AM.mp4")
    assert dt is not None
    assert (dt.year, dt.month, dt.day) == (2023, 1, 3)
    assert (dt.hour, dt.minute, dt.second) == (10, 56, 39)


def test_pm_hour_conversion() -> None:
    dt = parse_filename_datetime("Monday_April_3_2023_at_01_15_00_PM.mp4")
    assert dt is not None
    assert dt.hour == 13


def test_noon_is_twelve_pm() -> None:
    dt = parse_filename_datetime("Monday_April_3_2023_at_12_00_00_PM.mp4")
    assert dt is not None
    assert dt.hour == 12


def test_midnight_is_twelve_am() -> None:
    dt = parse_filename_datetime("Monday_April_3_2023_at_12_00_00_AM.mp4")
    assert dt is not None
    assert dt.hour == 0


def test_timezone_is_pacific() -> None:
    dt = parse_filename_datetime("Monday_April_3_2023_at_10_31_35_AM.mp4")
    assert dt is not None
    assert dt.tzinfo is not None
    assert "Los_Angeles" in str(dt.tzinfo)


def test_unparseable_filename_returns_none() -> None:
    assert parse_filename_datetime("just-a-lecture.mp4") is None
    assert parse_filename_datetime("") is None


# ---- date_to_term_label ------------------------------------------------------

@pytest.mark.parametrize(
    ("d", "expected"),
    [
        (date(2023, 1, 1), "Winter 2023"),
        (date(2023, 3, 20), "Winter 2023"),   # last day of Winter
        (date(2023, 3, 21), "Spring 2023"),   # first day of Spring
        (date(2023, 6, 15), "Spring 2023"),
        (date(2023, 6, 16), "Summer 2023"),
        (date(2023, 9, 21), "Summer 2023"),
        (date(2023, 9, 22), "Autumn 2023"),   # first day of Autumn
        (date(2023, 12, 31), "Autumn 2023"),
    ],
)
def test_term_label_boundaries(d: date, expected: str) -> None:
    assert date_to_term_label(d) == expected


# ---- build_course_index + pick_course ---------------------------------------

def _entry(code: str, term: str, folder: str = "folder-guid") -> CourseEntry:
    return CourseEntry(
        canvas_id=1, name=code, code=code, term=term,
        panopto_folder=folder, out_dir=None,
    )


def _event(number: str, section: str | None, summary: str = "lecture") -> ClassEvent:
    from datetime import datetime

    now = datetime(2023, 4, 3, 10, 0, 0)
    return ClassEvent(
        start=now, end=now, summary=summary,
        code_number=number, section=section,
    )


def test_exact_section_match() -> None:
    by_key, by_num_term = build_course_index([_entry("CSS 343 B", "Spring 2023")])
    course, reason = pick_course(_event("343", "B"), "Spring 2023", by_key, by_num_term)
    assert reason == "exact"
    assert course is not None and course.code == "CSS 343 B"


def test_unique_section_when_calendar_omits_letter() -> None:
    by_key, by_num_term = build_course_index([_entry("CSS 343 A", "Spring 2023")])
    course, reason = pick_course(_event("343", None), "Spring 2023", by_key, by_num_term)
    assert reason == "unique-section"
    assert course is not None


def test_ambiguous_when_multiple_sections_and_no_letter() -> None:
    by_key, by_num_term = build_course_index(
        [_entry("CSS 343 A", "Spring 2023"), _entry("CSS 343 B", "Spring 2023")]
    )
    course, reason = pick_course(_event("343", None), "Spring 2023", by_key, by_num_term)
    assert reason == "ambiguous"
    assert course is None


def test_section_not_in_yaml() -> None:
    by_key, by_num_term = build_course_index([_entry("CSS 343 A", "Spring 2023")])
    course, reason = pick_course(_event("343", "B"), "Spring 2023", by_key, by_num_term)
    assert reason == "section-not-in-yaml"
    assert course is None


def test_no_course_for_unknown_number() -> None:
    by_key, by_num_term = build_course_index([_entry("CSS 343 A", "Spring 2023")])
    course, reason = pick_course(_event("999", None), "Spring 2023", by_key, by_num_term)
    assert reason == "no-course"
    assert course is None


def test_entries_without_folder_are_excluded_from_index() -> None:
    # An entry with no panopto_folder can't receive transcripts and must not
    # inflate the section count for the ambiguity check.
    by_key, by_num_term = build_course_index(
        [_entry("CSS 343 A", "Spring 2023"), _entry("CSS 343 B", "Spring 2023", folder="")]
    )
    course, reason = pick_course(_event("343", None), "Spring 2023", by_key, by_num_term)
    assert reason == "unique-section"
    assert course is not None and course.code == "CSS 343 A"
