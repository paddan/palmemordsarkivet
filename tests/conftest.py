"""Test-fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "rag"))


@pytest.fixture
def tiny_pdf(tmp_path: Path) -> Path:
    """Skapa en enkel PDF med pymupdf. Skipas om pymupdf saknas."""
    pymupdf = pytest.importorskip("pymupdf")
    pdf_path = tmp_path / "tiny.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Hej världen från testet")
    doc.save(str(pdf_path))
    doc.close()
    return pdf_path
