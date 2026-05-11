"""Tester för merge_text och merge_one från merge_pages.py."""

from __future__ import annotations

import json
from pathlib import Path

from merge_pages import merge_one, merge_text


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


def test_page_beyond_range_pads_with_empty_pages() -> None:
    # Surya OCR:ar sidor utifrån PDF:ens verkliga sidnumrering. Om text/-filen
    # saknar sidor (ocrmypdf/pdftotext missade dem) ska merge_text expandera
    # originalet med tomma sidor så Surya-texten hamnar rätt — annars tappar
    # vi data tyst.
    assert merge_text("A.\fB.", {5: "X"}) == "A.\fB.\f\f\fX"


def test_page_beyond_range_multiple() -> None:
    assert merge_text("A.\fB.", {3: "C", 5: "E"}) == "A.\fB.\fC\f\fE"


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


# --- merge_one (med cleanup) ---


def _setup(tmp_path: Path, original: str = "A.\fB.\fC.") -> tuple[Path, Path, Path]:
    txt_dir = tmp_path / "text"
    pages_dir = tmp_path / "text_pages"
    txt_dir.mkdir()
    pages_dir.mkdir()
    (txt_dir / "doc.txt").write_text(original, encoding="utf-8")
    stem_dir = pages_dir / "doc"
    stem_dir.mkdir()
    return txt_dir, pages_dir, stem_dir


def test_merge_one_writes_merged_text(tmp_path: Path) -> None:
    txt_dir, pages_dir, stem_dir = _setup(tmp_path)
    (stem_dir / "page-002.txt").write_text("NY", encoding="utf-8")
    (stem_dir / "page-002.json").write_text('{"score": 90}', encoding="utf-8")

    assert merge_one("doc", txt_dir, pages_dir) is True
    assert (txt_dir / "doc.txt").read_text(encoding="utf-8") == "A.\fNY\fC."


def test_merge_one_removes_page_txt_but_keeps_json(tmp_path: Path) -> None:
    # .json är idempotens-markör för ocr_pages.py och ska INTE raderas.
    txt_dir, pages_dir, stem_dir = _setup(tmp_path)
    (stem_dir / "page-002.txt").write_text("NY", encoding="utf-8")
    (stem_dir / "page-002.json").write_text('{"score": 90}', encoding="utf-8")

    merge_one("doc", txt_dir, pages_dir)

    assert not (stem_dir / "page-002.txt").exists()
    assert (stem_dir / "page-002.json").exists()


def test_merge_one_removes_page_png(tmp_path: Path) -> None:
    txt_dir, pages_dir, stem_dir = _setup(tmp_path)
    (stem_dir / "page-002.txt").write_text("NY", encoding="utf-8")
    (stem_dir / "page-002.png").write_bytes(b"\x89PNG\r\n")

    merge_one("doc", txt_dir, pages_dir)

    assert not (stem_dir / "page-002.png").exists()


def test_merge_one_removes_combined_txt(tmp_path: Path) -> None:
    # Legacy: ocr_pages.py kunde tidigare skapa text_pages/<stem>.txt.
    txt_dir, pages_dir, stem_dir = _setup(tmp_path)
    (stem_dir / "page-002.txt").write_text("NY", encoding="utf-8")
    combined = pages_dir / "doc.txt"
    combined.write_text("ofullständig combined", encoding="utf-8")

    merge_one("doc", txt_dir, pages_dir)

    assert not combined.exists()


def test_merge_one_pads_for_pages_beyond_original(tmp_path: Path) -> None:
    # Originalet har 3 sidor men text_pages har page-005 — merge_one ska
    # expandera text/ med tomma sidor så Surya-texten hamnar rätt.
    txt_dir, pages_dir, stem_dir = _setup(tmp_path)
    (stem_dir / "page-002.txt").write_text("NY", encoding="utf-8")
    (stem_dir / "page-005.txt").write_text("UTANFÖR", encoding="utf-8")

    merge_one("doc", txt_dir, pages_dir)

    assert (txt_dir / "doc.txt").read_text(encoding="utf-8") == "A.\fNY\fC.\f\fUTANFÖR"
    assert not (stem_dir / "page-002.txt").exists()
    assert not (stem_dir / "page-005.txt").exists()


def test_merge_one_idempotent(tmp_path: Path) -> None:
    # Andra körningen ska inte göra något (alla per-sida-txt är redan borta).
    txt_dir, pages_dir, stem_dir = _setup(tmp_path)
    (stem_dir / "page-002.txt").write_text("NY", encoding="utf-8")
    (stem_dir / "page-002.json").write_text('{"score": 90}', encoding="utf-8")

    assert merge_one("doc", txt_dir, pages_dir) is True
    text_after_first = (txt_dir / "doc.txt").read_text(encoding="utf-8")

    assert merge_one("doc", txt_dir, pages_dir) is False
    assert (txt_dir / "doc.txt").read_text(encoding="utf-8") == text_after_first


def test_merge_one_no_updates_returns_false(tmp_path: Path) -> None:
    # Mappen finns men inga page-NNN.txt → inget att göra.
    txt_dir, pages_dir, _ = _setup(tmp_path)
    assert merge_one("doc", txt_dir, pages_dir) is False


def test_merge_one_cleans_up_when_text_already_merged(tmp_path: Path) -> None:
    # Vanligt vid retroaktiv städning: texten i text/ är redan korrekt
    # (mergad i en tidigare körning) men page-NNN.txt ligger kvar.
    # merge_one ska radera artefakterna ändå.
    txt_dir, pages_dir, stem_dir = _setup(tmp_path, original="A.\fNY\fC.")
    (stem_dir / "page-002.txt").write_text("NY", encoding="utf-8")
    (stem_dir / "page-002.png").write_bytes(b"\x89PNG\r\n")

    assert merge_one("doc", txt_dir, pages_dir) is True
    assert (txt_dir / "doc.txt").read_text(encoding="utf-8") == "A.\fNY\fC."
    assert not (stem_dir / "page-002.txt").exists()
    assert not (stem_dir / "page-002.png").exists()
