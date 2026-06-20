"""Tester för OCR-tolerant fuzzy-sökning (src/search_fuzzy.py)."""

from __future__ import annotations

from search_fuzzy import build_index, fuzzy_search, tokenize


def test_tokenize_lowercases_and_keeps_swedish_letters() -> None:
    assert tokenize("Stig Engström, 1986!") == ["stig", "engström", "1986"]


def test_fuzzy_search_finds_ocr_garbled_term() -> None:
    rows = [
        {"source": "a.txt", "page": 1, "chunk_idx": 0,
         "text": "vittnet såg Olof Palme på Sveavägen"},      # OCR-fel: Paine?
        {"source": "b.txt", "page": 2, "chunk_idx": 0,
         "text": "Olof Paine mördades nära korsningen"},        # OCR-fel
        {"source": "c.txt", "page": 3, "chunk_idx": 0,
         "text": "helt orelaterad text om något annat"},
    ]
    index = build_index(rows)
    hits = fuzzy_search(rows, "Palme", index=index, top_k=10, threshold=0.6)
    sources = {h["source"] for h in hits}
    # Både den exakta och den OCR-stavade ("Paine") träffen ska komma med.
    assert "a.txt" in sources
    assert "b.txt" in sources
    assert "c.txt" not in sources


def test_fuzzy_search_ranks_exact_match_first() -> None:
    rows = [
        {"source": "garbled.txt", "page": 1, "chunk_idx": 0, "text": "Engstrcm"},
        {"source": "exact.txt", "page": 1, "chunk_idx": 0, "text": "Engström"},
    ]
    index = build_index(rows)
    hits = fuzzy_search(rows, "Engström", index=index, top_k=10, threshold=0.6)
    assert hits[0]["source"] == "exact.txt"


def test_fuzzy_search_respects_threshold_and_top_k() -> None:
    rows = [
        {"source": f"{i}.txt", "page": 1, "chunk_idx": i, "text": "Palme"}
        for i in range(5)
    ]
    index = build_index(rows)
    hits = fuzzy_search(rows, "Palme", index=index, top_k=3, threshold=0.6)
    assert len(hits) == 3
    # Helt olik fråga ger inga träffar.
    assert fuzzy_search(rows, "zzzzzz", index=index, top_k=3, threshold=0.8) == []
