"""Tester för maskeringsutforskaren (src/redactions.py)."""

from __future__ import annotations

from db import connect, init_schema, record_page
from redactions import (
    MASK_TOKEN,
    count_redactions,
    document_label,
    documents_by_redaction,
    documents_table_html,
    filter_documents,
    page_redactions,
    redaction_snippets,
    selected_document,
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


def test_filter_and_select_documents_for_lightweight_ui() -> None:
    docs = [
        {"pdf_stem": "Liggaren_4626-7844", "redactions": 518, "pages_with_redactions": 78},
        {"pdf_stem": "Pol-1986-03-17 Ingvar Carlsson", "redactions": 21, "pages_with_redactions": 1},
    ]

    filtered = filter_documents(docs, "ingvar")

    assert filtered == [docs[1]]
    assert filter_documents(docs, "") == docs
    assert selected_document(docs, "Liggaren_4626-7844") == docs[0]
    assert selected_document(docs, "saknas") is None
    assert document_label(docs[1]) == "Pol-1986-03-17 Ingvar Carlsson — 21 maskeringar på 1 sida"


def test_documents_table_html_makes_rows_clickable_without_checkboxes() -> None:
    docs = [
        {"pdf_stem": "Liggaren_4626-7844", "redactions": 518, "pages_with_redactions": 78},
        {"pdf_stem": "Pol-1986-03-17 Ingvar Carlsson", "redactions": 21, "pages_with_redactions": 1},
    ]

    html = documents_table_html(docs, selected_stem=docs[1]["pdf_stem"])

    assert 'href="/Maskeringar?redaction_doc=Liggaren_4626-7844#maskeringar-detalj"' in html
    assert 'href="/Maskeringar?redaction_doc=Pol-1986-03-17%20Ingvar%20Carlsson#maskeringar-detalj"' in html
    assert "redaction-row redaction-selected" in html
    assert "<input" not in html
    assert "checkbox" not in html


def test_documents_table_html_uses_real_table_headers_and_cells() -> None:
    docs = [
        {"pdf_stem": "Liggaren_4626-7844", "redactions": 518, "pages_with_redactions": 78},
    ]

    html = documents_table_html(docs)

    assert "<table" in html
    assert "<thead>" in html
    assert '<th scope="col">Dokument</th>' in html
    assert '<th scope="col">Maskeringar</th>' in html
    assert '<th scope="col">Sidor</th>' in html
    assert "<tbody>" in html
    assert "<td" in html
    assert "background:rgba(128,128,128,.18)" in html
    assert "#fff" not in html


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
