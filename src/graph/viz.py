"""Bygg och rendera ego-nätverk ur kunskapsgrafen för webui-grafsidan.

Neo4j-frågorna hålls tunna; transformationen rad→noder/kanter och
pyvis-renderingen är rena funktioner (testbara utan Neo4j). Sidan
``src/pages/1_Graf.py`` kopplar ihop dem med Streamlit.
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
        add(doc_id, d.get("nr") or d["stem"], "Dokument", stem=d["stem"])
        add_edge(doc_id, d.get("center_norm") or fallback_norm, "nämner")
    return list(nodes.values()), list(edges.values())


def build_pyvis_html(nodes: list[dict], edges: list[dict], height: str = "620px") -> str:
    """Rendera noder/kanter till fristående interaktiv pyvis-HTML."""
    from pyvis.network import Network  # noqa: PLC0415

    net = Network(height=height, width="100%", directed=True,
                  bgcolor="#ffffff", font_color="#222222")
    net.barnes_hut(gravity=-8000, spring_length=120)
    for n in nodes:
        is_center = bool(n.get("center"))
        net.add_node(
            n["id"], label=n["namn"],
            color=NODE_COLORS.get(n["type"], "#999999"),
            shape="box" if n["type"] == "Dokument" else "dot",
            title=f'{n["type"]}: {n["namn"]}',
            size=28 if is_center else 16,
            borderWidth=4 if is_center else 1,
        )
    present = {n["id"] for n in nodes}
    for e in edges:
        if e["source"] not in present or e["target"] not in present:
            continue
        base = e.get("label") or ""
        count = e.get("count", 1)
        # Visa antalet förekomster när relationen återkommer i flera dokument.
        label = f"{base} (×{count})" if count > 1 else base
        # Dämpad, takad bredd så att en relation med många källor inte blir
        # en oläslig klump (1 → 1.5, växer långsamt, max 5).
        width = min(1.0 + 0.5 * (count - 1), 5.0)
        net.add_edge(e["source"], e["target"], label=label, title=label,
                     width=width)
    return net.generate_html(notebook=False)
