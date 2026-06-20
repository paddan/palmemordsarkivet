"""Tester för facetterad sökning (src/facets.py)."""

from __future__ import annotations

from db import connect, init_schema, record_doc_entities
from facets import (
    entity_facets,
    entity_stem_index,
    filter_hits_by_stems,
    source_to_stem,
    sources_where_clause,
    stems_with_entity,
)


def _fresh(tmp_path):
    conn = connect(tmp_path / "state.db")
    init_schema(conn)
    return conn


def test_source_to_stem_strips_txt_suffix() -> None:
    assert source_to_stem("100 — Skandia.txt") == "100 — Skandia"
    assert source_to_stem("100 — Skandia") == "100 — Skandia"


def _seed(conn) -> None:
    record_doc_entities(
        conn, pdf_stem="A", page_num=1, model="test",
        payload={"entiteter": [
            {"typ": "person", "namn": "Stig Engström"},
            {"typ": "plats", "namn": "Sveavägen"},
        ]},
    )
    record_doc_entities(
        conn, pdf_stem="A", page_num=2, model="test",
        payload={"entiteter": [{"typ": "person", "namn": "Stig Engström"}]},
    )
    record_doc_entities(
        conn, pdf_stem="B", page_num=1, model="test",
        payload={"entiteter": [
            {"typ": "person", "namn": "stig engström"},
            {"typ": "organisation", "namn": "Skandia"},
        ]},
    )


def test_entity_facets_grouped_with_counts(tmp_path) -> None:
    conn = _fresh(tmp_path)
    _seed(conn)
    facets = entity_facets(conn)
    persons = dict(facets["person"])
    # Räknar antal dokument (pdf_stem), inte sidor, och slår ihop på casefold.
    assert persons["Stig Engström"] == 2
    assert "Skandia" in dict(facets["organisation"])
    # Mest förekommande först inom varje typ.
    assert facets["person"][0][0] == "Stig Engström"


def test_stems_with_entity_is_case_insensitive(tmp_path) -> None:
    conn = _fresh(tmp_path)
    _seed(conn)
    assert stems_with_entity(conn, "stig ENGSTRÖM") == {"A", "B"}
    assert stems_with_entity(conn, "Skandia") == {"B"}
    assert stems_with_entity(conn, "okänd") == set()


def test_entity_stem_index_maps_casefolded_name_to_stems(tmp_path) -> None:
    conn = _fresh(tmp_path)
    _seed(conn)
    idx = entity_stem_index(conn)
    assert idx["stig engström"] == {"A", "B"}
    assert idx["skandia"] == {"B"}


def test_sources_where_clause_quotes_and_escapes() -> None:
    assert sources_where_clause(set()) is None
    clause = sources_where_clause({"100 — A", "200 — B"})
    assert clause == "source IN ('100 — A.txt','200 — B.txt')"
    # Enkelfnuttar i filnamn escapas så klausulen inte bryts.
    assert sources_where_clause({"O'Brien"}) == "source IN ('O''Brien.txt')"


def test_filter_hits_by_stems() -> None:
    # source är "<pdf_stem>.txt" så filtret matchar på stem.
    hits = [
        {"source": "A — titel.txt", "text": "x"},
        {"source": "B — titel.txt", "text": "y"},
        {"source": "C — titel.txt", "text": "z"},
    ]
    kept = filter_hits_by_stems(hits, {"A — titel", "C — titel"})
    assert [h["source"] for h in kept] == ["A — titel.txt", "C — titel.txt"]
