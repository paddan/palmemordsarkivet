"""Tester för chunk_text från rag/ingest.py."""

from __future__ import annotations

from ingest import chunk_text


def test_short_text_one_chunk() -> None:
    chunks = chunk_text("Kort text.", size=800, overlap=150)
    assert len(chunks) == 1
    assert chunks[0][2] == "Kort text."


def test_long_text_multiple_chunks() -> None:
    text = ("Mening ett. " * 200).strip()
    chunks = chunk_text(text, size=800, overlap=150)
    assert len(chunks) >= 2
    # alla chunks ska vara icke-tomma
    for _, _, c in chunks:
        assert c.strip()


def test_no_formfeed_in_chunks() -> None:
    text = "Sida ett.\fSida två."
    chunks = chunk_text(text, size=800, overlap=150)
    # chunkern får aldrig spotta ut \f
    for _, _, c in chunks:
        assert "\f" not in c


def test_empty_text() -> None:
    assert chunk_text("", 800, 150) == []
    assert chunk_text("   \n  ", 800, 150) == []
