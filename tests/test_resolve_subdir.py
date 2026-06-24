"""Regression tests for the path-traversal guard in `cli._resolve_subdir`.

This is the function that turns a user-supplied `--out-dir` into a directory
under TRANSCRIPT_DIR. Because transcripts are written there, a guard that let
`..` or an absolute path through would let a caller write outside the intended
tree, so these are the tests that matter most.
"""
from __future__ import annotations

from pathlib import Path

import click
import pytest

from panopto_transcriber.cli import _resolve_subdir


def test_none_returns_base_unchanged(tmp_path: Path) -> None:
    assert _resolve_subdir(tmp_path, None) == tmp_path


def test_empty_string_returns_base(tmp_path: Path) -> None:
    assert _resolve_subdir(tmp_path, "") == tmp_path


def test_simple_relative_subdir_is_created(tmp_path: Path) -> None:
    out = _resolve_subdir(tmp_path, "cse143")
    assert out == tmp_path / "cse143"
    assert out.is_dir()


def test_nested_relative_subdir_is_created(tmp_path: Path) -> None:
    out = _resolve_subdir(tmp_path, "fall/cse143")
    assert out == tmp_path / "fall" / "cse143"
    assert out.is_dir()


def test_absolute_path_rejected(tmp_path: Path) -> None:
    with pytest.raises(click.BadParameter):
        _resolve_subdir(tmp_path, "/etc")


@pytest.mark.parametrize(
    "evil",
    [
        "..",
        "../sibling",
        "a/../../escape",
        "../../etc/passwd",
        "sub/../../..",
    ],
)
def test_dotdot_segments_rejected(tmp_path: Path, evil: str) -> None:
    with pytest.raises(click.BadParameter):
        _resolve_subdir(tmp_path, evil)


def test_rejected_paths_are_not_created(tmp_path: Path) -> None:
    with pytest.raises(click.BadParameter):
        _resolve_subdir(tmp_path, "../escape")
    # The guard must reject *before* creating anything.
    assert not (tmp_path.parent / "escape").exists()


def test_symlink_escape_is_blocked(tmp_path: Path) -> None:
    """A pre-existing symlink inside base must not become an escape hatch.

    `_resolve_subdir` re-validates with `resolve()`, so a subdir name that is
    actually a symlink pointing outside base resolves outside and is rejected.
    """
    base = tmp_path / "base"
    base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = base / "link"
    link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(click.BadParameter):
        _resolve_subdir(base, "link/sneaky")
