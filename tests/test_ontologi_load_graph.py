"""Tester för inläsningen av mordnattens observationer i Neo4j (``ontologi.load_graph``).

Fake-session-testerna spelar in varje sats och dess parametrar utan Neo4j;
Neo4j-testerna körs bara mot en tom testdatabas (``NEO4J_TEST_URI``).
"""
from __future__ import annotations

import re

import pytest

from graph.document_rows import build_document_rows
from graph.load_documents import write_document_layer
from ontologi import load_graph
from ontologi.config import load_config
from operations.context import OperationContext, ProgressSink

_FORBJUDNA = ("NÄMNER", "RELATERAR", "HAR_SIDA", "INGÅR_I", "HÄNVISAR_TILL", "DEL_AV")
_STAM = "A1 — Förhör med ' och \" och —"


class _Sink(ProgressSink):
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def write_log(self, message: str, level: str = "info") -> None:
        self.lines.append((level, message))


def _ctx(sink: _Sink | None = None) -> OperationContext:
    return OperationContext(sink=sink or _Sink(), cancel_requested=lambda: False)


@pytest.fixture(scope="module")
def cfg():
    return load_config()


def _obs(index: int, *, namn: str | None = "Anna Berg", maskad: bool = False,
         handelse: str | None = "skotten", kartplats: str | None = "Tunnelgatan / trapporna",
         utdrag: str = "Jag såg en man springa uppför trapporna vid Tunnelgatan.",
         observerade: list | None = None) -> dict:
    return {
        "index": index,
        "tid_text": "strax efter halv tolv",
        "tid_fran": "23:21",
        "tid_till": "23:22",
        "plats": {"namn_ra": "trapporna vid Tunnelgatan", "kartplats_namn": kartplats,
                  "lat": 59.33734 if kartplats else None,
                  "lon": 18.06519 if kartplats else None,
                  "match": "fuzzy" if kartplats else "none"},
        "handelse_id": handelse,
        "vittne": {"namn": namn, "maskad": maskad, "kategori": "namn" if maskad else "okänd",
                   "roll": "vittne"},
        "observerade": observerade if observerade is not None else [],
        "utdrag": utdrag,
        "osakerhet": None,
    }


def _person(kan_vara: tuple[str, ...] = ("garningsmannen",)) -> dict:
    return {"beskrivning": "lång man i mörk rock",
            "signalement": [{"typ": "längd", "varde_text": "180–185 cm",
                             "min": 180, "max": 185, "enhet": "cm"}],
            "kan_vara": list(kan_vara)}


def _ext(stem: str, sida: int, hash_: str, obs: list[dict]) -> dict:
    return {"pdf_stem": stem, "sida": sida, "text_hash": hash_, "schemaversion": 1,
            "profile": "p", "model": "m", "raw": "{}",
            "parsed": {"observationer": obs}, "n_observationer": len(obs),
            "extracted_at": "2026-11-27T00:00:00"}


# ── Rena hjälpare ────────────────────────────────────────────────────────────

def test_observation_id_shape() -> None:
    assert load_graph.observation_id("S", 3, 2, "abcdef0123456789") == "S#3#2#abcdef012345"


def test_slug() -> None:
    assert load_graph.slug("Tunnelgatan / trapporna") == "tunnelgatan-trapporna"
    assert load_graph.slug("  Sveavägen / Adolf Fredriks kyrkogata ") == (
        "sveavägen-adolf-fredriks-kyrkogata")


def test_constraints_cover_every_label() -> None:
    text = " ".join(load_graph.ONTOLOGY_CONSTRAINTS)
    for label in ("Observation", "Vittne", "MaskadUppgift", "Händelse", "Kartplats",
                  "ObserveradPerson", "Egenskap", "Nyckelperson"):
        assert f":{label})" in text
    assert all("IF NOT EXISTS" in c for c in load_graph.ONTOLOGY_CONSTRAINTS)


def test_extraction_hash_covers_text_schema_and_content() -> None:
    bas = _ext("S", 1, "a" * 64, [_obs(0)])
    eh = load_graph.extraction_hash(bas)
    assert re.fullmatch(r"[0-9a-f]{64}", eh)
    assert load_graph.extraction_hash(_ext("S", 1, "a" * 64, [_obs(0)])) == eh
    omvand = dict(bas, parsed={"observationer": [dict(reversed(list(_obs(0).items())))]})
    assert load_graph.extraction_hash(omvand) == eh
    assert load_graph.extraction_hash(dict(bas, text_hash="b" * 64)) != eh
    assert load_graph.extraction_hash(dict(bas, schemaversion=2)) != eh
    assert load_graph.extraction_hash(_ext("S", 1, "a" * 64, [_obs(0, namn="Bo Ek")])) != eh
    # Modell och profil ingår inte direkt: samma svar ger samma hash.
    assert load_graph.extraction_hash(dict(bas, model="annan")) == eh


# ── Falsk session ────────────────────────────────────────────────────────────

class _Result:
    def __init__(self, rows):
        self._rows = rows

    def data(self):
        return self._rows

    def consume(self):
        return None


class FakeSession:
    """Spelar in satser; sidkontrollen svarar att ``Sida`` finns."""

    def __init__(self, *, sida_finns: bool = True):
        self.calls: list[tuple[str, dict]] = []
        self.tx_of: list[int | None] = []
        self._tx: int | None = None
        self.transactions = 0
        self.sida_finns = sida_finns

    def run(self, query, **params):
        self.calls.append((query, params))
        self.tx_of.append(self._tx)
        if "AS finns" in query:
            return _Result([{"finns": 1 if self.sida_finns else 0}])
        if "AS n" in query:
            return _Result([{"n": 0}])
        return _Result([])

    def _tx_run(self, fn):
        self.transactions += 1
        self._tx = self.transactions
        try:
            return fn(self)
        finally:
            self._tx = None

    execute_write = _tx_run
    execute_read = _tx_run


def _full_ext() -> dict:
    return _ext(_STAM, 2, "f" * 64, [
        _obs(0, observerade=[_person()]),
        _obs(1, namn=None, maskad=True, kartplats=None, handelse=None),
        _obs(2, namn=None, maskad=False),
    ])


def test_fake_statements_are_parameterised_and_never_touch_other_layers(cfg) -> None:
    session = FakeSession()
    load_graph.load_ontology(session, [_full_ext()], cfg, ctx=_ctx())
    assert session.calls
    for query, _params in session.calls:
        # Inga värden ur extraktionen eller konfigurationen inbakade i satsen.
        assert "A1 —" not in query and "Anna" not in query and "Tunnelgatan" not in query
        assert "'skotten'" not in query and "garningsmannen" not in query
        for forbjuden in _FORBJUDNA:
            assert forbjuden not in query, (forbjuden, query)
        # Sida läses bara, skapas eller ändras aldrig.
        for line in query.splitlines():
            if ":Sida" in line:
                assert line.strip().startswith(("MATCH", "OPTIONAL MATCH")), line
        assert not re.search(r"\b(SET|REMOVE)\s+s\.", query)


def test_fake_one_transaction_per_page(cfg) -> None:
    session = FakeSession()
    ext2 = _ext(_STAM, 3, "e" * 64, [_obs(0)])
    load_graph.load_ontology(session, [_full_ext(), ext2], cfg, ctx=_ctx())
    page_tx = {session.tx_of[i] for i, (q, p) in enumerate(session.calls)
               if p.get("sid") in (f"{_STAM}#2", f"{_STAM}#3")}
    assert len(page_tx) == 2 and None not in page_tx
    # Varje sats för en sida går i sidans transaktion.
    for i, (_q, p) in enumerate(session.calls):
        if p.get("sid") == f"{_STAM}#2":
            assert session.tx_of[i] == min(page_tx)


def test_fake_write_page_rows(cfg) -> None:
    session = FakeSession()
    counts = load_graph.write_page(session, _full_ext())
    assert counts["nya"] == 3 and counts["observationer"] == 3
    params = [p for _q, p in session.calls]
    obs_rows = next(p["rows"] for q, p in session.calls if "MERGE (o:Observation" in q)
    ids = [r["id"] for r in obs_rows]
    eh = load_graph.extraction_hash(_full_ext())
    assert ids == [load_graph.observation_id(_STAM, 2, i, eh) for i in range(3)]
    assert obs_rows[0]["extraktion_hash"] == eh
    assert obs_rows[0]["text_hash"] == "f" * 64
    # plats_ra sparas alltid, även när platsen matchats mot en kartplats.
    assert obs_rows[0]["plats_ra"] == "trapporna vid Tunnelgatan"
    assert obs_rows[1]["plats_ra"] == "trapporna vid Tunnelgatan"
    vittnen = next(p["rows"] for q, p in session.calls if "MERGE (v:Vittne" in q)
    assert vittnen == [{"obs_id": ids[0], "id": f"{_STAM}#anna berg", "namn": "Anna Berg",
                        "namn_norm": "anna berg", "stem": _STAM}]
    maskade = next(p["rows"] for q, p in session.calls if "MERGE (m:MaskadUppgift" in q)
    assert [m["id"] for m in maskade] == [f"{ids[1]}#m"]
    assert maskade[0]["lokalt_id"] == "2-1" and maskade[0]["kategori"] == "namn"
    assert "namn" not in maskade[0] and "namn_norm" not in maskade[0]
    platser = next(p["rows"] for q, p in session.calls if "MERGE (o)-[:PLATS]" in q)
    assert {r["kartplats_id"] for r in platser} == {"tunnelgatan-trapporna"}
    assert len(platser) == 2
    egenskaper = next(p["rows"] for q, p in session.calls if "MERGE (e:Egenskap" in q)
    assert egenskaper[0]["id"] == f"{ids[0]}#p0#s0" and egenskaper[0]["min"] == 180
    assert any("sid" in p for p in params)


def test_fake_missing_page_raises(cfg) -> None:
    session = FakeSession(sida_finns=False)
    with pytest.raises(ValueError, match="A1 —.*2"):
        load_graph.write_page(session, _full_ext())
    assert not any("MERGE" in q for q, _p in session.calls)


def test_fake_placeholder_name_never_becomes_witness(cfg) -> None:
    session = FakeSession()
    load_graph.write_page(session, _ext(_STAM, 2, "a" * 64, [_obs(0, namn="NN")]))
    assert not any("MERGE (v:Vittne" in q for q, _p in session.calls)


def test_fake_unmatched_kartplats_logged_at_debug(cfg) -> None:
    sink = _Sink()
    ctx = OperationContext(sink=sink, cancel_requested=lambda: False, debug=True)
    session = FakeSession()
    load_graph.write_page(session, _ext(_STAM, 2, "a" * 64, [_obs(0, kartplats="Okänd plats")]),
                          ctx=ctx)
    debug = [m for lvl, m in sink.lines if lvl == "debug"]
    assert any("okänd-plats" in m and "sida 2" in m for m in debug), debug


def test_samma_uppgift_pairs_only_across_documents() -> None:
    rows = [
        {"id": "A#1#0#x", "stem": "A", "utdrag": "Jag såg en man springa uppför trapporna."},
        {"id": "B#1#0#y", "stem": "B", "utdrag": "jag  såg en man springa uppför trapporna"},
        {"id": "A#2#0#z", "stem": "A", "utdrag": "Jag såg en man springa uppför trapporna!"},
        {"id": "C#1#0#w", "stem": "C", "utdrag": "Helt annan text om en bil på Kungsgatan."},
    ]
    pairs = load_graph.samma_uppgift_pairs(rows)
    assert ("A#1#0#x", "B#1#0#y") in pairs and ("A#2#0#z", "B#1#0#y") in pairs
    assert ("A#1#0#x", "A#2#0#z") not in pairs
    assert all(a < b for a, b in pairs)
    assert not any("C#1#0#w" in p for p in pairs)


# ── Neo4j ────────────────────────────────────────────────────────────────────

def _meta(stem: str) -> dict:
    return {"pdf_stem": stem, "arkiv": "palme", "nr": stem.split(" — ")[0],
            "titel": "Titel", "bestallt": None, "upplagt": None, "anmarkning": None,
            "antal_sidor": None, "dokumentdatum": None, "avkortat": False,
            "uppslag": [], "hanvisningar": [], "del_av": None}


def _documents(session, *stems: str, sidor: int = 1) -> None:
    rows = build_document_rows([_meta(s) for s in stems], {s: sidor for s in stems})
    write_document_layer(session, rows, batch=100, ctx=_ctx())


def _one(session, query: str, **params):
    return session.run(query, **params).single()


@pytest.mark.neo4j
def test_load_creates_observation_graph(neo4j_session, cfg) -> None:
    _documents(neo4j_session, "A")
    ext = _ext("A", 1, "1" * 64, [_obs(0, observerade=[_person()])])
    res = load_graph.load_ontology(neo4j_session, [ext], cfg, ctx=_ctx())
    assert res["nya"] == 1 and res["observationer"] == 1
    rec = _one(neo4j_session, """
        MATCH (o:Observation)-[:KÄLLA]->(s:Sida)
        MATCH (o)-[:AVSER]->(h:Händelse)
        MATCH (o)-[:PLATS]->(k:Kartplats)
        MATCH (o)-[:LÄMNAD_AV]->(v:Vittne)
        MATCH (o)-[:OBSERVERADE]->(p:ObserveradPerson)-[:SIGNALEMENT]->(e:Egenskap)
        MATCH (p)-[r:KAN_VARA]->(n:Nyckelperson)
        RETURN count(o) AS n_obs, o.status AS status, s.id AS sida, h.id AS h,
               k.id AS k, v.namn_norm AS v, e.min AS emin, e.max AS emax,
               r.status AS kv, n.id AS np""")
    assert rec["n_obs"] == 1 and rec["status"] == "ogranskad"
    assert rec["sida"] == "A#1" and rec["h"] == "skotten"
    assert rec["k"] == "tunnelgatan-trapporna" and rec["v"] == "anna berg"
    assert rec["emin"] == 180 and rec["emax"] == 185
    assert rec["kv"] == "ogranskad" and rec["np"] == "garningsmannen"


@pytest.mark.neo4j
def test_reload_same_text_is_idempotent(neo4j_session, cfg) -> None:
    _documents(neo4j_session, "A")
    ext = _ext("A", 1, "1" * 64, [_obs(0, observerade=[_person()]), _obs(1)])
    load_graph.load_ontology(neo4j_session, [ext], cfg, ctx=_ctx())

    def _antal():
        return _one(neo4j_session, "MATCH (n) WITH count(n) AS n "
                    "MATCH ()-[r]->() RETURN n, count(r) AS r")

    fore = (_antal()["n"], _antal()["r"])
    neo4j_session.run("MATCH (o:Observation) SET o.status = 'godkänd'").consume()
    res = load_graph.load_ontology(neo4j_session, [ext], cfg, ctx=_ctx())
    assert res["nya"] == 0 and res["inaktuella"] == 0 and res["borttagna"] == 0
    assert (_antal()["n"], _antal()["r"]) == fore
    # En granskad observation sätts aldrig tillbaka till ogranskad.
    statusar = {r["s"] for r in neo4j_session.run("MATCH (o:Observation) RETURN o.status AS s")}
    assert statusar == {"godkänd"}


@pytest.mark.neo4j
def test_changed_text_replaces_unreviewed_and_marks_reviewed_stale(neo4j_session, cfg) -> None:
    _documents(neo4j_session, "A")
    gammal = _ext("A", 1, "1" * 64, [_obs(0, observerade=[_person()]),
                                     _obs(1, observerade=[_person()])])
    load_graph.load_ontology(neo4j_session, [gammal], cfg, ctx=_ctx())
    godkand_id = load_graph.observation_id("A", 1, 1, load_graph.extraction_hash(gammal))
    ogranskad_id = load_graph.observation_id("A", 1, 0, load_graph.extraction_hash(gammal))
    neo4j_session.run("MATCH (o:Observation {id: $id}) SET o.status = 'godkänd'",
                      id=godkand_id).consume()

    ny = _ext("A", 1, "2" * 64, [_obs(0, utdrag="En helt ny iakttagelse om en man i rock.")])
    res = load_graph.load_ontology(neo4j_session, [ny], cfg, ctx=_ctx())
    assert res["borttagna"] == 1 and res["inaktuella"] == 1 and res["nya"] == 1

    rows = {r["id"]: r["s"] for r in neo4j_session.run(
        "MATCH (o:Observation) RETURN o.id AS id, o.status AS s")}
    assert ogranskad_id not in rows
    assert rows[godkand_id] == "inaktuell"
    assert rows[load_graph.observation_id("A", 1, 0, load_graph.extraction_hash(ny))] == (
        "ogranskad")
    # Den ogranskades personer och egenskaper är borta; den godkändas finns kvar.
    assert _one(neo4j_session, "MATCH (p:ObserveradPerson) WHERE p.id STARTS WITH $id "
                "RETURN count(p) AS n", id=ogranskad_id)["n"] == 0
    assert _one(neo4j_session, "MATCH (e:Egenskap) WHERE e.id STARTS WITH $id "
                "RETURN count(e) AS n", id=ogranskad_id)["n"] == 0
    assert _one(neo4j_session, "MATCH (:Observation {id: $id})-[:OBSERVERADE]->"
                "(:ObserveradPerson)-[:SIGNALEMENT]->(e:Egenskap) RETURN count(e) AS n",
                id=godkand_id)["n"] == 1
    # Sidan och dokumentlagret är orörda.
    assert _one(neo4j_session, "MATCH (:Dokument {stem: 'A'})-[:HAR_SIDA]->(s:Sida {id: 'A#1'}) "
                "RETURN count(s) AS n")["n"] == 1
    # En ny körning med samma text räknar inte den inaktuella igen.
    res2 = load_graph.load_ontology(neo4j_session, [ny], cfg, ctx=_ctx())
    assert res2["inaktuella"] == 0 and res2["borttagna"] == 0 and res2["nya"] == 0


@pytest.mark.neo4j
def test_same_person_proposed_but_never_for_masked(neo4j_session, cfg) -> None:
    _documents(neo4j_session, "A", "B", "C")
    exts = [
        _ext("A", 1, "1" * 64, [_obs(0, namn="Anna Berg")]),
        _ext("B", 1, "2" * 64, [_obs(0, namn="anna  berg", utdrag="Annat utdrag om en bil.")]),
        _ext("C", 1, "3" * 64, [_obs(0, namn=None, maskad=True, utdrag="Maskerat vittne såg.")]),
    ]
    res = load_graph.load_ontology(neo4j_session, exts, cfg, ctx=_ctx())
    assert res["samma_person"] == 1
    rec = _one(neo4j_session, "MATCH (a:Vittne)-[r:SAMMA_PERSON]->(b:Vittne) "
               "RETURN a.stem AS a, b.stem AS b, r.status AS s, count(r) AS n")
    assert (rec["a"], rec["b"], rec["s"], rec["n"]) == ("A", "B", "ogranskad", 1)
    assert _one(neo4j_session, "MATCH (m:MaskadUppgift)-[r]-() WHERE type(r) <> 'LÄMNAD_AV' "
                "RETURN count(r) AS n")["n"] == 0
    c_id = load_graph.observation_id("C", 1, 0, load_graph.extraction_hash(exts[2]))
    rec = _one(neo4j_session, "MATCH (m:MaskadUppgift {id: $id}) "
               "RETURN m.stem AS s, m.lokalt_id AS lok", id=f"{c_id}#m")
    assert (rec["s"], rec["lok"]) == ("C", "1-0")
    # Ett granskat förslag skrivs inte över och dubbleras inte.
    neo4j_session.run("MATCH ()-[r:SAMMA_PERSON]->() SET r.status = 'avvisad'").consume()
    res2 = load_graph.load_ontology(neo4j_session, exts, cfg, ctx=_ctx())
    assert res2["samma_person"] == 0
    rec = _one(neo4j_session, "MATCH ()-[r:SAMMA_PERSON]-() "
               "RETURN count(DISTINCT r) AS n, collect(r.status)[0] AS s")
    assert rec["n"] == 1 and rec["s"] == "avvisad"


@pytest.mark.neo4j
def test_masked_witnesses_on_different_pages_stay_distinct(neo4j_session, cfg) -> None:
    _documents(neo4j_session, "A", sidor=2)
    exts = [
        _ext("A", 1, "1" * 64, [_obs(0, namn=None, maskad=True, utdrag="Maskat vittne på sida ett.")]),
        _ext("A", 2, "2" * 64, [_obs(0, namn=None, maskad=True, utdrag="Maskat vittne på sida två.")]),
    ]
    load_graph.load_ontology(neo4j_session, exts, cfg, ctx=_ctx())
    rows = {r["id"]: (r["lok"], r["n"]) for r in neo4j_session.run(
        "MATCH (o:Observation)-[:LÄMNAD_AV]->(m:MaskadUppgift) "
        "RETURN m.id AS id, m.lokalt_id AS lok, count(o) AS n")}
    forvantat = {
        f"{load_graph.observation_id('A', s, 0, load_graph.extraction_hash(e))}#m": (f"{s}-0", 1)
        for s, e in ((1, exts[0]), (2, exts[1]))}
    assert rows == forvantat


@pytest.mark.neo4j
def test_masked_node_of_stale_reviewed_observation_is_kept_apart(neo4j_session, cfg) -> None:
    _documents(neo4j_session, "A")
    gammal = _ext("A", 1, "1" * 64, [_obs(0, namn=None, maskad=True)])
    gammal["parsed"]["observationer"][0]["vittne"].update(kategori="adress", roll="granne")
    load_graph.load_ontology(neo4j_session, [gammal], cfg, ctx=_ctx())
    gammal_id = load_graph.observation_id("A", 1, 0, load_graph.extraction_hash(gammal))
    neo4j_session.run("MATCH (o:Observation {id: $id}) SET o.status = 'godkänd'",
                      id=gammal_id).consume()

    ny = _ext("A", 1, "2" * 64, [_obs(0, namn=None, maskad=True, utdrag="Ny maskad iakttagelse.")])
    ny["parsed"]["observationer"][0]["vittne"].update(kategori="telefon", roll="förbipasserande")
    load_graph.load_ontology(neo4j_session, [ny], cfg, ctx=_ctx())
    ny_id = load_graph.observation_id("A", 1, 0, load_graph.extraction_hash(ny))

    rows = {r["oid"]: (r["mid"], r["status"], r["kat"], r["roll"]) for r in neo4j_session.run(
        "MATCH (o:Observation)-[:LÄMNAD_AV]->(m:MaskadUppgift) "
        "RETURN o.id AS oid, m.id AS mid, o.status AS status, m.kategori AS kat, "
        "m.roll AS roll")}
    assert rows == {
        gammal_id: (f"{gammal_id}#m", "inaktuell", "adress", "granne"),
        ny_id: (f"{ny_id}#m", "ogranskad", "telefon", "förbipasserande"),
    }


@pytest.mark.neo4j
def test_same_text_new_extraction_replaces_unreviewed(neo4j_session, cfg) -> None:
    _documents(neo4j_session, "A")
    gammal = _ext("A", 1, "1" * 64, [_obs(i, utdrag=f"Gammalt utdrag nummer {i} om mannen.")
                                     for i in range(3)])
    load_graph.load_ontology(neo4j_session, [gammal], cfg, ctx=_ctx())
    ny = _ext("A", 1, "1" * 64, [_obs(0, utdrag="Nytt utdrag ur samma sidtext om mannen.")])
    res = load_graph.load_ontology(neo4j_session, [ny], cfg, ctx=_ctx())
    assert res["borttagna"] == 3 and res["nya"] == 1
    rows = [r["id"] for r in neo4j_session.run("MATCH (o:Observation) RETURN o.id AS id")]
    assert rows == [load_graph.observation_id("A", 1, 0, load_graph.extraction_hash(ny))]
    rec = _one(neo4j_session, "MATCH (o:Observation) RETURN o.text_hash AS t, "
               "o.extraktion_hash AS e")
    assert rec["t"] == "1" * 64 and rec["e"] == load_graph.extraction_hash(ny)


@pytest.mark.neo4j
def test_near_identical_quotes_get_samma_uppgift(neo4j_session, cfg) -> None:
    _documents(neo4j_session, "A", "B", "C")
    citat = "Jag såg en man i mörk rock springa uppför trapporna vid Tunnelgatan."
    exts = [
        _ext("A", 1, "1" * 64, [_obs(0, utdrag=citat)]),
        _ext("B", 1, "2" * 64, [_obs(0, namn="Bo Ek", utdrag=citat.replace("mörk", "mörka"))]),
        _ext("C", 1, "3" * 64, [_obs(0, namn="Cia Ek", utdrag=citat, handelse="ambulans")]),
    ]
    res = load_graph.load_ontology(neo4j_session, exts, cfg, ctx=_ctx())
    assert res["samma_uppgift"] == 1
    rec = _one(neo4j_session, "MATCH (a:Observation)-[r:SAMMA_UPPGIFT]->(b:Observation) "
               "RETURN a.id AS a, b.id AS b, count(r) AS n")
    assert rec["n"] == 1 and rec["a"].startswith("A#") and rec["b"].startswith("B#")
    res2 = load_graph.load_ontology(neo4j_session, exts, cfg, ctx=_ctx())
    assert res2["samma_uppgift"] == 0
    assert _one(neo4j_session, "MATCH ()-[r:SAMMA_UPPGIFT]->() RETURN count(r) AS n")["n"] == 1


@pytest.mark.neo4j
def test_rejected_observation_gets_no_samma_uppgift(neo4j_session, cfg) -> None:
    _documents(neo4j_session, "A", "B")
    citat = "Jag såg en man i mörk rock springa uppför trapporna vid Tunnelgatan."
    exts = [_ext("A", 1, "1" * 64, [_obs(0, utdrag=citat)])]
    load_graph.load_ontology(neo4j_session, exts, cfg, ctx=_ctx())
    neo4j_session.run("MATCH (o:Observation) SET o.status = 'avvisad'").consume()
    exts.append(_ext("B", 1, "2" * 64, [_obs(0, namn="Bo Ek", utdrag=citat)]))
    res = load_graph.load_ontology(neo4j_session, exts, cfg, ctx=_ctx())
    assert res["samma_uppgift"] == 0


@pytest.mark.neo4j
def test_missing_page_node_is_skipped_with_warning(neo4j_session, cfg) -> None:
    _documents(neo4j_session, "A")
    sink = _Sink()
    exts = [_ext("Z", 4, "9" * 64, [_obs(0)]), _ext("A", 1, "1" * 64, [_obs(0)])]
    res = load_graph.load_ontology(neo4j_session, exts, cfg, ctx=_ctx(sink))
    assert res["saknade_sidor"] == 1 and res["sidor"] == 1 and res["nya"] == 1
    varningar = [m for lvl, m in sink.lines if lvl == "warning"]
    assert any("Z" in m and "4" in m for m in varningar)
    assert _one(neo4j_session, "MATCH (o:Observation) WHERE o.id STARTS WITH 'Z#' "
                "RETURN count(o) AS n")["n"] == 0
    assert _one(neo4j_session, "MATCH (s:Sida {id: 'Z#4'}) RETURN count(s) AS n")["n"] == 0
    info = [m for lvl, m in sink.lines if lvl == "info" and m.startswith("Ontologi:")]
    assert info == ["Ontologi: 1 sidor, 1 observationer (1 nya, 0 inaktuella, "
                    "0 borttagna), 0 SAMMA_PERSON, 0 SAMMA_UPPGIFT"]
