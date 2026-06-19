"""Tester för källbläddrarens rena arkivlogik."""

from __future__ import annotations

from pathlib import Path

import archive_browser


def _write(path: Path, text: str = "text") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_parse_document_source_extracts_metadata_and_prefers_ocr_pdf(tmp_path: Path) -> None:
    root = tmp_path
    text_path = _write(root / "generated" / "text" / "123 — Vittnesförhör.txt")
    _write(root / "downloaded" / "files" / "123 — Vittnesförhör.pdf", "%PDF")
    ocr_pdf = _write(root / "generated" / "ocr" / "123 — Vittnesförhör.pdf", "%PDF")

    record = archive_browser.parse_document_source(text_path, root)

    assert record.source == "123 — Vittnesförhör.txt"
    assert record.stem == "123 — Vittnesförhör"
    assert record.nr == "123"
    assert record.title == "Vittnesförhör"
    assert record.text_path == text_path
    assert record.pdf_path == ocr_pdf
    assert record.source_kind == "palme"


def test_parse_document_source_marks_wpu_pdf_source(tmp_path: Path) -> None:
    root = tmp_path
    text_path = _write(root / "generated" / "text" / "wpu-abc Rapport.txt")
    wpu_pdf = _write(root / "downloaded" / "wpu_files" / "wpu-abc Rapport.pdf", "%PDF")

    record = archive_browser.parse_document_source(text_path, root)

    assert record.nr is None
    assert record.title == "wpu-abc Rapport"
    assert record.pdf_path == wpu_pdf
    assert record.source_kind == "wpu"


def test_iter_documents_sorts_by_number_then_title(tmp_path: Path) -> None:
    root = tmp_path
    _write(root / "generated" / "text" / "10 — Zeta.txt")
    _write(root / "generated" / "text" / "2 — Alfa.txt")
    _write(root / "generated" / "text" / "2 — Beta.txt")
    _write(root / "generated" / "text" / "Bilaga utan nummer.txt")

    records = archive_browser.iter_documents(root)

    assert [record.source for record in records] == [
        "2 — Alfa.txt",
        "2 — Beta.txt",
        "10 — Zeta.txt",
        "Bilaga utan nummer.txt",
    ]


def test_filter_documents_matches_nr_title_and_source_case_insensitively(tmp_path: Path) -> None:
    root = tmp_path
    _write(root / "generated" / "text" / "12 — Tunnelgatan.txt")
    _write(root / "generated" / "text" / "33 — Skandia PM.txt")
    records = archive_browser.iter_documents(root)

    assert [record.source for record in archive_browser.filter_documents(records, "12")] == [
        "12 — Tunnelgatan.txt",
    ]
    assert [record.source for record in archive_browser.filter_documents(records, "skandia")] == [
        "33 — Skandia PM.txt",
    ]
    assert [record.source for record in archive_browser.filter_documents(records, "TUNNELGATAN.TXT")] == [
        "12 — Tunnelgatan.txt",
    ]
    assert archive_browser.filter_documents(records, "saknas") == []


def test_read_preview_collapses_whitespace_and_truncates_on_word_boundary(tmp_path: Path) -> None:
    text_path = _write(
        tmp_path / "generated" / "text" / "1 — Preview.txt",
        "Första\n\nraden\tmed   extra mellanrum. Andra meningen fortsätter länge.",
    )

    assert archive_browser.read_preview(text_path, max_chars=36) == (
        "Första raden med extra mellanrum..."
    )
