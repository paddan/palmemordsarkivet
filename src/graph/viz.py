"""Bygg ego-nätverk ur kunskapsgrafen för Streamlit-grafvyerna.

Neo4j-frågorna hålls tunna; transformationen rad→noder/kanter och
Cytoscape-konverteringen är rena funktioner (testbara utan Neo4j).
Renderingen sker med st-link-analysis i ``Utredning.py`` (svarsgrafen) och
``src/pages/3_Graf.py`` (grafsidan).
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Entitetstyper som går att söka på (Dokument söks inte direkt).
LABELS = ("Person", "Plats", "Organisation")

NODE_COLORS = {
    "Person": "#e4572e",
    "Plats": "#17bebb",
    "Organisation": "#ffc914",
    "Dokument": "#76b041",
}

# Material-ikoner per nodtyp för st-link-analysis-renderingen.
NODE_ICONS = {
    "Person": "person",
    "Plats": "place",
    "Organisation": "business",
    "Dokument": "description",
}


def resolve_password() -> str | None:
    """Neo4j-lösenord: NEO4J_PASSWORD i miljön, annars neo4j/.password
    (skapad av neo4j.sh). None om inget hittas."""
    pw = os.environ.get("NEO4J_PASSWORD")
    if pw:
        return pw
    f = ROOT / "neo4j" / ".password"
    if f.is_file():
        return f.read_text(encoding="utf-8").strip() or None
    return None


def connect(password: str, uri: str = "bolt://localhost:7687", user: str = "neo4j"):
    """Öppna en Neo4j-driver. Kastar om paketet saknas eller servern är nere."""
    from neo4j import GraphDatabase  # noqa: PLC0415 — kräver extras [graph]
    driver = GraphDatabase.driver(uri, auth=(user, password))
    driver.verify_connectivity()
    return driver


# ── Neo4j-frågor (tunna) ──────────────────────────────────────────────────────

_SEARCH_CYPHER = """
UNWIND $labels AS lbl
MATCH (n) WHERE lbl IN labels(n) AND toLower(n.namn) CONTAINS toLower($q)
RETURN head(labels(n)) AS label, n.namn AS namn, n.norm AS norm
ORDER BY size(n.namn) ASC LIMIT $limit
"""

_REL_CYPHER = """
MATCH (c)-[r:RELATERAR]-(m)
WHERE c.norm = $norm AND $label IN labels(c)
RETURN elementId(r) AS rid,
       startNode(r).norm AS s_norm, startNode(r).namn AS s_namn,
       head(labels(startNode(r))) AS s_label,
       endNode(r).norm AS e_norm, endNode(r).namn AS e_namn,
       head(labels(endNode(r))) AS e_label,
       r.typ AS typ
LIMIT $limit
"""

_DOC_CYPHER = """
MATCH (d:Dokument)-[:NÄMNER]->(c)
WHERE c.norm = $norm AND $label IN labels(c)
RETURN DISTINCT d.stem AS stem, d.nr AS nr, d.titel AS titel
LIMIT $limit
"""


def search_entities(session, query: str, limit: int = 20) -> list[dict]:
    """Sök entiteter vars namn innehåller ``query`` (skiftlägesokänsligt)."""
    rows = session.run(_SEARCH_CYPHER, labels=list(LABELS), q=query, limit=limit)
    out: list[dict] = []
    seen: set[tuple] = set()
    for r in rows:
        key = (r["label"], r["norm"])
        if key in seen:
            continue
        seen.add(key)
        out.append({"label": r["label"], "namn": r["namn"], "norm": r["norm"]})
    return out


def lookup_centers(session, names: list[str], per_name_limit: int = 10) -> list[dict]:
    """Slå upp svars-entiteter mot grafen → center-dicts för ``assemble_graph``.

    Bästa träff per namn: exakt namnmatch (skiftlägesokänslig) vinner, annars
    första träffen (``search_entities`` sorterar kortast namn först). Namn utan
    träff hoppas över; dubbletter (samma label+norm) dedupas."""
    centers: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for name in names:
        hits = search_entities(session, name, limit=per_name_limit)
        if not hits:
            continue
        exact = [h for h in hits if h["namn"].lower() == name.lower()]
        best = exact[0] if exact else hits[0]
        key = (best["label"], best["norm"])
        if key in seen:
            continue
        seen.add(key)
        centers.append(best)
    return centers


def fetch_ego(session, norm: str, label: str, limit: int = 80) -> tuple[list[dict], list[dict]]:
    """Hämta relationsrader (RELATERAR) och dokumentrader (NÄMNER) kring en nod."""
    rels = [dict(r) for r in session.run(_REL_CYPHER, norm=norm, label=label, limit=limit)]
    docs = [dict(r) for r in session.run(_DOC_CYPHER, norm=norm, label=label, limit=limit)]
    return rels, docs


def dedup_rels(rows: list[dict]) -> list[dict]:
    """Ta bort dubblerade relationsrader (samma relationsinstans hämtas en gång
    per utfällt center när båda ändpunkterna är center). Dedupas på ``rid``
    (Neo4j elementId); rader utan rid släpps igenom oförändrade."""
    seen: set = set()
    out: list[dict] = []
    for r in rows:
        rid = r.get("rid")
        if rid is not None:
            if rid in seen:
                continue
            seen.add(rid)
        out.append(r)
    return out


# ── Ren transformation + rendering ────────────────────────────────────────────

def _doc_label(d: dict) -> str:
    """Läsbar etikett för en dokumentnod: ``Nr — Titel``.

    Faller tillbaka på enbart numret eller titeln om någon del saknas, och
    sist på filstammen. Titeln saknades tidigare i nodnamnet (bara numret
    visades)."""
    nr = (d.get("nr") or "").strip()
    titel = (d.get("titel") or "").strip()
    if nr and titel:
        return f"{nr} — {titel}"
    return titel or nr or d["stem"]


def assemble_graph(
    centers: dict | list[dict], rel_rows: list[dict], doc_rows: list[dict]
) -> tuple[list[dict], list[dict]]:
    """Bygg nod- och kantlistor ur center(s) + relations-/dokumentrader.

    ``centers`` är en center-dict eller en lista av sådana (vid utfällning av
    flera noder). Centernoder får ``center=True`` så de kan framhävas.

    Noder: ``{id, namn, type}`` (dokument-noder har även ``stem`` för
    PDF-öppning, id-prefixat ``doc:`` så de inte krockar med entitets-norm).
    Varje dokumentrad får sin ``nämner``-kant mot sitt eget center via
    ``center_norm`` (faller tillbaka på första centret).
    Kanter: ``{source, target, label, count}``."""
    if isinstance(centers, dict):
        centers = [centers]
    nodes: dict[str, dict] = {}
    # Kollapsa identiska kanter (samma source/target/label från olika sidor)
    # till en, men räkna förekomsterna i ``count``.
    edges: dict[tuple[str, str, str], dict] = {}

    def add(nid: str, namn: str | None, typ: str, **extra) -> None:
        if nid not in nodes:
            nodes[nid] = {"id": nid, "namn": namn or nid, "type": typ, **extra}

    def add_edge(source: str, target: str, label: str) -> None:
        key = (source, target, label)
        e = edges.get(key)
        if e is None:
            edges[key] = {"source": source, "target": target, "label": label, "count": 1}
        else:
            e["count"] += 1

    for c in centers:
        add(c["norm"], c["namn"], c["label"], center=True)
    for r in rel_rows:
        add(r["s_norm"], r["s_namn"], r["s_label"])
        add(r["e_norm"], r["e_namn"], r["e_label"])
        add_edge(r["s_norm"], r["e_norm"], r.get("typ") or "")
    fallback_norm = centers[0]["norm"]
    for d in doc_rows:
        doc_id = "doc:" + d["stem"]
        add(doc_id, _doc_label(d), "Dokument", stem=d["stem"])
        add_edge(doc_id, d.get("center_norm") or fallback_norm, "nämner")
    return list(nodes.values()), list(edges.values())


def to_cytoscape_elements(nodes: list[dict], edges: list[dict]) -> dict:
    """Konvertera ``assemble_graph``-noder/kanter till Cytoscape-elementformat
    (st-link-analysis). Nodens typ blir Cytoscape-``label`` (stylinggrupp);
    alla kanter får gruppen ``REL`` med relationstexten i ``name`` så att en
    enda EdgeStyle med ``caption="name"`` visar texten. Centernoder märks med
    ``★``-prefix i namnet (NodeStyle kan bara styla per typ-grupp, inte per
    attribut). Kanter med saknade ändpunkter hoppas över (komponenten
    kraschar annars)."""
    cy_nodes = []
    for n in nodes:
        # Korta av långa namn (främst dokumenttitlar) i grafen så de inte
        # spränger nodlayouten; full titel finns kvar i sidopanelens lista.
        display = n["namn"] if len(n["namn"]) <= 45 else n["namn"][:44] + "…"
        name = f"★ {display}" if n.get("center") else display
        data = {"id": n["id"], "label": n["type"], "name": name}
        if n.get("stem"):
            data["stem"] = n["stem"]
        if n.get("center"):
            data["center"] = True
        cy_nodes.append({"data": data})
    present = {n["id"] for n in nodes}
    cy_edges = []
    for i, e in enumerate(edges):
        if e["source"] not in present or e["target"] not in present:
            continue
        base = e.get("label") or ""
        count = e.get("count", 1)
        name = f"{base} (×{count})" if count > 1 else base
        cy_edges.append({"data": {
            "id": f"e{i}", "source": e["source"], "target": e["target"],
            "label": "REL", "name": name,
        }})
    return {"nodes": cy_nodes, "edges": cy_edges}
