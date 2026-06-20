"""Facetterad sökning — filtrera träffar på entiteter ur kunskapsgrafen.

Entitetsextraktionen (graph/extract_entities.py) lägger person-, plats- och
organisationsnamn per sida i ``doc_entities``. Den här modulen aggregerar dem
till facetter och låter UI:t begränsa sökträffar till dokument som nämner en
vald entitet. Ren logik utan Streamlit.
"""

from __future__ import annotations

import sqlite3

import db as _state_db

# Facettyper vi exponerar, i visningsordning.
FACET_TYPES = ("person", "plats", "organisation")


def source_to_stem(source: str) -> str:
    """``"100 — Skandia.txt"`` → ``"100 — Skandia"`` (LanceDB-source → pdf_stem)."""
    return source[:-4] if source.endswith(".txt") else source


def _iter_entities(conn: sqlite3.Connection):
    """Yield (pdf_stem, typ, namn) för varje extraherad entitet."""
    for row in _state_db.iter_doc_entities(conn):
        stem = row["pdf_stem"]
        for ent in row["payload"].get("entiteter", []):
            typ = str(ent.get("typ") or "").strip().lower()
            namn = str(ent.get("namn") or "").strip()
            if typ and namn:
                yield stem, typ, namn


def entity_facets(conn: sqlite3.Connection) -> dict[str, list[tuple[str, int]]]:
    """Facetter per typ: lista av (namn, antal_dokument), mest förekommande först.

    Namn slås ihop skiftlägesokänsligt; visningsnamnet blir den variant som
    setts flest gånger. Antalet räknar distinkta ``pdf_stem`` (dokument)."""
    # typ -> casefold-nyckel -> {stems: set, variants: {namn: count}}
    buckets: dict[str, dict[str, dict]] = {t: {} for t in FACET_TYPES}
    for stem, typ, namn in _iter_entities(conn):
        if typ not in buckets:
            continue
        key = namn.casefold()
        entry = buckets[typ].setdefault(key, {"stems": set(), "variants": {}})
        entry["stems"].add(stem)
        entry["variants"][namn] = entry["variants"].get(namn, 0) + 1

    facets: dict[str, list[tuple[str, int]]] = {}
    for typ in FACET_TYPES:
        items = []
        for entry in buckets[typ].values():
            display = max(entry["variants"].items(), key=lambda kv: kv[1])[0]
            items.append((display, len(entry["stems"])))
        items.sort(key=lambda kv: (-kv[1], kv[0].casefold()))
        facets[typ] = items
    return facets


def entity_stem_index(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """Skiftlägesokänsligt index ``casefold(namn) -> {pdf_stem, …}``.

    Byggs en gång och cachas av UI:t så ``doc_entities`` inte parsas om per
    vald facett och sökning."""
    index: dict[str, set[str]] = {}
    for stem, _typ, namn in _iter_entities(conn):
        index.setdefault(namn.casefold(), set()).add(stem)
    return index


def stems_with_entity(conn: sqlite3.Connection, namn: str) -> set[str]:
    """pdf_stems som nämner ``namn`` (skiftlägesokänsligt)."""
    target = namn.strip().casefold()
    if not target:
        return set()
    return {
        stem
        for stem, _typ, ent_namn in _iter_entities(conn)
        if ent_namn.casefold() == target
    }


def filter_hits_by_stems(hits: list[dict], stems: set[str]) -> list[dict]:
    """Behåll bara träffar vars source-stem finns i ``stems``."""
    return [h for h in hits if source_to_stem(str(h.get("source") or "")) in stems]


def sources_where_clause(stems: set[str]) -> str | None:
    """SQL-where för LanceDB-prefilter på ``source``-kolumnen, eller None.

    Låter vektorsökningen begränsas till de aktuella dokumenten *innan* topp-K
    väljs — annars skulle dokument utanför topp-K aldrig kunna ytas av en
    facett. Enkelfnuttar i filnamn escapas (``'`` → ``''``)."""
    if not stems:
        return None
    quoted = ",".join(
        "'" + (stem + ".txt").replace("'", "''") + "'" for stem in sorted(stems)
    )
    return f"source IN ({quoted})"
