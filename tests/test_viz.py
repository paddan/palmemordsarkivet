"""Tester för graph.viz — subgrafbygge och Cytoscape-konvertering (ingen Neo4j krävs)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from graph.viz import assemble_graph, dedup_rels, search_entities


def test_assemble_graph_center_with_relations_and_docs() -> None:
    center = {"norm": "stig engström", "namn": "Stig Engström", "label": "Person"}
    rels = [{"s_norm": "stig engström", "s_namn": "Stig Engström", "s_label": "Person",
             "e_norm": "lars jepsson", "e_namn": "Lars Jepsson", "e_label": "Person",
             "typ": "igenkände"}]
    docs = [{"stem": "1 — PM", "nr": "1", "titel": "PM"}]
    nodes, edges = assemble_graph(center, rels, docs)
    ids = {n["id"] for n in nodes}
    assert {"stig engström", "lars jepsson", "doc:1 — PM"} <= ids
    doc = next(n for n in nodes if n["id"] == "doc:1 — PM")
    assert doc["stem"] == "1 — PM" and doc["type"] == "Dokument"
    # Dokumentnoden visar nr + titel, inte bara numret.
    assert doc["namn"] == "1 — PM"
    assert {"source": "stig engström", "target": "lars jepsson",
            "label": "igenkände", "count": 1} in edges
    assert {"source": "doc:1 — PM", "target": "stig engström",
            "label": "nämner", "count": 1} in edges


def test_assemble_graph_center_only() -> None:
    center = {"norm": "x", "namn": "X", "label": "Plats"}
    nodes, edges = assemble_graph(center, [], [])
    assert len(nodes) == 1 and nodes[0]["id"] == "x"
    assert edges == []


def test_assemble_graph_dedups_repeated_edge() -> None:
    # Samma relation finns på flera sidor/dokument → flera identiska rader.
    center = {"norm": "stig engström", "namn": "Stig Engström", "label": "Person"}
    rels = [
        {"s_norm": "stig engström", "s_namn": "Stig Engström", "s_label": "Person",
         "e_norm": "skandia", "e_namn": "Skandia", "e_label": "Organisation",
         "typ": "arbetade på"},
        {"s_norm": "stig engström", "s_namn": "Stig Engström", "s_label": "Person",
         "e_norm": "skandia", "e_namn": "Skandia", "e_label": "Organisation",
         "typ": "arbetade på"},
        {"s_norm": "stig engström", "s_namn": "Stig Engström", "s_label": "Person",
         "e_norm": "skandia", "e_namn": "Skandia", "e_label": "Organisation",
         "typ": "arbetade på"},
    ]
    nodes, edges = assemble_graph(center, rels, [])
    assert len(edges) == 1
    assert edges[0]["source"] == "stig engström"
    assert edges[0]["target"] == "skandia"
    assert edges[0]["label"] == "arbetade på"
    assert edges[0]["count"] == 3


def test_assemble_graph_dedups_repeated_node() -> None:
    center = {"norm": "a", "namn": "A", "label": "Person"}
    rels = [
        {"s_norm": "a", "s_namn": "A", "s_label": "Person",
         "e_norm": "b", "e_namn": "B", "e_label": "Person", "typ": "t1"},
        {"s_norm": "b", "s_namn": "B", "s_label": "Person",
         "e_norm": "a", "e_namn": "A", "e_label": "Person", "typ": "t2"},
    ]
    nodes, edges = assemble_graph(center, rels, [])
    assert sorted(n["id"] for n in nodes) == ["a", "b"]
    assert len(edges) == 2


def test_dedup_rels_collapses_same_rid() -> None:
    rows = [
        {"rid": "5:abc:1", "s_norm": "a", "e_norm": "b", "typ": "x"},
        {"rid": "5:abc:1", "s_norm": "a", "e_norm": "b", "typ": "x"},
        {"rid": "5:abc:2", "s_norm": "a", "e_norm": "c", "typ": "y"},
    ]
    out = dedup_rels(rows)
    assert len(out) == 2
    assert {r["rid"] for r in out} == {"5:abc:1", "5:abc:2"}


def test_dedup_rels_passes_through_rows_without_rid() -> None:
    rows = [{"s_norm": "a", "e_norm": "b"}, {"s_norm": "a", "e_norm": "b"}]
    assert len(dedup_rels(rows)) == 2


def test_assemble_graph_multiple_centers_marked() -> None:
    centers = [
        {"norm": "a", "namn": "A", "label": "Person"},
        {"norm": "b", "namn": "B", "label": "Organisation"},
    ]
    rels = [{"s_norm": "a", "s_namn": "A", "s_label": "Person",
             "e_norm": "b", "e_namn": "B", "e_label": "Organisation", "typ": "arbetade på"}]
    nodes, edges = assemble_graph(centers, rels, [])
    by_id = {n["id"]: n for n in nodes}
    assert by_id["a"].get("center") and by_id["b"].get("center")
    assert len(edges) == 1


def test_assemble_graph_doc_edges_target_their_own_center() -> None:
    centers = [
        {"norm": "a", "namn": "A", "label": "Person"},
        {"norm": "b", "namn": "B", "label": "Person"},
    ]
    docs = [
        {"stem": "1 — PM", "nr": "1", "titel": "PM", "center_norm": "b"},
    ]
    _, edges = assemble_graph(centers, [], docs)
    assert {"source": "doc:1 — PM", "target": "b",
            "label": "nämner", "count": 1} in edges


def test_search_entities_dedups() -> None:
    class _Session:
        def run(self, *a, **k):
            return [
                {"label": "Person", "namn": "Stig Engström", "norm": "stig engström"},
                {"label": "Person", "namn": "Stig Engström", "norm": "stig engström"},
                {"label": "Plats", "namn": "Stockholm", "norm": "stockholm"},
            ]
    out = search_entities(_Session(), "st", limit=10)
    assert len(out) == 2
    assert {o["label"] for o in out} == {"Person", "Plats"}


class _FakeSession:
    """Fejkar Neo4j-session: run() slår upp rader på söksträngen $q."""

    def __init__(self, by_query: dict[str, list[dict]]) -> None:
        self.by_query = by_query

    def run(self, cypher: str, **params):
        return list(self.by_query.get(params["q"].lower(), []))


def test_lookup_centers_exact_match_wins() -> None:
    from graph.viz import lookup_centers
    session = _FakeSession({"palme": [
        {"label": "Person", "namn": "Lisbeth Palme", "norm": "lisbeth palme"},
        {"label": "Person", "namn": "Palme", "norm": "palme"},
    ]})
    out = lookup_centers(session, ["Palme"])
    assert out == [{"label": "Person", "namn": "Palme", "norm": "palme"}]


def test_lookup_centers_falls_back_to_first_hit() -> None:
    from graph.viz import lookup_centers
    session = _FakeSession({"engström": [
        {"label": "Person", "namn": "Stig Engström", "norm": "stig engström"},
        {"label": "Person", "namn": "Margareta Engström", "norm": "margareta engström"},
    ]})
    out = lookup_centers(session, ["Engström"])
    assert out[0]["norm"] == "stig engström"


def test_lookup_centers_skips_misses_and_dedups() -> None:
    from graph.viz import lookup_centers
    hit = {"label": "Organisation", "namn": "Skandia", "norm": "skandia"}
    session = _FakeSession({"skandia": [hit], "skandiahuset": [hit]})
    out = lookup_centers(session, ["Skandia", "Okänd Person", "Skandiahuset"])
    assert out == [hit]

def test_to_cytoscape_elements_basic() -> None:
    from graph.viz import to_cytoscape_elements
    nodes = [
        {"id": "stig engström", "namn": "Stig Engström", "type": "Person",
         "center": True},
        {"id": "doc:1 — PM", "namn": "1", "type": "Dokument", "stem": "1 — PM"},
    ]
    edges = [{"source": "doc:1 — PM", "target": "stig engström",
              "label": "nämner", "count": 2}]
    out = to_cytoscape_elements(nodes, edges)
    person = out["nodes"][0]["data"]
    # Centernoder märks med ★-prefix (enda sättet att särskilja dem visuellt
    # i st-link-analysis, som bara stylar per typ-grupp).
    assert person == {"id": "stig engström", "label": "Person",
                      "name": "★ Stig Engström", "center": True}
    doc = out["nodes"][1]["data"]
    assert doc["label"] == "Dokument" and doc["stem"] == "1 — PM"
    edge = out["edges"][0]["data"]
    assert edge["label"] == "REL" and edge["name"] == "nämner (×2)"
    assert edge["source"] == "doc:1 — PM" and edge["target"] == "stig engström"
    assert edge["id"] == "e0"


def test_to_cytoscape_elements_skips_dangling_edges() -> None:
    from graph.viz import to_cytoscape_elements
    nodes = [{"id": "a", "namn": "A", "type": "Person"}]
    edges = [{"source": "a", "target": "ghost", "label": "x"}]
    out = to_cytoscape_elements(nodes, edges)
    assert out["edges"] == []


def test_to_cytoscape_elements_truncates_long_names() -> None:
    from graph.viz import to_cytoscape_elements
    namn = "12345 — " + "x" * 60
    nodes = [{"id": "doc:x", "namn": namn, "type": "Dokument", "stem": "x"}]
    name = to_cytoscape_elements(nodes, [])["nodes"][0]["data"]["name"]
    assert name.endswith("…") and len(name) == 45


def test_assemble_graph_doc_label_variants() -> None:
    from graph.viz import _doc_label
    assert _doc_label({"stem": "1 — PM", "nr": "1", "titel": "PM"}) == "1 — PM"
    assert _doc_label({"stem": "s", "nr": "", "titel": "Bara titel"}) == "Bara titel"
    assert _doc_label({"stem": "s", "nr": "42", "titel": ""}) == "42"
    assert _doc_label({"stem": "fallback", "nr": "", "titel": ""}) == "fallback"
