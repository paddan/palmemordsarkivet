"""Tester för detect_redactions_image och _merge_redaction_markers."""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _make_image(width: int, height: int, dark_bands: list[tuple[int, int, int]] | None = None):
    """Skapa en PIL-bild med vita pixlar och valfria mörka horisontella band.

    dark_bands: [(y_start, y_end, x_fraction)] — mörkhet läggs i hela raden
    upp till x_fraction av bredden (default 1.0 = hela raden).
    """
    from PIL import Image

    img = Image.new("RGB", (width, height), color=(240, 240, 240))
    pixels = img.load()
    for y0, y1, x_frac in (dark_bands or []):
        x_end = int(width * x_frac)
        for y in range(y0, y1 + 1):
            for x in range(x_end):
                pixels[x, y] = (5, 5, 5)
    return img


# ---------------------------------------------------------------------------
# detect_redactions_image
# ---------------------------------------------------------------------------

def test_no_redactions_white_page():
    from ocr_pages import detect_redactions_image
    img = _make_image(300, 400)
    assert detect_redactions_image(img) == []


def test_single_wide_dark_band():
    from ocr_pages import detect_redactions_image
    # Band på rader 100-120, täcker 80 % av bredden — ska detekteras
    img = _make_image(300, 400, [(100, 120, 0.80)])
    blocks = detect_redactions_image(img)
    assert len(blocks) == 1
    y0, y1 = blocks[0]
    assert 95 <= y0 <= 105
    assert 115 <= y1 <= 125


def test_narrow_band_not_detected():
    from ocr_pages import detect_redactions_image
    # Band täcker bara 5 % av bredden — under tröskeln 10 %
    img = _make_image(300, 400, [(150, 165, 0.05)])
    assert detect_redactions_image(img) == []


def test_thin_band_under_min_height():
    from ocr_pages import detect_redactions_image
    # Band är bara 4 rader — under min_height=8
    img = _make_image(300, 400, [(200, 203, 0.80)])
    assert detect_redactions_image(img) == []


def test_two_separate_bands():
    from ocr_pages import detect_redactions_image
    img = _make_image(300, 600, [(50, 70, 0.90), (300, 330, 0.85)])
    blocks = detect_redactions_image(img)
    assert len(blocks) == 2


# ---------------------------------------------------------------------------
# _merge_redaction_markers — utan line_bboxes (approximerad position)
# ---------------------------------------------------------------------------

def test_merge_inserts_marker_approximate():
    from ocr_pages import _merge_redaction_markers

    text = "\n".join(f"rad {i}" for i in range(20))
    # Block mitt på sidan (y 400-440 av 1000 → ~40% → rad ~8)
    result = _merge_redaction_markers(text, [(400, 440)], image_height=1000)
    assert "[MASKAD]" in result
    lines = result.split("\n")
    idx = lines.index("[MASKAD]")
    assert 5 <= idx <= 12  # rimlig position


def test_merge_no_blocks_unchanged():
    from ocr_pages import _merge_redaction_markers

    text = "rad 1\nrad 2\nrad 3"
    assert _merge_redaction_markers(text, [], image_height=1000) == text


# ---------------------------------------------------------------------------
# _merge_redaction_markers — med line_bboxes (exakt surya-position)
# ---------------------------------------------------------------------------

def test_merge_surya_exact_position():
    from ocr_pages import _merge_redaction_markers

    # Tre textrader med y-koordinater 10, 50, 90
    bboxes = [
        {"text": "rad A", "bbox": [0, 5, 100, 20]},   # mittpunkt y=12.5
        {"text": "rad B", "bbox": [0, 45, 100, 60]},  # mittpunkt y=52.5
        {"text": "rad C", "bbox": [0, 85, 100, 100]}, # mittpunkt y=92.5
    ]
    # Maskeringsblock y 65-75 → mittpunkt 70 → ska hamna mellan B och C
    result = _merge_redaction_markers(
        "", [(65, 75)], image_height=200, line_bboxes=bboxes
    )
    lines = result.split("\n")
    assert lines == ["rad A", "rad B", "[MASKAD]", "rad C"]


def test_pdf_patch_preserves_original_when_text_line_cannot_be_inserted(tiny_pdf):
    pymupdf = pytest.importorskip("pymupdf")
    from ocr_pages import update_pdf_text_layer

    original = tiny_pdf.read_bytes()
    lines = {1: [{"text": "kan inte infogas", "bbox": [10, 10, 11, 11]}]}

    with patch.object(pymupdf.Page, "insert_textbox", return_value=-1), pytest.raises(
        RuntimeError, match="kunde inte infoga"
    ):
        update_pdf_text_layer(tiny_pdf, tiny_pdf, lines, dpi=72)

    assert tiny_pdf.read_bytes() == original


# ---------------------------------------------------------------------------
# detect_redactions_file (orkestrering: idempotens via markörfil, DB-synk)
# ---------------------------------------------------------------------------


def test_detect_redactions_file_returns_zero_when_txt_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    from ocr_pages import detect_redactions_file

    pdf = tmp_path / "doc.pdf"
    txt_file = tmp_path / "doc.txt"  # skapas aldrig
    marker = tmp_path / "doc.redact"

    assert detect_redactions_file(pdf, txt_file, marker) == 0


def test_detect_redactions_file_existing_marker_syncs_db_and_is_consumed(
    tmp_path, monkeypatch,
):
    # Idempotens: en markörfil från en tidigare, redan slutförd körning ska
    # bara synka DB:n och sedan tas bort — inte köra om detekteringen.
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    import db as state_db
    from ocr_pages import detect_redactions_file

    pdf = tmp_path / "doc.pdf"
    txt_file = tmp_path / "doc.txt"
    txt_file.write_text("Text med [MASKAD] redan infogat.", encoding="utf-8")
    marker = tmp_path / "doc.redact"
    marker.write_text("", encoding="utf-8")

    n = detect_redactions_file(pdf, txt_file, marker)

    assert n == 0
    assert not marker.exists()

    conn = state_db.connect()
    row = conn.execute(
        "SELECT redaction_checked_at, has_redactions FROM pdf_files WHERE pdf_stem=?",
        ("doc",),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["redaction_checked_at"] is not None
    assert row["has_redactions"] == 1


def test_detect_redactions_file_detects_and_inserts_marker(tmp_path, monkeypatch):
    # Regression: orkestreringen (inte bara detect_redactions_image i
    # isolering) ska faktiskt infoga [MASKAD] i sidtexten och skriva filen,
    # samt markera dokumentet i state.db.
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    import db as state_db
    import ocr_pages
    from ocr_pages import detect_redactions_file

    pdf = tmp_path / "doc.pdf"
    txt_file = tmp_path / "doc.txt"
    txt_file.write_text("Rad ett.\nRad två.\nRad tre.", encoding="utf-8")
    marker = tmp_path / "doc.redact"

    dark_image = _make_image(300, 400, dark_bands=[(100, 140, 1.0)])
    monkeypatch.setattr(
        ocr_pages, "render_pages", lambda pdf_path, dpi: iter([(1, dark_image)])
    )

    n = detect_redactions_file(pdf, txt_file, marker)

    assert n == 1
    assert "[MASKAD]" in txt_file.read_text(encoding="utf-8")
    assert not marker.exists()

    conn = state_db.connect()
    row = conn.execute(
        "SELECT redaction_checked_at, has_redactions FROM pdf_files WHERE pdf_stem=?",
        ("doc",),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["has_redactions"] == 1
