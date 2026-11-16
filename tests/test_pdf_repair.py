"""Tester för reservvägen via en qpdf-lagad kopia (``pdf_repair``)."""

from __future__ import annotations

import shutil
import sys
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

pdfium = pytest.importorskip("pypdfium2")

needs_qpdf = pytest.mark.skipif(shutil.which("qpdf") is None, reason="qpdf saknas")


def _row(kind: int, offset: int, gen: int) -> bytes:
    return bytes([kind]) + offset.to_bytes(3, "big") + bytes([gen])


def _xref(out: bytearray, num: int, rows: list[bytes], size: int, extra: str = "") -> int:
    data = zlib.compress(b"".join(rows))
    start = len(out)
    out.extend(f"{num} 0 obj\n<</Type/XRef/Size {size}/W[1 3 1]/Root 1 0 R{extra}"
               f"/Filter/FlateDecode/Length {len(data)}>>\nstream\n".encode()
               + data + b"\nendstream\nendobj\n")
    out.extend(f"startxref\n{start}\n%%EOF\n".encode())
    return start


def broken_pdf(path: Path) -> Path:
    """Två sidor där sida 2 inte går att ladda i PDFium.

    Samma mönster som Pol-1986-03-05_E167-00-C_Christina_Wallin.pdf: sidans
    objekt är fritt i den första xref-strömmen och återanvänds (generation 1) i
    en stegvis uppdatering. qpdf och poppler läser den; PDFium ger
    ``Failed to load page``.
    """
    out = bytearray(b"%PDF-1.7\n")
    offsets: dict[int, int] = {}

    def obj(num: int, gen: int, body: bytes) -> None:
        offsets[num] = len(out)
        out.extend(f"{num} {gen} obj\n".encode() + body + b"\nendobj\n")

    obj(1, 0, b"<</Type/Catalog/Pages 2 0 R>>")
    obj(2, 0, b"<</Type/Pages/Count 1/Kids[3 0 R]>>")
    obj(3, 0, b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 300]>>")
    first = _xref(out, 5, [_row(0, 0, 255), _row(1, offsets[1], 0), _row(1, offsets[2], 0),
                           _row(1, offsets[3], 0), _row(0, 0, 0), _row(1, len(out), 0)], 6)
    obj(2, 0, b"<</Type/Pages/Count 2/Kids[3 0 R 4 1 R]>>")
    obj(4, 1, b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 300]>>")
    _xref(out, 6, [_row(1, offsets[2], 0), _row(1, offsets[4], 1), _row(1, len(out), 0)], 7,
          f"/Index[2 1 4 1 6 1]/Prev {first}")
    path.write_bytes(bytes(out))
    return path


def test_fixture_reproduces_the_pdfium_failure(tmp_path) -> None:
    doc = pdfium.PdfDocument(str(broken_pdf(tmp_path / "trasig.pdf")))
    try:
        assert len(doc) == 2
        doc[0].close()
        with pytest.raises(pdfium.PdfiumError):
            doc[1]
    finally:
        doc.close()


@needs_qpdf
def test_broken_page_loads_from_repaired_copy(tmp_path) -> None:
    from pdf_repair import open_pdf

    notes: list[str] = []
    with open_pdf(broken_pdf(tmp_path / "trasig.pdf"), on_repair=notes.append) as pdf:
        assert len(pdf) == 2
        assert pdf.page(1).get_size() == (200, 300)
        assert pdf.page(2).get_size() == (200, 300)
        pdf.page(2)
    assert len(notes) == 1 and "trasig.pdf" in notes[0]


def test_good_pdf_never_runs_qpdf(tmp_path, monkeypatch) -> None:
    import pdf_repair

    path = tmp_path / "hel.pdf"
    doc = pdfium.PdfDocument.new()
    doc.new_page(200, 300)
    doc.save(str(path))
    doc.close()
    monkeypatch.setattr(pdf_repair, "repair_copy", lambda src, dst: pytest.fail("qpdf körs"))

    with pdf_repair.open_pdf(path) as pdf:
        pdf.page(1)


def test_without_qpdf_the_page_still_fails(tmp_path, monkeypatch) -> None:
    import pdf_repair

    monkeypatch.setattr(pdf_repair.shutil, "which", lambda name: None)
    notes: list[str] = []
    with pdf_repair.open_pdf(broken_pdf(tmp_path / "trasig.pdf"), on_repair=notes.append) as pdf:
        pdf.page(1)
        with pytest.raises(pdfium.PdfiumError):
            pdf.page(2)
    assert notes and "qpdf saknas" in notes[0]


@needs_qpdf
def test_unopenable_document_is_read_from_repaired_copy(tmp_path) -> None:
    """Ett dokument PDFium inte kan öppna alls (avklippt trailer) läses ur kopian."""
    from pdf_repair import open_pdf

    path = tmp_path / "avklippt.pdf"
    doc = pdfium.PdfDocument.new()
    doc.new_page(200, 300)
    doc.new_page(200, 300)
    doc.save(str(path))
    doc.close()
    path.write_bytes(path.read_bytes()[:-40])  # startxref och %%EOF försvinner
    with pytest.raises(pdfium.PdfiumError):
        pdfium.PdfDocument(str(path))

    notes: list[str] = []
    with open_pdf(path, on_repair=notes.append) as pdf:
        assert len(pdf) == 2
        assert pdf.page(2).get_size() == (200, 300)
    assert len(notes) == 1 and "går inte att öppna" in notes[0]


def test_unopenable_file_raises_after_failed_repair(tmp_path) -> None:
    from pdf_repair import open_pdf

    path = tmp_path / "html.pdf"
    path.write_text("<html>inte en pdf</html>", encoding="utf-8")
    with pytest.raises(pdfium.PdfiumError), open_pdf(path):
        pass


@needs_qpdf
def test_render_pages_yields_the_broken_page(tmp_path) -> None:
    import ocr_pages

    pages = [n for n, _ in ocr_pages.render_pages(broken_pdf(tmp_path / "t.pdf"), 72)]
    assert pages == [1, 2]
    assert ocr_pages.render_page(tmp_path / "t.pdf", 2, 72).size == (200, 300)


@needs_qpdf
def test_page_review_uses_the_repaired_copy(tmp_path) -> None:
    from page_review import page_count, render_page_image, unloadable_pages

    path = broken_pdf(tmp_path / "t.pdf")
    assert page_count(path) == 2
    assert render_page_image(path, 2, dpi=72).size == (200, 300)
    assert unloadable_pages(path, [1, 2]) == set()


def test_unloadable_pages_without_qpdf(tmp_path, monkeypatch) -> None:
    import pdf_repair
    from page_review import unloadable_pages

    monkeypatch.setattr(pdf_repair.shutil, "which", lambda name: None)
    assert unloadable_pages(broken_pdf(tmp_path / "t.pdf"), [1, 2, 3]) == {2, 3}
    garbage = tmp_path / "skräp.pdf"
    garbage.write_bytes(b"%PDF trasig")
    assert unloadable_pages(garbage, [1, 2]) == {1, 2}
