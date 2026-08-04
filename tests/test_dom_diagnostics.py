"""Tests for the DOM-capture failure diagnosis.

The original message blamed the page's coordinate-extraction script for every
failure. In the most common case that is wrong: Chromium never loaded the page
and returned its own error page instead, so the script never ran. Sending
someone to debug the script wastes the time the message was meant to save.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.argv = ["pytest"]

import narraoke as h  # noqa: E402

CHROMIUM = "/usr/bin/chromium-browser"


def test_unreadable_file_names_the_real_cause(tmp_path: Path) -> None:
    """The file exists, so this is an access problem, not a missing file.

    Snap-confined Chromium cannot read outside $HOME, which is what actually
    happens when rendering from /tmp on a snap-based install.
    """
    page = tmp_path / "page.html"
    page.write_text("<html></html>", encoding="utf-8")
    message = h._diagnose_dom_failure(
        "<html><title>ERR_FILE_NOT_FOUND</title></html>", page, CHROMIUM
    )
    assert "ERR_FILE_NOT_FOUND" in message
    assert "could not *read* it" in message
    assert "snap" in message
    assert CHROMIUM in message
    # And it must lead with the cheap workaround
    assert "--output-dir" in message
    assert "NARRAOKE_CHROMIUM" in message


def test_a_genuinely_missing_file_is_called_a_bug(tmp_path: Path) -> None:
    """narraoke wrote that path moments earlier; its absence is our fault."""
    message = h._diagnose_dom_failure(
        "ERR_FILE_NOT_FOUND", tmp_path / "never-written.html", CHROMIUM
    )
    assert "missing" in message
    assert "narraoke bug" in message


def test_document_error_wins_over_incidental_ones(tmp_path: Path) -> None:
    """A failed font fetch reports ERR_INTERNET_DISCONNECTED on a page that
    otherwise loaded. The code describing the *document* must win.

    This is a real case: the page template links Google Fonts, so an offline
    render mentions both codes and the first regex match was the wrong one.
    """
    page = tmp_path / "page.html"
    page.write_text("<html></html>", encoding="utf-8")
    dom = (
        "<html>ERR_INTERNET_DISCONNECTED for fonts.googleapis.com "
        "... ERR_FILE_NOT_FOUND</html>"
    )
    message = h._diagnose_dom_failure(dom, page, CHROMIUM)
    assert "ERR_FILE_NOT_FOUND" in message
    assert "ERR_INTERNET_DISCONNECTED" not in message


def test_other_error_codes_are_reported_plainly(tmp_path: Path) -> None:
    page = tmp_path / "page.html"
    page.write_text("<html></html>", encoding="utf-8")
    message = h._diagnose_dom_failure("ERR_CONNECTION_REFUSED", page, CHROMIUM)
    assert "ERR_CONNECTION_REFUSED" in message
    assert CHROMIUM in message


def test_a_loaded_page_still_blames_the_script(tmp_path: Path) -> None:
    """With no error code the page did load, so the script is the suspect.

    The original message was right in this case — it was just applied to
    every case.
    """
    page = tmp_path / "page.html"
    page.write_text("<html></html>", encoding="utf-8")
    message = h._diagnose_dom_failure(
        "<html><body>a normal page</body></html>", page, CHROMIUM
    )
    assert "coordinate-extraction script" in message
    assert "page.html" in message, "should point at the dump for inspection"
