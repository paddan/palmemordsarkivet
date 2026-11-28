"""Tester för dokumentlagret i Neo4j (``graph.load_documents``).

Neo4j-testerna körs bara mot en tom testdatabas (``NEO4J_TEST_URI``); de övriga
använder en falsk session som spelar in varje sats och dess parametrar.
"""
from __future__ import annotations

import pytest

from graph import load_documents
from graph.document_rows import DocumentRows, build_document_rows
from operations.context import OperationContext, TerminalSink
from operations.exceptions import OperationFailed


def _ctx() -> OperationContext:
    return OperationContext(sink=TerminalSink(), cancel_requested=lambda: False)


def _meta(stem: str, **kw) -> dict:
    base = {
        "pdf_stem": stem, "arkiv": "palme", "nr": stem.split(" — ")[0],
        "titel": "Titel", "bestallt": None, "upplagt": None, "anmarkning": None,
        "antal_sidor": None, "dokumentdatum": None, "avkortat": False,
        "uppslag": [], "hanvisningar": [], "del_av": None,
    }
    base.update(kw)
    return base


def _rows(metas: list[dict], counts: dict[str, int]) -> DocumentRows:
    return build_document_rows(metas, counts)


# ── Falsk session ────────────────────────────────────────────────────────────

class _Result:
    def __init__(self, rows):
        self._rows = rows

    def data(self):
        return self._rows

    def consume(self):
        return None


class FakeSession:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.tx_of: list[int | None] = []  # transaktion per anrop (None = utanför)
        self._tx: int | None = None
        self.transactions = 0

    def run(self, query, **params):
        self.calls.append((query, params))
        self.tx_of.append(self._tx)
        return _Result([])

    def execute_write(self, fn):
        self.transactions += 1
        self._tx = self.transactions
        try:
            return fn(self)
        finally:
            self._tx = None


def _written(session: FakeSession) -> list[tuple[str, dict]]:
    return [c for c in session.calls if "CONSTRAINT" not in c[0]]


def _rows_for_fake(n_docs: int = 3) -> DocumentRows:
    stems = [f"D{i} — Titel med ' och \" och —" for i in range(n_docs)]
    metas = [_meta(s, uppslag=["D4991-00"]) for s in stems]
    return _rows(metas, {s: 2 for s in stems})


def test_phases_run_in_order() -> None:
    session = FakeSession()
    load_documents.write_document_layer(session, _rows_for_fake(), batch=1000, ctx=_ctx())
    queries = [q for q, _ in _written(session)]
    first = {
        "doc": next(i for i, q in enumerate(queries) if "MERGE (d:Dokument" in q),
        "sida": next(i for i, q in enumerate(queries) if "MERGE (s:Sida" in q),
        "edges": next(i for i, q in enumerate(queries) if "INGÅR_I" in q),
        "cleanup": next(i for i, q in enumerate(queries) if "DELETE s" in q),
    }
    assert first["doc"] < first["sida"] < first["edges"] < first["cleanup"]


def test_all_statements_are_parameterized() -> None:
    rows = _rows_for_fake()
    session = FakeSession()
    load_documents.write_document_layer(session, rows, batch=2, ctx=_ctx())
    texts = [d["stem"] for d in rows.documents] + [p["id"] for p in rows.pages] + ["D4991-00"]
    for query, _params in session.calls:
        for text in texts:
            assert text not in query


def test_no_statement_modifies_knowledge_graph_edges() -> None:
    session = FakeSession()
    load_documents.write_document_layer(session, _rows_for_fake(), batch=1000, ctx=_ctx())
    for query, _ in session.calls:
        assert "NÄMNER" not in query
        assert "RELATERAR" not in query


def test_batches_split_rows() -> None:
    rows = _rows_for_fake(5)
    session = FakeSession()
    load_documents.write_document_layer(session, rows, batch=2, ctx=_ctx())
    doc_batches = [p["rows"] for q, p in session.calls
                   if "MERGE (d:Dokument {stem: row.stem})" in q and "rows" in p]
    assert [len(b) for b in doc_batches] == [2, 2, 1]
    page_batches = [p["rows"] for q, p in session.calls if "MERGE (s:Sida" in q]
    assert max(len(b) for b in page_batches) <= 2
    assert sum(len(b) for b in page_batches) == len(rows.pages)


def test_phase3_batch_is_one_transaction() -> None:
    rows = _rows_for_fake(2)
    rows = DocumentRows(rows.documents, rows.pages, rows.uppslag,
                        [{"stem": rows.documents[0]["stem"],
                          "target_stem": rows.documents[1]["stem"]}],
                        [], 0)
    session = FakeSession()
    load_documents.write_document_layer(session, rows, batch=1000, ctx=_ctx())
    txs = {session.tx_of[i] for i, (q, _) in enumerate(session.calls)
           if "DELETE r" in q and "$stems" in q or "INGÅR_I]->(u)" in q
           or "HÄNVISAR_TILL]->(t)" in q}
    assert len(txs) == 1 and None not in txs


def test_returns_counts() -> None:
    rows = _rows_for_fake(2)
    counts = load_documents.write_document_layer(
        FakeSession(), rows, batch=1000, ctx=_ctx())
    assert counts["documents"] == 2
    assert counts["pages"] == 4
    assert counts["uppslag"] == 1
    for key in ("removed_pages", "removed_documents", "cleared_documents",
                "removed_uppslag"):
        assert counts[key] == 0


def test_missing_password_fails(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(load_documents, "resolve_password", lambda: None)
    with pytest.raises(OperationFailed, match="NEO4J_PASSWORD"):
        load_documents.run_load_documents(
            uri="bolt://x", user="neo4j", batch=10, text_dir=tmp_path, context=_ctx())


def test_batch_below_one_fails(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(load_documents, "resolve_password", lambda: "pw")
    with pytest.raises(OperationFailed, match="minst 1"):
        load_documents.run_load_documents(
            uri="bolt://x", user="neo4j", batch=0, text_dir=tmp_path, context=_ctx())


# ── Neo4j (CI) ───────────────────────────────────────────────────────────────

def _load(session, metas, counts, batch=1000):
    load_documents.write_document_layer(
        session, _rows(metas, counts), batch=batch, ctx=_ctx())


def _count(session, cypher: str) -> int:
    return session.run(cypher).single()["c"]


@pytest.mark.neo4j
def test_load_creates_documents_pages_and_edges(neo4j_session) -> None:
    metas = [
        _meta("A", nr="1", titel="Första", uppslag=["D4991-00"], hanvisningar=["2"]),
        _meta("B", nr="2", titel="Andra", uppslag=["D4991-00"]),
    ]
    _load(neo4j_session, metas, {"A": 2, "B": 1})
    assert _count(neo4j_session, "MATCH (d:Dokument) RETURN count(d) AS c") == 2
    assert _count(neo4j_session, "MATCH (s:Sida) RETURN count(s) AS c") == 3
    ids = {r["id"] for r in neo4j_session.run("MATCH (s:Sida) RETURN s.id AS id")}
    assert ids == {"A#1", "A#2", "B#1"}
    assert _count(neo4j_session, "MATCH (u:Uppslag) RETURN count(u) AS c") == 1
    assert _count(neo4j_session, "MATCH ()-[r:INGÅR_I]->() RETURN count(r) AS c") == 2
    assert _count(neo4j_session, "MATCH ()-[r:HÄNVISAR_TILL]->() RETURN count(r) AS c") == 1
    titel = neo4j_session.run("MATCH (d:Dokument {stem:'A'}) RETURN d.titel AS t").single()["t"]
    assert titel == "Första"


@pytest.mark.neo4j
def test_reload_is_idempotent(neo4j_session) -> None:
    metas = [
        _meta("A", nr="1", uppslag=["D4991-00"], hanvisningar=["2"]),
        _meta("B", nr="2", uppslag=["D4991-00"]),
    ]
    _load(neo4j_session, metas, {"A": 2, "B": 1})
    first = (_count(neo4j_session, "MATCH (n) RETURN count(n) AS c"),
             _count(neo4j_session, "MATCH ()-[r]->() RETURN count(r) AS c"))
    _load(neo4j_session, metas, {"A": 2, "B": 1}, batch=1)
    second = (_count(neo4j_session, "MATCH (n) RETURN count(n) AS c"),
              _count(neo4j_session, "MATCH ()-[r]->() RETURN count(r) AS c"))
    assert first == second


@pytest.mark.neo4j
def test_reload_removes_stale_pages_and_documents_but_keeps_mentions(neo4j_session) -> None:
    neo4j_session.run(
        "CREATE (:Dokument {stem:'A'})-[:NÄMNER {graph_owner:'palmemordsarkivet'}]"
        "->(:Person {norm:'x'})").consume()
    _load(neo4j_session, [_meta("A", nr="1", titel="T"), _meta("B", nr="2")],
          {"A": 2, "B": 1})
    _load(neo4j_session, [_meta("A", nr="1", titel="T")], {"A": 1})
    ids = {r["id"] for r in neo4j_session.run("MATCH (s:Sida) RETURN s.id AS id")}
    assert ids == {"A#1"}
    assert _count(neo4j_session, "MATCH (d:Dokument {stem:'B'}) RETURN count(d) AS c") == 0
    assert _count(neo4j_session,
                  "MATCH (:Dokument {stem:'A'})-[r:NÄMNER]->(:Person {norm:'x'}) "
                  "RETURN count(r) AS c") == 1
    rec = neo4j_session.run("MATCH (d:Dokument {stem:'A'}) RETURN d.nr AS nr, d.titel AS t").single()
    assert (rec["nr"], rec["t"]) == ("1", "T")


@pytest.mark.neo4j
def test_odd_characters_in_stem(neo4j_session) -> None:
    stem = "Pol-1 — Ett 'citat' och \"dubbla\" — slut"
    _load(neo4j_session, [_meta(stem, nr="Pol-1")], {stem: 1})
    got = neo4j_session.run(
        "MATCH (d:Dokument {stem: $s})-[:HAR_SIDA]->(p:Sida) RETURN p.id AS id", s=stem
    ).single()
    assert got["id"] == f"{stem}#1"
