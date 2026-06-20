"""Tester för maskeringsutforskaren (src/redactions.py)."""

from __future__ import annotations

from db import connect, init_schema, record_page
from redactions import (
    MASK_TOKEN,
    count_redactions,
    documents_by_redaction,
    page_redactions,
    redaction_snippets,
)


def test_count_redactions_counts_tokens() -> None:
    assert count_redactions("inget här") == 0
    assert count_redactions(f"namnet {MASK_TOKEN} bor på {MASK_TOKEN}") == 2


def test_redaction_snippets_gives_context_around_each_mask() -> None:
    text = "Vittnet " + MASK_TOKEN + " såg gärningsmannen springa mot tunnelbanan."
    snippets = redaction_snippets(text, window=10)
    assert len(snippets) == 1
    assert MASK_TOKEN in snippets[0]
    assert "Vittnet" in snippets[0]
    assert "såg" in snippets[0]


def test_redaction_snippets_collapses_whitespace() -> None:
    text = f"rad ett\n\n{MASK_TOKEN}\n\nrad tre"
    snippets = redaction_snippets(text, window=20)
    assert "\n" not in snippets[0]


def _fresh(tmp_path):
    conn = connect(tmp_path / "state.db")
    init_schema(conn)
    return conn


def test_documents_by_redaction_aggregates_and_sorts(tmp_path) -> None:
    conn = _fresh(tmp_path)
    record_page(conn, pdf_stem="A", page_num=1, engine="tesseract",
                text=f"ett {MASK_TOKEN} två {MASK_TOKEN}", score=80)
    record_page(conn, pdf_stem="A", page_num=2, engine="tesseract",
                text=f"tre {MASK_TOKEN}", score=80)
    record_page(conn, pdf_stem="B", page_num=1, engine="tesseract",
                text=f"bara en {MASK_TOKEN}", score=80)
    record_page(conn, pdf_stem="C", page_num=1, engine="tesseract",
                text="ingen maskering alls", score=80)

    docs = documents_by_redaction(conn)
    # Bara dokument med minst en maskering, mest maskerade först.
    assert [d["pdf_stem"] for d in docs] == ["A", "B"]
    assert docs[0]["redactions"] == 3
    assert docs[0]["pages_with_redactions"] == 2
    assert docs[1]["redactions"] == 1


def test_page_redactions_lists_pages_with_snippets(tmp_path) -> None:
    conn = _fresh(tmp_path)
    record_page(conn, pdf_stem="A", page_num=1, engine="tesseract",
                text=f"hemligt {MASK_TOKEN} namn", score=80)
    record_page(conn, pdf_stem="A", page_num=2, engine="tesseract",
                text="vanlig sida", score=80)

    pages = page_redactions(conn, "A")
    assert len(pages) == 1
    assert pages[0]["page_num"] == 1
    assert pages[0]["redactions"] == 1
    assert pages[0]["snippets"]
