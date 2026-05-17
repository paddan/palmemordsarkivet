"""Tester för merge_text och merge_one från merge_pages.py."""

from __future__ import annotations

from pathlib import Path

import db as state_db
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


# --- merge_one (db-baserad) ---


def _setup(
    tmp_path: Path, monkeypatch, original: str = "A.\fB.\fC.",
    stem: str = "doc",
) -> Path:
    """Sätt upp tmp text-katalog och tom STATE_DB. Returnerar txt_dir."""
    txt_dir = tmp_path / "text"
    txt_dir.mkdir()
    (txt_dir / f"{stem}.txt").write_text(original, encoding="utf-8")

    db_path = tmp_path / "state.db"
    monkeypatch.setenv("STATE_DB", str(db_path))
    conn = state_db.connect(db_path)
    state_db.init_schema(conn)
    state_db.upsert_pdf_file(
        conn, pdf_stem=stem, source="files",
        pdf_path=f"downloaded/files/{stem}.pdf",
    )
    conn.close()
    return txt_dir


def _add_page(stem: str, page_num: int, text: str) -> None:
    conn = state_db.connect()
    state_db.init_schema(conn)
    state_db.record_page(
        conn, pdf_stem=stem, page_num=page_num,
        engine="tesseract", text=text, score=90.0,
    )
    conn.close()


def test_merge_one_writes_merged_text(tmp_path: Path, monkeypatch) -> None:
    txt_dir = _setup(tmp_path, monkeypatch)
    _add_page("doc", 2, "NY")

    assert merge_one("doc", txt_dir) is True
    assert (txt_dir / "doc.txt").read_text(encoding="utf-8") == "A.\fNY\fC."


def test_merge_one_marks_merged_at(tmp_path: Path, monkeypatch) -> None:
    txt_dir = _setup(tmp_path, monkeypatch)
    _add_page("doc", 2, "NY")

    assert merge_one("doc", txt_dir) is True

    conn = state_db.connect()
    row = state_db.get_pdf_file(conn, "doc")
    conn.close()
    assert row is not None
    assert row["merged_at"] is not None
    assert row["text_mtime"] is not None
    assert row["text_mtime"] == (txt_dir / "doc.txt").stat().st_mtime


def test_merge_one_pads_for_pages_beyond_original(
    tmp_path: Path, monkeypatch,
) -> None:
    # Originalet har 3 sidor men pdf_pages har en sida 5 — merge_one ska
    # expandera text/ med tomma sidor så texten hamnar rätt.
    txt_dir = _setup(tmp_path, monkeypatch)
    _add_page("doc", 2, "NY")
    _add_page("doc", 5, "UTANFÖR")

    assert merge_one("doc", txt_dir) is True
    assert (txt_dir / "doc.txt").read_text(encoding="utf-8") == "A.\fNY\fC.\f\fUTANFÖR"


def test_merge_one_idempotent(tmp_path: Path, monkeypatch) -> None:
    # Andra körningen ger samma resultat → text_changed=False → returnerar False.
    txt_dir = _setup(tmp_path, monkeypatch)
    _add_page("doc", 2, "NY")

    assert merge_one("doc", txt_dir) is True
    text_after_first = (txt_dir / "doc.txt").read_text(encoding="utf-8")

    assert merge_one("doc", txt_dir) is False
    assert (txt_dir / "doc.txt").read_text(encoding="utf-8") == text_after_first


def test_merge_one_no_pages_returns_false(tmp_path: Path, monkeypatch) -> None:
    # Inga rader i pdf_pages → inget att göra.
    txt_dir = _setup(tmp_path, monkeypatch)
    assert merge_one("doc", txt_dir) is False


def test_merge_one_missing_txt_returns_false(
    tmp_path: Path, monkeypatch,
) -> None:
    # Saknad text/<stem>.txt — kan inte mergas mot något.
    txt_dir = tmp_path / "text"
    txt_dir.mkdir()
    db_path = tmp_path / "state.db"
    monkeypatch.setenv("STATE_DB", str(db_path))
    conn = state_db.connect(db_path)
    state_db.init_schema(conn)
    conn.close()
    _add_page("doc", 1, "NY")

    assert merge_one("doc", txt_dir) is False
