"""Verklig Surya 2-körning (extern runtime): hoppas när layoutmotorn inte går att köra.

Markerad ``external`` och bortvald i den vanliga sviten (``addopts`` i
``pyproject.toml``). Kör uttryckligen med::

    PALME_SURYA_PYTHON=~/.local/share/infoscry/surya-venv/bin/python \\
        .venv/bin/pytest -m external tests/test_surya_real.py -v

Sidan ritas här med PIL (inget arkivmaterial), så testet kan spridas fritt.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

pytestmark = pytest.mark.external

HEADER = ("Datum", "Plats", "Iakttagelse")
ROWS = (
    ("1986-02-28", "Sveavägen", "Man i mörk rock"),
    ("1986-03-01", "Tunnelgatan", "Springande person"),
    ("1986-03-02", "Kungsgatan", "Bil med två män"),
)
FONT_DIR = Path("/System/Library/Fonts/Supplemental")


def _font(bold: bool, size: int = 34) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    name = "Arial Bold.ttf" if bold else "Arial.ttf"
    path = FONT_DIR / name
    if path.exists():
        return ImageFont.truetype(str(path), size)
    return ImageFont.load_default(size=size)


def _table_page(path: Path) -> None:
    """En A4-sida i 150 dpi med rubrik, ett stycke och en tabell med rutnät."""
    image = Image.new("RGB", (1240, 1754), "white")
    draw = ImageDraw.Draw(image)
    draw.text((100, 100), "Sammanställning av iakttagelser", font=_font(True, 44), fill="black")
    draw.text((100, 190), "Tabellen nedan listar tre iakttagelser från vittnesförhören.",
              font=_font(False, 30), fill="black")
    widths = (260, 300, 480)
    x0, y0, row_h = 100, 300, 70
    rows = (HEADER, *ROWS)
    for r, row in enumerate(rows):
        x = x0
        y = y0 + r * row_h
        for w, cell in zip(widths, row, strict=True):
            if r == 0:
                draw.rectangle((x, y, x + w, y + row_h), fill=(220, 220, 220))
            draw.rectangle((x, y, x + w, y + row_h), outline="black", width=3)
            draw.text((x + 14, y + 18), cell, font=_font(r == 0, 30), fill="black")
            x += w
    image.save(path)


def test_real_surya_reads_table_page(tmp_path: Path) -> None:
    import surya_layout
    from surya_client import SuryaWorker, surya_layout_unavailable_reason

    reason = surya_layout_unavailable_reason()
    if reason is not None:
        pytest.skip(f"Surya 2-layout går inte att köra: {reason}")

    page = tmp_path / "tabell.png"
    _table_page(page)
    with SuryaWorker() as worker:
        raw = worker.read_page("tabelltest", 1, page)
    assert raw.status == "read"

    blocks, _degraded = surya_layout.parse_page(raw.blocks, page_height=1754)
    tables = [b for b in blocks if b.kind is surya_layout.BlockKind.TABLE and b.rows]
    assert tables, f"inget TABLE-block: {[b.kind for b in blocks]}"
    rows = tables[0].rows
    assert rows is not None
    header = [c.text for c in rows[0]]
    assert all(c.header for c in rows[0]), rows[0]  # rubrikraden kommer som <th>
    for word in HEADER:
        assert any(word in cell for cell in header), (word, header)
    body = " ".join(c.text for row in rows[1:] for c in row)
    for word in ("Sveavägen", "Tunnelgatan", "Kungsgatan"):
        assert word in body
