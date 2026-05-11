"""Tester för merge_text från merge_pages.py."""

from __future__ import annotations

from merge_pages import merge_text


def test_no_updates_returns_original() -> None:
    assert merge_text("Sida 1.\fSida 2.", {}) == "Sida 1.\fSida 2."


def test_replace_middle_page() -> None:
    assert merge_text("A.\fB.\fC.", {2: "NY"}) == "A.\fNY\fC."


def test_replace_first_page() -> None:
    assert merge_text("A.\fB.", {1: "NY"}) == "NY\fB."


def test_replace_last_page() -> None:
    assert merge_text("A.\fB.\fC.", {3: "NY"}) == "A.\fB.\fNY"


def test_replace_multiple_pages() -> None:
    assert merge_text("A.\fB.\fC.", {1: "X", 3: "Z"}) == "X\fB.\fZ"


def test_page_out_of_range_ignored() -> None:
    # Sidan 5 finns inte i ett 2-sidors dokument — ignoreras tyst, original behålls.
    assert merge_text("A.\fB.", {5: "X"}) == "A.\fB."


def test_zero_and_negative_page_ignored() -> None:
    assert merge_text("A.\fB.", {0: "X", -1: "Y"}) == "A.\fB."


def test_single_page_original() -> None:
    assert merge_text("Endast en sida.", {1: "NY"}) == "NY"


def test_empty_original_returns_empty() -> None:
    # Saknad originaltext — ingen sidstruktur att slå ihop mot.
    assert merge_text("", {1: "NY"}) == ""


def test_empty_pages_preserved() -> None:
    # Sida 2 är tom i originalet (\f\f) — ska bevaras om den inte uppdateras.
    assert merge_text("A.\f\fC.", {1: "X"}) == "X\f\fC."
