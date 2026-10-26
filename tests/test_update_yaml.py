"""Regression tests for `discover.update_yaml_in_place`.

`discover-folders` edits courses.yml in place to fill in folder GUIDs. The
contract: only blank `panopto_folder: ""` lines for known canvas_ids get
filled, comments and key order are preserved, and re-running is a no-op (the
README promises "Re-running is safe").
"""
from __future__ import annotations

from pathlib import Path

from panopto_transcriber.discover import update_yaml_in_place

SAMPLE = """\
courses:
  - canvas_id: 1234567
    name: "CSE 143 — Computer Programming II"
    code: "CSE 143"
    term: "Spring 2026"
    panopto_folder: ""  # TODO  <- paste the folder GUID here
  - canvas_id: 7654321
    name: "CSS 343"
    code: "CSS 343"
    term: "Spring 2026"
    panopto_folder: ""
"""

GUID = "f77faf32-1111-2222-3333-b01f01628e04"


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "courses.yml"
    p.write_text(text)
    return p


def test_fills_blank_folder_for_matching_id(tmp_path: Path) -> None:
    p = _write(tmp_path, SAMPLE)
    modified = update_yaml_in_place(p, {1234567: GUID})
    assert modified == 1
    out = p.read_text()
    assert f'panopto_folder: "{GUID}"' in out


def test_preserves_inline_comment(tmp_path: Path) -> None:
    p = _write(tmp_path, SAMPLE)
    update_yaml_in_place(p, {1234567: GUID})
    out = p.read_text()
    assert "# TODO  <- paste the folder GUID here" in out


def test_other_entries_untouched(tmp_path: Path) -> None:
    p = _write(tmp_path, SAMPLE)
    update_yaml_in_place(p, {1234567: GUID})
    out = p.read_text()
    # The second course had no update, so its folder stays blank.
    assert out.count('panopto_folder: ""') == 1


def test_rerun_is_idempotent(tmp_path: Path) -> None:
    p = _write(tmp_path, SAMPLE)
    update_yaml_in_place(p, {1234567: GUID})
    first = p.read_text()
    # A filled folder is no longer blank, so re-running changes nothing.
    second_modified = update_yaml_in_place(p, {1234567: GUID})
    assert second_modified == 0
    assert p.read_text() == first


def test_multiple_updates_in_one_pass(tmp_path: Path) -> None:
    p = _write(tmp_path, SAMPLE)
    other = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    modified = update_yaml_in_place(p, {1234567: GUID, 7654321: other})
    assert modified == 2
    out = p.read_text()
    assert f'panopto_folder: "{GUID}"' in out
    assert f'panopto_folder: "{other}"' in out
    assert 'panopto_folder: ""' not in out


def test_unknown_id_does_nothing(tmp_path: Path) -> None:
    p = _write(tmp_path, SAMPLE)
    modified = update_yaml_in_place(p, {9999999: GUID})
    assert modified == 0
    assert p.read_text() == SAMPLE
