"""Dokumentlagret i Neo4j: ``Dokument``, ``Sida`` och ``Uppslag`` med kanter.

Läser ``document_meta`` ur state.db och sidantal ur textfilerna, bygger rader
(``graph.document_rows``) och skriver dem idempotent med MERGE. Lagret rör
aldrig kunskapsgrafens kanter (``NÄMNER``/``RELATERAR``): dokumentlagrets egna
kanter är ``HAR_SIDA``, ``INGÅR_I``, ``HÄNVISAR_TILL`` och ``DEL_AV``.

Allt Cypher är parametriserat — stammar innehåller ``—``, ``'`` och ``"``.
"""
from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import db as state_db  # noqa: E402
from graph import viz  # noqa: E402
from graph.document_rows import DocumentRows, build_document_rows, count_pages  # noqa: E402

DOCUMENT_CONSTRAINTS = [
    "CREATE CONSTRAINT dokument_stem IF NOT EXISTS "
    "FOR (d:Dokument) REQUIRE d.stem IS UNIQUE",
    "CREATE CONSTRAINT sida_id IF NOT EXISTS "
    "FOR (s:Sida) REQUIRE s.id IS UNIQUE",
    "CREATE CONSTRAINT uppslag_kod IF NOT EXISTS "
    "FOR (u:Uppslag) REQUIRE u.kod IS UNIQUE",
]

# Egenskaper som dokumentlagret äger på Dokument (stem, nr och titel behålls
# när ett dokument finns kvar i kunskapsgrafen men försvunnit ur arkivet).
_LAYER_PROPERTIES = ("arkiv", "bestallt", "upplagt", "anmarkning", "antal_sidor",
                     "dokumentdatum", "avkortat", "sidor")

_DOCUMENT_CYPHER = """
UNWIND $rows AS row
MERGE (d:Dokument {stem: row.stem})
SET d.arkiv = row.arkiv, d.nr = row.nr, d.titel = row.titel,
    d.bestallt = row.bestallt, d.upplagt = row.upplagt,
    d.anmarkning = row.anmarkning, d.antal_sidor = row.antal_sidor,
    d.dokumentdatum = row.dokumentdatum, d.avkortat = row.avkortat,
    d.sidor = row.sidor
"""

_PAGE_CYPHER = """
UNWIND $rows AS row
MATCH (d:Dokument {stem: row.stem})
MERGE (s:Sida {id: row.id})
SET s.stem = row.stem, s.sida = row.sida
MERGE (d)-[:HAR_SIDA]->(s)
"""

_CLEAR_EDGES_CYPHER = """
UNWIND $stems AS stem
MATCH (d:Dokument {stem: stem})-[r:INGÅR_I|HÄNVISAR_TILL|DEL_AV]->()
DELETE r
"""

_UPPSLAG_CYPHER = """
UNWIND $rows AS row
MATCH (d:Dokument {stem: row.stem})
MERGE (u:Uppslag {kod: row.kod})
MERGE (d)-[:INGÅR_I]->(u)
"""

_REFERENCE_CYPHER = """
UNWIND $rows AS row
MATCH (d:Dokument {stem: row.stem})
MATCH (t:Dokument {stem: row.target_stem})
MERGE (d)-[:HÄNVISAR_TILL]->(t)
"""

_PART_CYPHER = """
UNWIND $rows AS row
MATCH (d:Dokument {stem: row.stem})
MATCH (p:Dokument {stem: row.parent_stem})
MERGE (d)-[:DEL_AV]->(p)
"""

# Sidor vars dokument saknas eller vars sidnummer ligger utanför dokumentet.
_STALE_PAGES_CYPHER = """
MATCH (s:Sida)
OPTIONAL MATCH (d:Dokument {stem: s.stem})
WITH s, d WHERE d IS NULL OR s.sida > coalesce(d.sidor, 0)
DETACH DELETE s
RETURN count(*) AS n
"""

# Dokument som lagret en gång skrivit (har arkiv) men som inte längre finns.
_STALE_DOCUMENTS_CYPHER = """
MATCH (d:Dokument)
WHERE d.arkiv IS NOT NULL AND NOT d.stem IN $stems
RETURN d.stem AS stem
"""

_STALE_DOC_PAGES_CYPHER = """
UNWIND $stale AS stem
MATCH (s:Sida {stem: stem})
DETACH DELETE s
RETURN count(*) AS n
"""

_STALE_DOC_EDGES_CYPHER = """
UNWIND $stale AS stem
MATCH (d:Dokument {stem: stem})-[r:INGÅR_I|HÄNVISAR_TILL|DEL_AV]-()
DELETE r
"""

_STALE_DOC_DELETE_CYPHER = """
UNWIND $stale AS stem
MATCH (d:Dokument {stem: stem})
WHERE NOT (d)--()
DELETE d
RETURN count(*) AS n
"""

_STALE_DOC_CLEAR_CYPHER = (
    "UNWIND $stale AS stem\n"
    "MATCH (d:Dokument {stem: stem})\n"
    "WHERE d.arkiv IS NOT NULL\n"
    "REMOVE " + ", ".join(f"d.{p}" for p in _LAYER_PROPERTIES) + "\n"
    "RETURN count(*) AS n"
)

_ORPHAN_UPPSLAG_CYPHER = """
MATCH (u:Uppslag)
WHERE NOT (u)<-[:INGÅR_I]-()
DELETE u
RETURN count(*) AS n
"""


def resolve_password() -> str | None:
    """Neo4j-lösenord (miljön eller ``neo4j/.password``); se ``graph.viz``."""
    password: str | None = viz.resolve_password()
    return password


def _write(session, query: str, **params: Any) -> list[dict[str, Any]]:
    """Kör en parametriserad sats i en egen skrivtransaktion."""
    rows: list[dict[str, Any]] = session.execute_write(
        lambda tx: tx.run(query, **params).data())
    return rows


def _count(session, query: str, **params: Any) -> int:
    rows = _write(session, query, **params)
    return int(rows[0]["n"]) if rows else 0


def _chunks(rows: list, size: int):
    for offset in range(0, len(rows), size):
        yield rows[offset:offset + size]


def write_document_layer(
    session, rows: DocumentRows, *, batch: int, ctx
) -> dict[str, int]:
    """Skriv dokumentlagret i faser; varje batch i en egen skrivtransaktion."""
    for stmt in DOCUMENT_CONSTRAINTS:
        session.run(stmt).consume()

    for chunk in _chunks(rows.documents, batch):
        ctx.check_cancelled()
        _write(session, _DOCUMENT_CYPHER, rows=chunk)
    ctx.log(f"  {len(rows.documents)} dokument skrivna…")

    for chunk in _chunks(rows.pages, batch):
        ctx.check_cancelled()
        _write(session, _PAGE_CYPHER, rows=chunk)
    ctx.log(f"  {len(rows.pages)} sidor skrivna…")

    by_stem: dict[str, dict[str, list]] = {}
    for key, items in (("uppslag", rows.uppslag), ("references", rows.references),
                       ("parts", rows.parts)):
        for item in items:
            by_stem.setdefault(item["stem"], {"uppslag": [], "references": [],
                                              "parts": []})[key].append(item)
    for chunk in _chunks(rows.documents, batch):
        ctx.check_cancelled()
        stems = [d["stem"] for d in chunk]
        edge_batches = [
            (cypher, [r for s in stems for r in by_stem.get(s, {}).get(key, [])])
            for key, cypher in (("uppslag", _UPPSLAG_CYPHER),
                                ("references", _REFERENCE_CYPHER),
                                ("parts", _PART_CYPHER))
        ]

        def _rebuild(tx, stems=stems, edge_batches=edge_batches) -> None:
            # Radering och återskapande i en transaktion: en batch är atomär.
            tx.run(_CLEAR_EDGES_CYPHER, stems=stems).consume()
            for cypher, edge_rows in edge_batches:
                if edge_rows:
                    tx.run(cypher, rows=edge_rows).consume()

        session.execute_write(_rebuild)
    ctx.log("  uppslag, hänvisningar och delar skrivna…")

    ctx.check_cancelled()
    removed_pages = _count(session, _STALE_PAGES_CYPHER)
    live = [d["stem"] for d in rows.documents]
    stale = [r["stem"] for r in _write(session, _STALE_DOCUMENTS_CYPHER, stems=live)]
    removed_documents = cleared_documents = 0
    if stale:
        removed_pages += _count(session, _STALE_DOC_PAGES_CYPHER, stale=stale)
        _write(session, _STALE_DOC_EDGES_CYPHER, stale=stale)
        removed_documents = _count(session, _STALE_DOC_DELETE_CYPHER, stale=stale)
        cleared_documents = _count(session, _STALE_DOC_CLEAR_CYPHER, stale=stale)
        ctx.log(f"Städning: {len(stale)} dokument saknas i arkivet "
                f"(första: {stale[:3]})", level="debug")
    removed_uppslag = _count(session, _ORPHAN_UPPSLAG_CYPHER)

    return {
        "documents": len(rows.documents),
        "pages": len(rows.pages),
        "uppslag": len({u["kod"] for u in rows.uppslag}),
        "references": len(rows.references),
        "parts": len(rows.parts),
        "removed_pages": removed_pages,
        "removed_documents": removed_documents,
        "cleared_documents": cleared_documents,
        "removed_uppslag": removed_uppslag,
    }


def _ctx(context):
    if context is not None:
        return context
    from operations.context import ensure_terminal_context

    return ensure_terminal_context(None)


def _page_counts(stems: list[str], text_dir: Path, ctx) -> dict[str, int]:
    counts: dict[str, int] = {}
    missing: list[str] = []
    for stem in stems:
        path = text_dir / f"{stem}.txt"
        if path.is_file():
            counts[stem] = count_pages(path.read_text(encoding="utf-8"))
        else:
            counts[stem] = 0
            missing.append(stem)
    ctx.log(f"Sidantal: {len(counts) - len(missing)} textfiler lästa, "
            f"{len(missing)} saknas (första: {missing[:5]})", level="debug")
    return counts


def run_load_documents(
    *, uri: str, user: str, batch: int, text_dir: Path, context=None,
    writer: Callable[..., dict[str, int]] | None = None,
) -> int:
    """Läs in dokumentlagret i Neo4j. Returnerar exitkod."""
    from operations.exceptions import OperationFailed

    ctx = _ctx(context)
    password = resolve_password()
    if not password:
        raise OperationFailed("Sätt NEO4J_PASSWORD (eller starta Neo4j med neo4j.sh).")
    if batch < 1:
        raise OperationFailed("Batchstorleken måste vara minst 1.")

    from neo4j import GraphDatabase  # noqa: PLC0415 — kräver extras [graph]

    write = writer or write_document_layer
    with GraphDatabase.driver(uri, auth=(user, password)) as driver:
        # Misslyckas direkt om Neo4j är nere, före allt läsarbete.
        driver.verify_connectivity()
        conn = state_db.connect()
        state_db.init_schema(conn)
        try:
            metas = state_db.list_document_meta(conn)
        finally:
            conn.close()
        counts = _page_counts([m["pdf_stem"] for m in metas], Path(text_dir), ctx)
        rows = build_document_rows(metas, counts)
        with driver.session() as session:
            result = write(session, rows, batch=batch, ctx=ctx)
    ctx.log(
        f"Dokumentlager: {result['documents']} dokument, {result['pages']} sidor, "
        f"{result['uppslag']} uppslag, {result['references']} hänvisningar "
        f"({rows.unresolved_references} utan mål), {result['parts']} delar; "
        f"borttaget: {result['removed_pages']} sidor, "
        f"{result['removed_documents']} dokument, "
        f"{result['removed_uppslag']} uppslag "
        f"({result['cleared_documents']} dokument fick lagret rensat)."
    )
    return 0
