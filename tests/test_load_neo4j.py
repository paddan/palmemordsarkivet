"""Tester för load_neo4j — normalisering och radbygge (ingen Neo4j krävs)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from graph.load_neo4j import build_rows, normalize_name


def test_normalize_name_collapses_case_and_whitespace() -> None:
    assert normalize_name("  Stig   ENGSTRÖM ") == "stig engström"


def test_build_rows_mentions_and_relations() -> None:
    payload = {
        "entiteter": [
            {"typ": "person", "namn": "Stig Engström"},
            {"typ": "plats", "namn": "Dekorima"},
        ],
        "relationer": [
            {"fran": "Stig Engström", "typ": "sågs vid", "till": "Dekorima"},
            {"fran": "Okänd Person", "typ": "känner", "till": "Dekorima"},  # endpoint saknas
        ],
    }
    mentions, relations = build_rows(
        "281 — Förhör — 2022 — D-1 — 2", 4, payload)
    assert {(m["label"], m["norm"]) for m in mentions} == {
        ("Person", "stig engström"), ("Plats", "dekorima")}
    assert mentions[0]["stem"] == "281 — Förhör — 2022 — D-1 — 2"
    assert mentions[0]["nr"] == "281"
    assert mentions[0]["sida"] == 4
    # Relationen med okänd endpoint filtreras bort
    assert len(relations) == 1
    assert relations[0]["fran_label"] == "Person"
    assert relations[0]["till_label"] == "Plats"
    assert relations[0]["typ"] == "sågs vid"


def test_write_batches_groups_by_label() -> None:
    from unittest.mock import MagicMock
    from graph.load_neo4j import write_mentions

    session = MagicMock()
    mentions = [
        {"stem": "a", "nr": "1", "titel": "t", "sida": 1,
         "label": "Person", "norm": "x", "namn": "X"},
        {"stem": "a", "nr": "1", "titel": "t", "sida": 1,
         "label": "Plats", "norm": "y", "namn": "Y"},
        {"stem": "a", "nr": "1", "titel": "t", "sida": 2,
         "label": "Person", "norm": "z", "namn": "Z"},
    ]
    write_mentions(session, mentions)
    # En run per label
    assert session.run.call_count == 2
    queries = [c.args[0] for c in session.run.call_args_list]
    assert any(":Person" in q for q in queries)
    assert any(":Plats" in q for q in queries)
    # Person-batchen innehåller båda person-raderna
    person_call = next(c for c in session.run.call_args_list if ":Person" in c.args[0])
    assert len(person_call.kwargs["rows"]) == 2


# --- namnkanonisering (entity resolution light) ----------------------------

def test_normalize_name_inverts_surname_first() -> None:
    from graph.load_neo4j import normalize_name
    assert normalize_name("Andersson, Jan") == "jan andersson"
    assert normalize_name("Nieminen, Yvonne Anita") == "yvonne anita nieminen"


def test_normalize_name_keeps_address_with_digits() -> None:
    from graph.load_neo4j import normalize_name
    # Siffror → trolig adress, invertera inte
    assert normalize_name("Sveavägen 44, Stockholm") == "sveavägen 44, stockholm"


def test_normalize_name_keeps_multi_comma() -> None:
    from graph.load_neo4j import normalize_name
    assert normalize_name("a, b, c") == "a, b, c"


def test_normalize_name_plain_unchanged() -> None:
    from graph.load_neo4j import normalize_name
    assert normalize_name("  Stig   ENGSTRÖM ") == "stig engström"


def test_display_name_inverts_but_keeps_case() -> None:
    from graph.load_neo4j import display_name
    assert display_name("Nieminen, Yvonne Anita") == "Yvonne Anita Nieminen"
    assert display_name("Stig Engström") == "Stig Engström"


def test_build_rows_merges_inverted_person_with_relation() -> None:
    from graph.load_neo4j import build_rows
    payload = {
        "entiteter": [
            {"typ": "person", "namn": "Palme, Olof"},
            {"typ": "person", "namn": "Holmér, Hans"},
        ],
        "relationer": [
            {"fran": "Palme, Olof", "typ": "kände", "till": "Holmér, Hans"},
        ],
    }
    mentions, relations = build_rows("1 — x", 1, payload)
    norms = {m["norm"] for m in mentions}
    assert norms == {"olof palme", "hans holmér"}
    # Visningsnamnet ska vara den inverterade, läsbara formen
    assert {m["namn"] for m in mentions} == {"Olof Palme", "Hans Holmér"}
    # Relationen matchar trots inverterad form i payloaden
    assert len(relations) == 1
    assert relations[0]["fran_norm"] == "olof palme"
    assert relations[0]["till_norm"] == "hans holmér"


# --- dokument-scopad efternamnsuppslagning ---------------------------------

def test_build_surname_map_unique_only() -> None:
    from graph.load_neo4j import build_surname_map
    entries = [
        {"pdf_stem": "A", "page_num": 1, "payload": {"entiteter": [
            {"typ": "person", "namn": "Stig Engström"}], "relationer": []}},
        {"pdf_stem": "A", "page_num": 2, "payload": {"entiteter": [
            {"typ": "person", "namn": "Olof Palme"},
            {"typ": "person", "namn": "Lisbeth Palme"}], "relationer": []}},
    ]
    m = build_surname_map(entries)
    assert m["A"]["engström"] == "Stig Engström"   # entydig → mappas
    assert "palme" not in m["A"]                    # två Palme → utesluts


def test_build_surname_map_scoped_per_document() -> None:
    from graph.load_neo4j import build_surname_map
    entries = [
        {"pdf_stem": "A", "page_num": 1, "payload": {"entiteter": [
            {"typ": "person", "namn": "Olof Palme"}], "relationer": []}},
        {"pdf_stem": "B", "page_num": 1, "payload": {"entiteter": [
            {"typ": "person", "namn": "Mårten Palme"}], "relationer": []}},
    ]
    m = build_surname_map(entries)
    assert m["A"]["palme"] == "Olof Palme"    # entydig inom A
    assert m["B"]["palme"] == "Mårten Palme"  # entydig inom B (scopat)


def test_build_rows_resolves_bare_surname() -> None:
    from graph.load_neo4j import build_rows, build_surname_map
    entries = [
        {"pdf_stem": "A", "page_num": 1, "payload": {"entiteter": [
            {"typ": "person", "namn": "Stig Engström"}], "relationer": []}},
        {"pdf_stem": "A", "page_num": 2, "payload": {"entiteter": [
            {"typ": "person", "namn": "Engström"},
            {"typ": "plats", "namn": "Dekorima"}],
            "relationer": [{"fran": "Engström", "typ": "sågs vid", "till": "Dekorima"}]}},
    ]
    smap = build_surname_map(entries)
    mentions, relations = build_rows("A", 2, entries[1]["payload"], smap["A"])
    person = [m for m in mentions if m["label"] == "Person"][0]
    assert person["norm"] == "stig engström"   # bart efternamn → fullnamn
    assert person["namn"] == "Stig Engström"
    assert len(relations) == 1
    assert relations[0]["fran_norm"] == "stig engström"  # relationen följer med


def test_build_rows_keeps_ambiguous_surname_separate() -> None:
    from graph.load_neo4j import build_rows, build_surname_map
    payload = {"entiteter": [
        {"typ": "person", "namn": "Olof Palme"},
        {"typ": "person", "namn": "Lisbeth Palme"},
        {"typ": "person", "namn": "Palme"}], "relationer": []}
    entries = [{"pdf_stem": "A", "page_num": 1, "payload": payload}]
    smap = build_surname_map(entries)
    mentions, _ = build_rows("A", 1, payload, smap.get("A", {}))
    norms = {m["norm"] for m in mentions}
    assert {"palme", "olof palme", "lisbeth palme"} <= norms  # bart Palme eget


def test_build_rows_backward_compatible_without_map() -> None:
    from graph.load_neo4j import build_rows
    payload = {"entiteter": [{"typ": "person", "namn": "Engström"}], "relationer": []}
    mentions, _ = build_rows("A", 1, payload)   # inget surname_map → oförändrat
    assert mentions[0]["norm"] == "engström"
