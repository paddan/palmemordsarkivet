"""Tester för ask.py — hit-nycklar, kontextformatering och RRF-fusion."""

from __future__ import annotations

import numpy as np
from ask import _hit_key, format_context, search_hybrid


class _FakeQuery:
    def __init__(self, rows: list[dict]):
        self._rows = rows
        self.selected: list[str] | None = None

    def limit(self, n: int) -> _FakeQuery:
        self._rows = self._rows[:n]
        return self

    def select(self, cols) -> _FakeQuery:
        self.selected = list(cols)
        return self

    def to_list(self) -> list[dict]:
        return list(self._rows)


class _FakeTable:
    """Minimal LanceDB-tabell: vektor- och FTS-sökning ur fasta listor."""

    def __init__(self, vec_rows: list[dict], fts_rows: list[dict] | None = None,
                 fts_error: Exception | None = None):
        self._vec = vec_rows
        self._fts = fts_rows or []
        self._fts_error = fts_error
        self.vec_query: _FakeQuery | None = None
        self.fts_query: _FakeQuery | None = None

    def search(self, q, query_type: str | None = None):
        if query_type == "fts":
            if self._fts_error is not None:
                raise self._fts_error
            self.fts_query = _FakeQuery(self._fts)
            return self.fts_query
        self.vec_query = _FakeQuery(self._vec)
        return self.vec_query


class _FakeModel:
    def encode(self, texts, **kwargs):
        return np.zeros((len(texts), 3), dtype=np.float32)


def _hit(source: str, page: int, chunk: int) -> dict:
    return {"text": f"text {source} s{page}", "source": source, "page": page,
            "chunk_idx": chunk, "nr": source.split(" ")[0], "titel": "Titel"}


# ---------------------------------------------------------------------------
# _hit_key
# ---------------------------------------------------------------------------

def test_hit_key_distinguishes_chunks_with_same_text() -> None:
    a = _hit("281 — x.txt", 1, 0)
    b = _hit("281 — x.txt", 1, 1)
    assert _hit_key(a) != _hit_key(b)


def test_hit_key_tolerates_missing_fields() -> None:
    assert _hit_key({}) == ("", 0, -1)


# ---------------------------------------------------------------------------
# format_context
# ---------------------------------------------------------------------------

def test_format_context_includes_nr_page_and_text() -> None:
    out = format_context([_hit("281 — x.txt", 4, 0)])
    assert "[Nr 281, sida 4" in out
    assert "text 281 — x.txt s4" in out


# ---------------------------------------------------------------------------
# search_hybrid (RRF)
# ---------------------------------------------------------------------------

def test_rrf_ranks_chunk_present_in_both_lists_first() -> None:
    common = _hit("a.txt", 1, 0)
    vec_only = _hit("b.txt", 1, 0)
    fts_only = _hit("c.txt", 1, 0)
    table = _FakeTable(vec_rows=[vec_only, common], fts_rows=[fts_only, common])
    hits = search_hybrid(table, _FakeModel(), "fråga", top_k=3)
    assert _hit_key(hits[0]) == _hit_key(common)
    assert len(hits) == 3


def test_rrf_respects_top_k() -> None:
    vec = [_hit(f"v{i}.txt", 1, 0) for i in range(5)]
    fts = [_hit(f"f{i}.txt", 1, 0) for i in range(5)]
    table = _FakeTable(vec_rows=vec, fts_rows=fts)
    hits = search_hybrid(table, _FakeModel(), "fråga", top_k=4)
    assert len(hits) == 4


def test_fts_search_selects_score_to_avoid_lancedb_warning() -> None:
    table = _FakeTable(
        vec_rows=[_hit("v.txt", 1, 0)],
        fts_rows=[_hit("f.txt", 1, 0)],
    )

    search_hybrid(table, _FakeModel(), "fråga", top_k=2)

    assert table.fts_query is not None
    assert table.fts_query.selected is not None
    assert "_score" in table.fts_query.selected


def test_fts_failure_falls_back_to_vector_hits() -> None:
    vec = [_hit("a.txt", 1, 0), _hit("b.txt", 1, 0)]
    table = _FakeTable(vec_rows=vec, fts_error=RuntimeError("tantivy saknas"))
    hits = search_hybrid(table, _FakeModel(), "fråga", top_k=2)
    assert [h["source"] for h in hits] == ["a.txt", "b.txt"]
