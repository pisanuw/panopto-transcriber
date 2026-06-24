"""Tests for the pure URL/GUID helpers in `downloader`."""
from __future__ import annotations

import pytest

from panopto_transcriber.downloader import _folder_url, _is_guid, _viewer_url

HOST = "uw.hosted.panopto.com"
GUID = "12345678-abcd-1234-abcd-1234567890ab"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (GUID, True),
        (GUID.upper(), True),
        ("not-a-guid", False),
        ("1234", False),
        ("", False),
        # right shape, non-hex characters
        ("zzzzzzzz-abcd-1234-abcd-1234567890ab", False),
    ],
)
def test_is_guid(value: str, expected: bool) -> None:
    assert _is_guid(value) is expected


def test_viewer_url_from_guid() -> None:
    assert _viewer_url(HOST, GUID) == (
        f"https://{HOST}/Panopto/Pages/Viewer.aspx?id={GUID}"
    )


def test_viewer_url_passthrough_for_full_url() -> None:
    url = f"https://{HOST}/Panopto/Pages/Viewer.aspx?id={GUID}"
    assert _viewer_url(HOST, url) == url


def test_folder_url_from_guid() -> None:
    assert _folder_url(HOST, GUID) == (
        f"https://{HOST}/Panopto/Pages/Sessions/List.aspx?folderID={GUID}"
    )


def test_folder_url_passthrough_for_full_url() -> None:
    url = f"https://{HOST}/Panopto/Pages/Sessions/List.aspx?folderID={GUID}"
    assert _folder_url(HOST, url) == url
