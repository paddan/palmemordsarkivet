"""Tester för graph.viz — subgrafbygge och pyvis-rendering (ingen Neo4j krävs)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from graph.viz import assemble_graph, build_pyvis_html, dedup_rels, search_entities


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


def test_build_pyvis_html_contains_node_labels() -> None:
    nodes = [{"id": "a", "namn": "Stig Engström", "type": "Person"},
             {"id": "b", "namn": "Lars Jepsson", "type": "Person"}]
    edges = [{"source": "a", "target": "b", "label": "igenkände"}]
    html = build_pyvis_html(nodes, edges)
    # pyvis JSON-kodar noddata med ensure_ascii (ö → ö); browsern avkodar.
    # Verifiera via accent-fria delar att båda noderna renderats.
    assert "Engstr" in html and "Lars Jepsson" in html
    assert len(html) > 100


def test_build_pyvis_html_skips_dangling_edges() -> None:
    nodes = [{"id": "a", "namn": "A", "type": "Person"}]
    edges = [{"source": "a", "target": "ghost", "label": "x"}]
    html = build_pyvis_html(nodes, edges)  # får inte krascha
    assert "A" in html


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
