"""Ladda doc_entities-payloads från state.db till Neo4j.

Idempotent: allt skrivs med MERGE, så omkörning ger samma graf.

Kör (kräver Neo4j igång, se neo4j/docker-compose.yml):
    NEO4J_PASSWORD=... python -m graph.load_neo4j [--batch 1000]
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import db as state_db  # noqa: E402

LABELS = {"person": "Person", "plats": "Plats", "organisation": "Organisation"}
GRAPH_OWNER = "palmemordsarkivet"

_WS_RE = re.compile(r"\s+")


def _invert_surname_first(name: str) -> str:
    """'Efternamn, Förnamn' → 'Förnamn Efternamn'.

    Slår bara till för enkla tvådelade namn: exakt ett kommatecken, båda
    sidor icke-tomma och inga siffror (annars trolig adress/lista — lämnas)."""
    parts = name.split(",")
    if len(parts) == 2:
        a, b = parts[0].strip(), parts[1].strip()
        if a and b and not any(c.isdigit() for c in name):
            return f"{b} {a}"
    return name


def normalize_name(name: str) -> str:
    """Unikhetsnyckel: kanoniserar 'Efternamn, Förnamn', gemener, kollapsad
    whitespace. Inversionen gör att 'Andersson, Jan' och 'Jan Andersson' blir
    samma nod istället för två."""
    return _WS_RE.sub(" ", _invert_surname_first(name)).strip().lower()


def display_name(name: str) -> str:
    """Visningsnamn: samma inversion som normalize_name men behåller versaler."""
    return _WS_RE.sub(" ", _invert_surname_first(name)).strip()


def _doc_meta(stem: str) -> dict:
    parts = [p.strip() for p in stem.split(" — ")]
    return {"stem": stem, "nr": parts[0] if parts else stem,
            "titel": parts[1] if len(parts) > 1 else ""}


def build_surname_map(entries: list[dict]) -> dict[str, dict[str, str]]:
    """Bygg per-dokument-uppslag efternamn → fullt visningsnamn.

    Tar alla doc_entities-rader (``iter_doc_entities``) och samlar, per
    ``pdf_stem``, flerordiga personnamn. Ett efternamn (sista token) tas bara
    med om *exakt ett* fullnamn i dokumentet slutar på det — då är det entydigt
    vem ett ensamt efternamn syftar på. Har dokumentet både "Olof Palme" och
    "Lisbeth Palme" utelämnas "palme" och inget slås ihop.

    Scopet är medvetet per dokument: olika dokument kan ha olika ende-Palme."""
    # stem → efternamn → {full_norm: full_display}
    by_stem: dict[str, dict[str, dict[str, str]]] = {}
    for e in entries:
        stem = e["pdf_stem"]
        for ent in e["payload"].get("entiteter", []):
            if ent.get("typ") != "person":
                continue
            raw = ent.get("namn", "")
            norm = normalize_name(raw)
            if not norm or " " not in norm:
                continue  # ensamt efternamn ger inget uppslag — bara fullnamn
            surname = norm.rsplit(" ", 1)[-1]
            by_stem.setdefault(stem, {}).setdefault(surname, {})[norm] = display_name(raw)
    out: dict[str, dict[str, str]] = {}
    for stem, surnames in by_stem.items():
        unique = {sn: next(iter(fulls.values()))
                  for sn, fulls in surnames.items() if len(fulls) == 1}
        if unique:
            out[stem] = unique
    return out


def _resolve_person_norm(raw: str, surname_map: dict[str, str]) -> tuple[str, str]:
    """(norm, visningsnamn) för ett namn, med efternamnsuppslag.

    Ett ensamt efternamn (en token) som finns i ``surname_map`` ersätts med
    dokumentets entydiga fullnamn — annars oförändrat."""
    norm = normalize_name(raw)
    if norm and " " not in norm and norm in surname_map:
        full = surname_map[norm]
        return normalize_name(full), full
    return norm, display_name(raw)


def build_rows(
    stem: str, page_num: int, payload: dict,
    surname_map: dict[str, str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Översätt en sid-payload till mention- och relationsrader för Cypher.

    ``surname_map`` (dokumentets efternamn→fullnamn från ``build_surname_map``)
    löser upp ensamma efternamn till fullnamn så att t.ex. "Engström" och
    "Stig Engström" blir samma nod. Relationer vars endpoints inte finns bland
    sidans entiteter hoppas över (LLM:en hittade på eller stavade annorlunda)."""
    surname_map = surname_map or {}
    meta = _doc_meta(stem)
    norm_to_label: dict[str, str] = {}
    mentions: list[dict] = []
    for e in payload.get("entiteter", []):
        label = LABELS.get(e.get("typ", ""))
        if not label:
            continue
        if label == "Person":
            norm, disp = _resolve_person_norm(e["namn"], surname_map)
        else:
            norm, disp = normalize_name(e["namn"]), display_name(e["namn"])
        if not norm:
            continue
        norm_to_label[norm] = label
        mentions.append({**meta, "sida": page_num, "label": label,
                         "norm": norm, "namn": disp})

    relations: list[dict] = []
    for r in payload.get("relationer", []):
        fn, _ = _resolve_person_norm(r["fran"], surname_map)
        tn, _ = _resolve_person_norm(r["till"], surname_map)
        if fn not in norm_to_label or tn not in norm_to_label:
            continue
        relations.append({"stem": stem, "sida": page_num, "typ": r["typ"],
                          "fran_norm": fn, "fran_label": norm_to_label[fn],
                          "till_norm": tn, "till_label": norm_to_label[tn]})
    return mentions, relations


CONSTRAINTS = [
    "CREATE CONSTRAINT dokument_stem IF NOT EXISTS "
    "FOR (d:Dokument) REQUIRE d.stem IS UNIQUE",
    *(f"CREATE CONSTRAINT {lbl.lower()}_norm IF NOT EXISTS "
      f"FOR (n:{lbl}) REQUIRE n.norm IS UNIQUE" for lbl in LABELS.values()),
]

_MENTION_CYPHER = """
UNWIND $rows AS row
MERGE (d:Dokument {{stem: row.stem}})
  ON CREATE SET d.nr = row.nr, d.titel = row.titel
MERGE (e:{label} {{norm: row.norm}})
  ON CREATE SET e.namn = row.namn
MERGE (d)-[r:NÄMNER {{sida: row.sida, graph_owner: 'palmemordsarkivet'}}]->(e)
"""

_RELATION_CYPHER = """
UNWIND $rows AS row
MATCH (a:{fl} {{norm: row.fran_norm}})
MATCH (b:{tl} {{norm: row.till_norm}})
MERGE (a)-[r:RELATERAR {{typ: row.typ, stem: row.stem, sida: row.sida,
                         graph_owner: 'palmemordsarkivet'}}]->(b)
"""


def write_mentions(session, mentions: list[dict]) -> None:
    """Skriv mention-rader, en batch per entitetslabel (Cypher kan inte
    parameterisera labels)."""
    by_label: dict[str, list[dict]] = {}
    for m in mentions:
        by_label.setdefault(m["label"], []).append(m)
    for label, rows in by_label.items():
        session.run(_MENTION_CYPHER.format(label=label), rows=rows)


def write_relations(session, relations: list[dict]) -> None:
    """Skriv relationsrader, en batch per (från-label, till-label)-par."""
    by_pair: dict[tuple[str, str], list[dict]] = {}
    for r in relations:
        by_pair.setdefault((r["fran_label"], r["till_label"]), []).append(r)
    for (fl, tl), rows in by_pair.items():
        session.run(_RELATION_CYPHER.format(fl=fl, tl=tl), rows=rows)


def _ctx(context):
    """Returnera ``context`` eller en terminal-context för förgrundskörning."""
    if context is not None:
        return context
    from operations.context import ensure_terminal_context

    return ensure_terminal_context(None)


def run_load_graph(*, uri: str, user: str, batch: int, context=None) -> int:
    """Ladda doc_entities till Neo4j. Returnerar exitkod."""
    ctx = _ctx(context)
    password = os.environ.get("NEO4J_PASSWORD")
    if not password:
        # Samma katalog som operations/neo4j.py (NEO4J_DIR): <root>/neo4j/.
        # Lösenordet används bara lokalt i denna funktion — env lämnas orörd.
        password_file = ROOT / "neo4j" / ".password"
        if password_file.exists():
            password = password_file.read_text(encoding="utf-8").strip()
            ctx.log(f"Läste Neo4j-lösenord från {password_file}")
    if not password:
        ctx.log("Sätt NEO4J_PASSWORD.", level="error")
        return 1

    from neo4j import GraphDatabase  # noqa: PLC0415 — kräver extras [graph]

    conn = state_db.connect()
    state_db.init_schema(conn)

    from graph.review import build_reviewed_rows

    try:
        entries, decisions = state_db.read_graph_review_snapshot(conn)
        name_rules = state_db.list_graph_name_rules(conn)
    finally:
        conn.close()
    mentions, relations = build_reviewed_rows(entries, decisions, name_rules)
    n_pages, n_mentions, n_relations = len(entries), len(mentions), len(relations)
    if batch < 1:
        from operations.exceptions import OperationFailed
        raise OperationFailed("Batchstorleken måste vara minst 1.")
    with GraphDatabase.driver(uri, auth=(user, password)) as driver, \
            driver.session() as session:
        for stmt in CONSTRAINTS:
            session.run(stmt)
        for rows, writer in ((mentions, write_mentions), (relations, write_relations)):
            for offset in range(0, len(rows), batch):
                ctx.check_cancelled()
                writer(session, rows[offset:offset + batch])
                ctx.log(f"  {min(offset + batch, len(rows))}/{len(rows)} rader laddade…")
    ctx.log(f"Klart: {n_pages} sidor → {n_mentions} omnämnanden, "
            f"{n_relations} relationer.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uri", default=os.environ.get("NEO4J_URI", "bolt://localhost:7687"))
    ap.add_argument("--user", default=os.environ.get("NEO4J_USER", "neo4j"))
    ap.add_argument("--batch", type=int, default=1000,
                    help="antal rader per skrivbatch (default: 1000)")
    args = ap.parse_args()

    return run_load_graph(uri=args.uri, user=args.user, batch=args.batch)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nAvbrutet.", file=sys.stderr)
        sys.exit(130)
