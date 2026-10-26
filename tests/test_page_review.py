"""Sidvy-logik för sidgranskningen (Streamlit-fri)."""
from __future__ import annotations

import pytest
from PIL import Image

import page_review
from db import archive_page_reading, connect, init_schema, record_page
from normalize_text import markdown_is_fresh
from surya_layout import BlockKind, LayoutBlock, blocks_to_json


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "state.db")
    init_schema(c)
    yield c
    c.close()


def _write_text(root, stem, pages):
    d = root / "generated" / "text"
    d.mkdir(parents=True)
    (d / f"{stem}.txt").write_text("\f".join(pages), encoding="utf-8")


def test_markdown_is_fresh():
    assert markdown_is_fresh("o\ufb03ce\r\nrad", "office\nrad") is True
    assert markdown_is_fresh(None, "x") is False
    assert markdown_is_fresh("abc", "abc") is True
    assert markdown_is_fresh("abc", "annan") is False


def test_load_page_view_without_pdf_pages_row(conn, tmp_path):
    _write_text(tmp_path, "d", ["sida ett", "sida två"])
    view = page_review.load_page_view(conn, tmp_path, "d", 2)
    assert view.engine == "hel-fils-OCR"
    assert view.current_text == "sida två"
    assert view.markdown is None and view.blocks == [] and view.stale is False
    assert view.readings == []
    assert view.page_count == 2 and view.pdf_available is False


def test_load_page_view_with_layout_row(conn, tmp_path):
    _write_text(tmp_path, "d", ["Hej"])
    block = LayoutBlock(kind=BlockKind.PARAGRAPH, text="Hej", order=0, bbox=(1, 2, 3, 4))
    record_page(
        conn, pdf_stem="d", page_num=1, engine="surya-layout", text="Hej", score=91.0,
        markdown="Hej", layout_json=blocks_to_json([block]), engine_identity="id-1",
    )
    archive_page_reading(
        conn, pdf_stem="d", page_num=1, engine="tesseract", text="Hrj", markdown=None,
        layout_json=None, engine_identity=None, score=40.0, reason="rescan",
    )
    view = page_review.load_page_view(conn, tmp_path, "d", 1)
    assert view.engine == "surya-layout" and view.score == 91.0
    assert view.engine_identity == "id-1" and view.markdown == "Hej"
    assert [b.text for b in view.blocks] == ["Hej"]
    assert view.processed_at
    assert view.stale is False
    assert isinstance(view.readings[0], dict) and view.readings[0]["text"] == "Hrj"


def test_stale_when_file_text_differs(conn, tmp_path):
    _write_text(tmp_path, "d", ["Ändrad text"])
    record_page(
        conn, pdf_stem="d", page_num=1, engine="surya-layout", text="Original",
        score=90.0, markdown="Original",
    )
    assert page_review.load_page_view(conn, tmp_path, "d", 1).stale is True


def test_page_count_and_pdf_lookup(conn, tmp_path):
    pdf = tmp_path / "downloaded" / "wpu_files" / "d.pdf"
    pdf.parent.mkdir(parents=True)
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument.new()
    doc.new_page(200, 200)
    doc.new_page(200, 200)
    doc.save(str(pdf))
    doc.close()
    assert page_review.page_count(pdf) == 2
    assert page_review.find_pdf(tmp_path, "d") == pdf
    assert page_review.find_pdf(tmp_path, "saknas") is None
    assert page_review.load_page_view(conn, tmp_path, "d", 1).page_count == 2
    img = page_review.render_page_image(pdf, 2, dpi=72)
    assert img.size == (200, 200)


def test_overlay_scales_bbox():
    img = Image.new("RGB", (300, 300), "white")
    block = LayoutBlock(kind=BlockKind.PARAGRAPH, text="x", order=0, bbox=(100, 100, 200, 200))
    out = page_review.draw_layout_overlay(img, [block], ocr_dpi=300, view_dpi=150)
    assert out is not img
    assert img.getpixel((50, 50)) == (255, 255, 255)  # indata orörd
    assert out.getpixel((50, 50)) != (255, 255, 255)  # ramens hörn
    assert out.getpixel((75, 75)) == (255, 255, 255)  # ingen fyllning
    assert out.getpixel((150, 150)) == (255, 255, 255)


@pytest.mark.parametrize("stored,expected", [("new", 150), ("old", 300)])
def test_view_exposes_layout_dpi_and_overlay_scales_from_it(conn, tmp_path, stored, expected):
    # beslut BB: ramarna skalas från layoutens lagrade dpi (gamla listformatet = 300)
    from surya_layout import layout_to_json

    _write_text(tmp_path, "d", ["Hej"])
    block = LayoutBlock(kind=BlockKind.PARAGRAPH, text="Hej", order=0, bbox=(60, 60, 120, 120))
    layout = layout_to_json([block], 150) if stored == "new" else blocks_to_json([block])
    record_page(conn, pdf_stem="d", page_num=1, engine="surya-layout", text="Hej",
                score=1.0, markdown="Hej", layout_json=layout)
    view = page_review.load_page_view(conn, tmp_path, "d", 1)
    assert view.ocr_dpi == expected and view.blocks == [block]
    out = page_review.draw_layout_overlay(Image.new("RGB", (200, 200), "white"),
                                          view.blocks, view.ocr_dpi, 75)
    corner = round(60 * 75 / expected)
    assert out.getpixel((corner, corner)) != (255, 255, 255)


def test_view_without_row_has_default_dpi(conn, tmp_path):
    _write_text(tmp_path, "d", ["Hej"])
    assert page_review.load_page_view(conn, tmp_path, "d", 1).ocr_dpi == 300


def test_overlay_colour_per_kind_and_none_bbox():
    img = Image.new("RGB", (100, 100), "white")
    blocks = [
        LayoutBlock(kind=BlockKind.HEADING, text="a", order=0, bbox=(10, 10, 40, 40)),
        LayoutBlock(kind=BlockKind.TABLE, text="b", order=1, bbox=(50, 50, 90, 90)),
        LayoutBlock(kind=BlockKind.FIGURE, text="c", order=2, bbox=None),
    ]
    out = page_review.draw_layout_overlay(img, blocks, ocr_dpi=100, view_dpi=100)
    assert out.getpixel((10, 10)) != out.getpixel((50, 50))


def test_escape_markdown_dollars():
    assert page_review.escape_markdown_dollars("a $5 och $6") == r"a \$5 och \$6"


def test_render_page_image_bounds(tmp_path):
    import pypdfium2 as pdfium
    pdf = tmp_path / "a.pdf"
    doc = pdfium.PdfDocument.new()
    doc.new_page(100, 100)
    doc.save(str(pdf))
    doc.close()
    for bad in (0, 2, -1):
        with pytest.raises(ValueError, match="finns inte"):
            page_review.render_page_image(pdf, bad)
    assert page_review.render_page_image(pdf, 1, dpi=72).size == (100, 100)


def test_missing_pdf_falls_back_to_text_pages(conn, tmp_path):
    _write_text(tmp_path, "d", ["a", "b", "c"])
    view = page_review.load_page_view(conn, tmp_path, "d", 1)
    assert view.page_count == 3 and view.pdf_available is False
    nothing = page_review.load_page_view(conn, tmp_path, "ingen", 1)
    assert nothing.page_count == 0 and nothing.current_text is None


def test_overlay_inverted_and_empty_bbox():
    img = Image.new("RGB", (100, 100), "white")
    blocks = [
        LayoutBlock(kind=BlockKind.PARAGRAPH, text="a", order=0, bbox=(40, 40, 10, 10)),
        LayoutBlock(kind=BlockKind.PARAGRAPH, text="b", order=1, bbox=(60, 10, 60, 50)),
    ]
    out = page_review.draw_layout_overlay(img, blocks, ocr_dpi=100, view_dpi=100)
    assert out.getpixel((10, 10)) != (255, 255, 255)
    assert out.getpixel((60, 30)) == (255, 255, 255)


def test_corrupt_layout_json_gives_no_blocks(conn, tmp_path):
    _write_text(tmp_path, "d", ["Hej"])
    record_page(conn, pdf_stem="d", page_num=1, engine="surya-layout", text="Hej",
                score=1.0, markdown="Hej", layout_json="{inte json")
    view = page_review.load_page_view(conn, tmp_path, "d", 1)
    assert view.blocks == [] and view.markdown == "Hej"


def test_page_out_of_range_and_short_text_file(conn, tmp_path):
    _write_text(tmp_path, "d", ["bara en"])
    view = page_review.load_page_view(conn, tmp_path, "d", 5)
    assert view.current_text is None and view.stale is False
    record_page(conn, pdf_stem="d", page_num=5, engine="surya", text="x", score=1.0)
    assert page_review.load_page_view(conn, tmp_path, "d", 5).stale is False


def test_null_text_row_is_treated_as_no_row(conn, tmp_path):
    """Äldre pdf_pages-rad med text NULL: textfilen är den gällande läsningen."""
    _write_text(tmp_path, "d", ["sida ur hel-fils-OCR"])
    record_page(conn, pdf_stem="d", page_num=1, engine="surya", text=None, score=12.0,
                markdown="# gammal", layout_json="[]", engine_identity="id-x")
    view = page_review.load_page_view(conn, tmp_path, "d", 1)
    assert view.engine == "hel-fils-OCR"
    assert view.score is None and view.processed_at is None
    assert view.markdown is None and view.blocks == [] and view.engine_identity is None
    assert view.stale is False
    assert view.current_text == "sida ur hel-fils-OCR"
