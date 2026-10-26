"""Sidvy-logik för sidgranskningen (Streamlit-fri).

Samlar det en granskningssida behöver om en enskild sida: den gällande texten
ur ``generated/text/<stem>.txt``, OCR-radens metadata, Markdown, layoutblock,
historiska läsningar och en renderad sidbild med ritade layoutramar.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import db as state_db
from normalize_text import markdown_is_fresh
from surya_layout import LEGACY_LAYOUT_DPI, BlockKind, LayoutBlock, layout_from_json

if TYPE_CHECKING:
    from PIL.Image import Image

log = logging.getLogger(__name__)

# Projektroten som Sidgranskning läser PDF:er och textfiler under (tester byter den).
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Motor som visas för sidor utan pdf_pages-rad (kommer från hel-fils-OCR:en).
WHOLE_FILE_ENGINE = "hel-fils-OCR"

# En färg per blocktyp (RGB) för layoutramarna.
KIND_COLOURS: dict[BlockKind, tuple[int, int, int]] = {
    BlockKind.HEADING: (200, 30, 30),
    BlockKind.PARAGRAPH: (30, 100, 220),
    BlockKind.LIST_ITEM: (20, 150, 150),
    BlockKind.TABLE: (230, 120, 0),
    BlockKind.FORMULA: (140, 60, 200),
    BlockKind.CODE: (90, 90, 90),
    BlockKind.CAPTION: (200, 160, 0),
    BlockKind.FOOTNOTE: (130, 100, 60),
    BlockKind.PAGE_HEADER: (0, 150, 60),
    BlockKind.PAGE_FOOTER: (120, 180, 60),
    BlockKind.FIGURE: (220, 60, 160),
    BlockKind.FORM: (0, 90, 160),
}


@dataclass(frozen=True)
class PageView:
    """Allt sidgranskningen visar om en sida."""

    pdf_stem: str
    page_num: int
    page_count: int
    current_text: str | None
    engine: str
    score: float | None
    processed_at: str | None
    engine_identity: str | None
    markdown: str | None
    blocks: list[LayoutBlock] = field(default_factory=list)
    stale: bool = False
    readings: list[dict] = field(default_factory=list)
    pdf_available: bool = False
    # dpi som layoutblockens bbox är i (sidan kan ha renderats under den begärda)
    ocr_dpi: int = LEGACY_LAYOUT_DPI


def find_pdf(root: Path, pdf_stem: str) -> Path | None:
    """PDF:en för en stem: palme-arkivet först, sedan wpu, annars ``None``."""
    for sub in ("files", "wpu_files"):
        cand = root / "downloaded" / sub / f"{pdf_stem}.pdf"
        if cand.exists():
            return cand
    return None


def page_count(pdf_path: Path) -> int:
    """Antal sidor i en PDF (pypdfium2)."""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        return int(len(doc))
    finally:
        doc.close()


def render_page_image(pdf_path: Path, page_num: int, dpi: int = 150) -> Image:
    """Rendera sida ``page_num`` (1-baserad) som PIL-bild."""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        if not 1 <= page_num <= len(doc):
            raise ValueError(
                f"Sidan {page_num} finns inte i {pdf_path.name} (giltigt: 1–{len(doc)})"
            )
        image: Image = doc[page_num - 1].render(scale=dpi / 72).to_pil()
        return image
    finally:
        doc.close()


def file_page(root: Path, pdf_stem: str, page_num: int) -> str | None:
    """Sidans gällande text ur textfilen (split på ``\\f``), eller ``None``."""
    txt = root / "generated" / "text" / f"{pdf_stem}.txt"
    if not txt.exists():
        return None
    pages = txt.read_text(encoding="utf-8").split("\f")
    if 1 <= page_num <= len(pages):
        return pages[page_num - 1]
    return None


def text_page_count(root: Path, pdf_stem: str) -> int | None:
    """Antal sidor i textfilen (split på ``\\f``), eller ``None`` om den saknas."""
    txt = root / "generated" / "text" / f"{pdf_stem}.txt"
    if not txt.exists():
        return None
    return len(txt.read_text(encoding="utf-8").split("\f"))


def load_page_view(
    conn: sqlite3.Connection, root: Path, pdf_stem: str, page_num: int,
) -> PageView:
    """Bygg sidvyn för en sida ur textfilen, ``pdf_pages`` och historiken."""
    pdf = find_pdf(root, pdf_stem)
    count = 0
    available = False
    if pdf is not None:
        try:
            count = page_count(pdf)
            available = True
        except Exception as exc:  # noqa: BLE001 - oläsbar PDF ska inte fälla vyn
            log.debug("sidantal för %s gick inte att läsa: %s", pdf_stem, exc)
    current = file_page(root, pdf_stem, page_num)
    if not available:
        count = text_page_count(root, pdf_stem) or 0
        log.debug("PDF saknas/oläsbar för %s, sidantal ur textfilen: %s", pdf_stem, count)
    row = state_db.get_page_row(conn, pdf_stem, page_num)
    readings = [dict(r) for r in state_db.list_page_readings(conn, pdf_stem, page_num)]
    if row is not None and row["text"] is None:
        # Äldre rad med text NULL (markör från tidiga körningar): läsningen finns
        # bara i textfilen, så sidan visas som hel-fils-OCR utan radens metadata.
        log.debug("%s s.%s: pdf_pages-raden (%s) saknar text, textfilen gäller",
                  pdf_stem, page_num, row["engine"])
        row = None
    if row is None:
        return PageView(
            pdf_stem=pdf_stem, page_num=page_num, page_count=count,
            current_text=current, engine=WHOLE_FILE_ENGINE, score=None,
            processed_at=None, engine_identity=None, markdown=None,
            blocks=[], stale=False, readings=readings, pdf_available=available,
        )
    blocks: list[LayoutBlock] = []
    ocr_dpi = LEGACY_LAYOUT_DPI
    if row["layout_json"]:
        try:
            ocr_dpi, blocks = layout_from_json(row["layout_json"])
        except Exception as exc:  # noqa: BLE001 - trasig layout visas som utan block
            log.debug("layout för %s s.%s kunde inte tolkas: %s", pdf_stem, page_num, exc)
        else:
            log.debug("layout för %s s.%s: %d block i %d dpi", pdf_stem, page_num, len(blocks), ocr_dpi)
    stale = current is not None and not markdown_is_fresh(row["text"], current)
    return PageView(
        pdf_stem=pdf_stem, page_num=page_num, page_count=count,
        current_text=current, engine=row["engine"], score=row["score"],
        processed_at=row["processed_at"], engine_identity=row["engine_identity"],
        markdown=row["markdown"], blocks=blocks, stale=stale, readings=readings,
        pdf_available=available, ocr_dpi=ocr_dpi,
    )


def draw_layout_overlay(
    image: Image, blocks: list[LayoutBlock], ocr_dpi: int, view_dpi: int,
) -> Image:
    """Kopia av ``image`` med en ram per block (färg per typ, ingen fyllning).

    Blockens bbox är i OCR-bildens pixlar och skalas med ``view_dpi / ocr_dpi``;
    ``ocr_dpi`` är layoutens lagrade dpi (``PageView.ocr_dpi``).
    Block utan bbox hoppas över. Indatabilden ändras aldrig.
    """
    from PIL import ImageDraw

    out = image.convert("RGB").copy()
    draw = ImageDraw.Draw(out)
    scale = view_dpi / ocr_dpi
    for block in blocks:
        if block.bbox is None:
            continue
        a, b, c, d = (round(v * scale) for v in block.bbox)
        x0, x1 = min(a, c), max(a, c)
        y0, y1 = min(b, d), max(b, d)
        if x0 == x1 or y0 == y1:
            log.debug("hoppar block med tom bbox %s", block.bbox)
            continue
        draw.rectangle((x0, y0, x1, y1), outline=KIND_COLOURS.get(block.kind, (255, 0, 0)), width=2)
    return out


def escape_markdown_dollars(md: str) -> str:
    """Escapa ``$`` så Streamlit inte tolkar två dollartecken som LaTeX."""
    return md.replace("$", "\\$")
