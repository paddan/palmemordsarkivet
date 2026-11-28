"""Inläsning av mordnattens observationer i Neo4j.

Läser extraktionsrader (``db.list_ontology_extractions``) och skriver ontologins
noder och kanter ovanpå dokumentlagrets ``Sida``-noder:

* ``(:Observation)-[:KÄLLA]->(:Sida)`` — en observation per extraherad iakttagelse,
* ``-[:LÄMNAD_AV]->(:Vittne)`` (namngivet vittne, id = stam + normaliserat namn)
  eller ``-[:LÄMNAD_AV]->(:MaskadUppgift)`` (id = ``<observations-id>#m``: en
  egen nod per observation, så maskade vittnen kopplas aldrig ihop — varken
  mellan sidor, mellan extraktioner eller mellan dokument),
* ``-[:AVSER]->(:Händelse)``, ``-[:PLATS]->(:Kartplats)``,
* ``-[:OBSERVERADE]->(:ObserveradPerson)-[:SIGNALEMENT]->(:Egenskap)`` och
  ``(:ObserveradPerson)-[:KAN_VARA]->(:Nyckelperson)``.

Extraktionshash: ``extraction_hash`` är sha256 av sidans ``text_hash``,
schemaversionen och det tolkade svaret (``parsed``, sorterade nycklar). Både en
ändrad sidtext och en ny extraktion av samma text (nytt schema, ny modell eller
ett annat svar) ger alltså en ny hash; ett identiskt innehåll är idempotent.
Observationens id bygger på den hashen, och sidans ``text_hash`` sparas också.

Statusregler: en ny observation får ``ogranskad``. När sidans extraktion ändrats
(annan ``extraktion_hash``) tas ogranskade observationer bort, medan granskade
(``godkänd``, ``avvisad``, ``rättad``) sätts till ``inaktuell`` med kanterna
kvar. Status sätts aldrig tillbaka till ``ogranskad``.

Länkförslag: ``SAMMA_PERSON`` föreslås mellan namngivna vittnen med samma
normaliserade namn i olika dokument. ``SAMMA_UPPGIFT`` länkar *uppgifter*
(observationer med nästan identiskt utdrag om samma händelse), inte vittnen —
den får aldrig läsas som att vittnena är samma person.

Maskning: en ``MaskadUppgift`` får aldrig ``SAMMA_PERSON`` eller någon annan
identitetskant, och ett platshållarnamn blir aldrig ett ``Vittne``. Fritextfälten
används aldrig för identitet.

Modulen läser bara ``Sida`` och rör aldrig kunskapsgrafens eller dokumentlagrets
kanter. Allt Cypher är parametriserat.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
from typing import Any

from ontologi.config import OntologiConfig
from ontologi.extraction import PLATSHALLARE_RE, normalize_text

ONTOLOGY_CONSTRAINTS = [
    f"CREATE CONSTRAINT {namn} IF NOT EXISTS FOR (n:{label}) REQUIRE n.id IS UNIQUE"
    for namn, label in (
        ("ontologi_observation_id", "Observation"),
        ("ontologi_vittne_id", "Vittne"),
        ("ontologi_maskad_id", "MaskadUppgift"),
        ("ontologi_handelse_id", "Händelse"),
        ("ontologi_kartplats_id", "Kartplats"),
        ("ontologi_person_id", "ObserveradPerson"),
        ("ontologi_egenskap_id", "Egenskap"),
        ("ontologi_nyckelperson_id", "Nyckelperson"),
    )
]

SAMMA_UPPGIFT_TROSKEL = 0.9
_UTESLUTNA_STATUSAR = ["avvisad", "inaktuell"]

# ── Referensnoder ────────────────────────────────────────────────────────────

_HANDELSE_CYPHER = """
UNWIND $rows AS row
MERGE (h:Händelse {id: row.id})
SET h.namn = row.namn, h.ordning = row.ordning,
    h.tid_fran = row.tid_fran, h.tid_till = row.tid_till
"""

_NYCKELPERSON_CYPHER = """
UNWIND $rows AS row
MERGE (n:Nyckelperson {id: row.id})
SET n.namn = row.namn
"""

_KARTPLATS_CYPHER = """
UNWIND $rows AS row
MERGE (k:Kartplats {id: row.id})
SET k.namn = row.namn, k.lat = row.lat, k.lon = row.lon
"""

# ── En sida ──────────────────────────────────────────────────────────────────

_SIDA_FINNS_CYPHER = """
OPTIONAL MATCH (s:Sida {id: $sid})
RETURN count(s) AS finns
"""

_TA_BORT_OGRANSKADE_CYPHER = """
MATCH (o:Observation)-[:KÄLLA]->(:Sida {id: $sid})
WHERE o.extraktion_hash <> $hash AND o.status = 'ogranskad'
OPTIONAL MATCH (o)-[:OBSERVERADE]->(p:ObserveradPerson)
OPTIONAL MATCH (p)-[:SIGNALEMENT]->(e:Egenskap)
WITH collect(DISTINCT o) AS os, collect(DISTINCT p) AS ps, collect(DISTINCT e) AS es
FOREACH (x IN es | DETACH DELETE x)
FOREACH (x IN ps | DETACH DELETE x)
FOREACH (x IN os | DETACH DELETE x)
RETURN size(os) AS n
"""

_MARKERA_INAKTUELLA_CYPHER = """
MATCH (o:Observation)-[:KÄLLA]->(:Sida {id: $sid})
WHERE o.extraktion_hash <> $hash AND NOT o.status IN ['ogranskad', 'inaktuell']
SET o.status = 'inaktuell'
RETURN count(o) AS n
"""

# Vittnen och maskade uppgifter i dokumentet som inte längre bär någon
# observation. Ett vittne med t.ex. ett SAMMA_PERSON-förslag behålls.
_STADA_VITTNEN_CYPHER = """
MATCH (v:Vittne {stem: $stem})
WHERE NOT (v)--()
DELETE v
"""

_STADA_MASKADE_CYPHER = """
MATCH (m:MaskadUppgift {stem: $stem})
WHERE NOT (m)<-[:LÄMNAD_AV]-()
DETACH DELETE m
"""

_BEFINTLIGA_CYPHER = """
MATCH (o:Observation)-[:KÄLLA]->(:Sida {id: $sid})
WHERE o.extraktion_hash = $hash
RETURN o.id AS id
"""

_OBSERVATION_CYPHER = """
MATCH (s:Sida {id: $sid})
UNWIND $rows AS row
MERGE (o:Observation {id: row.id})
ON CREATE SET o.status = 'ogranskad'
SET o.stem = row.stem, o.sida = row.sida,
    o.tid_fran = row.tid_fran, o.tid_till = row.tid_till, o.tid_text = row.tid_text,
    o.osakerhet = row.osakerhet, o.utdrag = row.utdrag, o.plats_ra = row.plats_ra,
    o.extraktion_hash = row.extraktion_hash, o.text_hash = row.text_hash,
    o.schemaversion = row.schemaversion
MERGE (o)-[:KÄLLA]->(s)
"""

_VITTNE_CYPHER = """
UNWIND $rows AS row
MATCH (o:Observation {id: row.obs_id})
MERGE (v:Vittne {id: row.id})
SET v.namn = row.namn, v.namn_norm = row.namn_norm, v.stem = row.stem
MERGE (o)-[:LÄMNAD_AV]->(v)
"""

_MASKAD_CYPHER = """
UNWIND $rows AS row
MATCH (o:Observation {id: row.obs_id})
MERGE (m:MaskadUppgift {id: row.id})
SET m.stem = row.stem, m.lokalt_id = row.lokalt_id,
    m.kategori = row.kategori, m.roll = row.roll
MERGE (o)-[:LÄMNAD_AV]->(m)
"""

_AVSER_CYPHER = """
UNWIND $rows AS row
MATCH (o:Observation {id: row.obs_id})
MATCH (h:Händelse {id: row.handelse_id})
MERGE (o)-[:AVSER]->(h)
"""

_PLATS_CYPHER = """
UNWIND $rows AS row
MATCH (o:Observation {id: row.obs_id})
MATCH (k:Kartplats {id: row.kartplats_id})
MERGE (o)-[:PLATS]->(k)
RETURN DISTINCT row.kartplats_id AS kartplats_id
"""

_PERSON_CYPHER = """
UNWIND $rows AS row
MATCH (o:Observation {id: row.obs_id})
MERGE (p:ObserveradPerson {id: row.id})
SET p.beskrivning = row.beskrivning
MERGE (o)-[:OBSERVERADE]->(p)
"""

_EGENSKAP_CYPHER = """
UNWIND $rows AS row
MATCH (p:ObserveradPerson {id: row.person_id})
MERGE (e:Egenskap {id: row.id})
SET e.typ = row.typ, e.varde_text = row.varde_text,
    e.min = row.min, e.max = row.max, e.enhet = row.enhet
MERGE (p)-[:SIGNALEMENT]->(e)
"""

_KAN_VARA_CYPHER = """
UNWIND $rows AS row
MATCH (p:ObserveradPerson {id: row.person_id})
MATCH (n:Nyckelperson {id: row.nyckelperson_id})
MERGE (p)-[r:KAN_VARA]->(n)
ON CREATE SET r.status = 'ogranskad'
"""

# ── Länkförslag ──────────────────────────────────────────────────────────────

# Bara Vittne (aldrig MaskadUppgift); en kant per par, från lägre till högre id.
_SAMMA_PERSON_CYPHER = """
MATCH (v:Vittne)
WHERE v.namn_norm IS NOT NULL
WITH v.namn_norm AS namn_norm, collect(v) AS vs
WHERE size(vs) > 1
UNWIND vs AS a
UNWIND vs AS b
WITH a, b
WHERE a.stem <> b.stem AND a.id < b.id AND NOT (a)-[:SAMMA_PERSON]-(b)
CREATE (a)-[:SAMMA_PERSON {status: 'ogranskad'}]->(b)
RETURN count(*) AS n
"""

_HANDELSE_IDS_CYPHER = "MATCH (h:Händelse) RETURN h.id AS id ORDER BY id"

_OBS_PER_HANDELSE_CYPHER = """
MATCH (o:Observation)-[:AVSER]->(:Händelse {id: $hid})
WHERE NOT o.status IN $uteslutna AND o.utdrag IS NOT NULL
MATCH (o)-[:KÄLLA]->(s:Sida)
RETURN DISTINCT o.id AS id, s.stem AS stem, o.utdrag AS utdrag
"""

_SAMMA_UPPGIFT_CYPHER = """
UNWIND $pairs AS pair
MATCH (a:Observation {id: pair[0]})
MATCH (b:Observation {id: pair[1]})
WITH a, b WHERE NOT (a)-[:SAMMA_UPPGIFT]-(b)
CREATE (a)-[:SAMMA_UPPGIFT {status: 'ogranskad'}]->(b)
RETURN count(*) AS n
"""


def slug(namn: str) -> str:
    """Kartplatsens id: gemener, allt utom bokstäver och siffror blir ``-``."""
    return re.sub(r"[\W_]+", "-", namn.lower()).strip("-")


def observation_id(stem: str, sida: int, index: int, hash_: str) -> str:
    return f"{stem}#{sida}#{index}#{hash_[:12]}"


def extraction_hash(row: dict) -> str:
    """sha256-hex av sidtextens hash, schemaversionen och det tolkade svaret."""
    underlag = (f"{row['text_hash']}|{row.get('schemaversion')}|"
                + json.dumps(row.get("parsed"), sort_keys=True, ensure_ascii=False))
    return hashlib.sha256(underlag.encode("utf-8")).hexdigest()


def _data(tx, query: str, **params: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = tx.run(query, **params).data()
    return rows


def _n(tx, query: str, **params: Any) -> int:
    rows = _data(tx, query, **params)
    return int(rows[0]["n"]) if rows else 0


def sync_reference_nodes(session, cfg: OntologiConfig) -> None:
    """MERGE:a händelser, nyckelpersoner och kartplatser ur konfigurationen."""
    handelser = [{"id": h.id, "namn": h.namn, "ordning": h.ordning,
                  "tid_fran": h.tid_fran, "tid_till": h.tid_till} for h in cfg.handelser]
    personer = [{"id": p.id, "namn": p.namn} for p in cfg.nyckelpersoner]
    platser: dict[str, dict] = {}
    for plats in cfg.platser:
        namn = plats.get("name") if isinstance(plats, dict) else None
        if not isinstance(namn, str) or not slug(namn):
            continue
        platser.setdefault(slug(namn), {"id": slug(namn), "namn": namn,
                                        "lat": plats.get("lat"), "lon": plats.get("lon")})

    def _skriv(tx) -> None:
        for query, rows in ((_HANDELSE_CYPHER, handelser),
                            (_NYCKELPERSON_CYPHER, personer),
                            (_KARTPLATS_CYPHER, list(platser.values()))):
            if rows:
                tx.run(query, rows=rows).consume()

    session.execute_write(_skriv)


def _vittnesnamn(vittne: dict) -> str | None:
    """Namnet om vittnet är namngivet och inte maskerat; platshållare blir None."""
    namn = vittne.get("namn")
    if vittne.get("maskad") or not isinstance(namn, str) or not namn.strip():
        return None
    if PLATSHALLARE_RE.match(namn):
        return None
    return namn


def _rader(extraction: dict, hash_: str, nya: list[dict]) -> dict[str, list[dict]]:
    """Bygg parameterraderna för sidans nya observationer."""
    stem, sida = extraction["pdf_stem"], extraction["sida"]
    rader: dict[str, list[dict]] = {k: [] for k in (
        "obs", "vittne", "maskad", "avser", "plats", "person", "egenskap", "kan_vara")}
    for o in nya:
        oid = observation_id(stem, sida, o["index"], hash_)
        plats = o.get("plats") or {}
        kartplats = plats.get("kartplats_namn")
        rader["obs"].append({
            "id": oid, "stem": stem, "sida": sida,
            "tid_fran": o.get("tid_fran"), "tid_till": o.get("tid_till"),
            "tid_text": o.get("tid_text"), "osakerhet": o.get("osakerhet"),
            "utdrag": o.get("utdrag"),
            "plats_ra": plats.get("namn_ra"),
            "extraktion_hash": hash_, "text_hash": extraction["text_hash"],
            "schemaversion": extraction.get("schemaversion"),
        })
        vittne = o.get("vittne") or {}
        namn = _vittnesnamn(vittne)
        if vittne.get("maskad"):
            rader["maskad"].append({
                "obs_id": oid, "id": f"{oid}#m", "stem": stem,
                "lokalt_id": f"{sida}-{o['index']}", "kategori": vittne.get("kategori"),
                "roll": vittne.get("roll"),
            })
        elif namn is not None:
            norm = normalize_text(namn)
            rader["vittne"].append({"obs_id": oid, "id": f"{stem}#{norm}", "namn": namn,
                                    "namn_norm": norm, "stem": stem})
        if o.get("handelse_id"):
            rader["avser"].append({"obs_id": oid, "handelse_id": o["handelse_id"]})
        if kartplats:
            rader["plats"].append({"obs_id": oid, "kartplats_id": slug(kartplats)})
        for i, person in enumerate(o.get("observerade") or []):
            pid = f"{oid}#p{i}"
            rader["person"].append({"obs_id": oid, "id": pid,
                                    "beskrivning": person.get("beskrivning")})
            for j, sig in enumerate(person.get("signalement") or []):
                rader["egenskap"].append({
                    "person_id": pid, "id": f"{pid}#s{j}", "typ": sig.get("typ"),
                    "varde_text": sig.get("varde_text"), "min": sig.get("min"),
                    "max": sig.get("max"), "enhet": sig.get("enhet"),
                })
            for nid in person.get("kan_vara") or []:
                rader["kan_vara"].append({"person_id": pid, "nyckelperson_id": nid})
    return rader


def write_page(tx, extraction: dict, *, ctx=None) -> dict[str, int]:
    """Skriv en sidas observationer i en transaktion.

    Höjer ``ValueError`` om dokumentlagrets ``Sida``-nod saknas. ``ctx`` används
    bara för debugloggning.
    """
    stem, sida = extraction["pdf_stem"], extraction["sida"]
    hash_ = extraction_hash(extraction)
    sid = f"{stem}#{sida}"
    rows = _data(tx, _SIDA_FINNS_CYPHER, sid=sid)
    if not rows or not rows[0]["finns"]:
        raise ValueError(f"Sida-noden saknas för {stem!r} sida {sida} "
                         "(dokumentlagret är inte inläst)")

    borttagna = _n(tx, _TA_BORT_OGRANSKADE_CYPHER, sid=sid, hash=hash_)
    inaktuella = _n(tx, _MARKERA_INAKTUELLA_CYPHER, sid=sid, hash=hash_)
    if borttagna:
        tx.run(_STADA_MASKADE_CYPHER, stem=stem).consume()
        tx.run(_STADA_VITTNEN_CYPHER, stem=stem).consume()

    observationer = list((extraction.get("parsed") or {}).get("observationer") or [])
    befintliga = {r["id"] for r in _data(tx, _BEFINTLIGA_CYPHER, sid=sid, hash=hash_)}
    nya = [o for o in observationer
           if observation_id(stem, sida, o["index"], hash_) not in befintliga]
    if nya:
        rader = _rader(extraction, hash_, nya)
        tx.run(_OBSERVATION_CYPHER, sid=sid, rows=rader["obs"]).consume()
        for key, query in (("vittne", _VITTNE_CYPHER), ("maskad", _MASKAD_CYPHER),
                           ("avser", _AVSER_CYPHER), ("person", _PERSON_CYPHER),
                           ("egenskap", _EGENSKAP_CYPHER), ("kan_vara", _KAN_VARA_CYPHER)):
            if rader[key]:
                tx.run(query, rows=rader[key]).consume()
        if rader["plats"]:
            traffade = {r["kartplats_id"] for r in _data(tx, _PLATS_CYPHER, rows=rader["plats"])}
            saknas = sorted({r["kartplats_id"] for r in rader["plats"]} - traffade)
            if saknas and ctx is not None:
                ctx.log(f"{stem} sida {sida}: kartplats saknas som nod, ingen PLATS-kant "
                        f"(första: {saknas[:3]})", level="debug")
    return {"observationer": len(observationer), "nya": len(nya),
            "inaktuella": inaktuella, "borttagna": borttagna}


def samma_uppgift_pairs(rows: list[dict]) -> list[tuple[str, str]]:
    """Par (lägre id, högre id) i olika dokument med nästan identiskt utdrag."""
    norm = [(r["id"], r["stem"], normalize_text(r["utdrag"] or "")) for r in rows]
    par: list[tuple[str, str]] = []
    for i, (aid, astem, atext) in enumerate(norm):
        for bid, bstem, btext in norm[i + 1:]:
            if astem == bstem or not atext or not btext:
                continue
            m = difflib.SequenceMatcher(None, atext, btext)
            if (m.real_quick_ratio() >= SAMMA_UPPGIFT_TROSKEL
                    and m.quick_ratio() >= SAMMA_UPPGIFT_TROSKEL
                    and m.ratio() >= SAMMA_UPPGIFT_TROSKEL):
                par.append((aid, bid) if aid < bid else (bid, aid))
    return par


def propose_links(session) -> dict[str, int]:
    """Föreslå ``SAMMA_PERSON`` och ``SAMMA_UPPGIFT`` (status ``ogranskad``)."""
    samma_person = session.execute_write(lambda tx: _n(tx, _SAMMA_PERSON_CYPHER))
    handelser = [r["id"] for r in session.execute_read(
        lambda tx: _data(tx, _HANDELSE_IDS_CYPHER))]
    samma_uppgift = 0
    for hid in handelser:
        rows = session.execute_read(
            lambda tx, hid=hid: _data(tx, _OBS_PER_HANDELSE_CYPHER, hid=hid,
                                      uteslutna=_UTESLUTNA_STATUSAR))
        pairs = [list(p) for p in samma_uppgift_pairs(rows)]
        if pairs:
            samma_uppgift += session.execute_write(
                lambda tx, pairs=pairs: _n(tx, _SAMMA_UPPGIFT_CYPHER, pairs=pairs))
    return {"samma_person": samma_person, "samma_uppgift": samma_uppgift}


def load_ontology(session, extractions: list[dict], cfg: OntologiConfig, *, ctx) -> dict[str, int]:
    """Constraints, referensnoder, en transaktion per sida och länkförslag."""
    for stmt in ONTOLOGY_CONSTRAINTS:
        session.run(stmt).consume()
    sync_reference_nodes(session, cfg)
    ctx.log(f"Referensnoder: {len(cfg.handelser)} händelser, "
            f"{len(cfg.nyckelpersoner)} nyckelpersoner, {len(cfg.platser)} kartplatser",
            level="debug")

    summa = {"sidor": 0, "observationer": 0, "nya": 0, "inaktuella": 0,
             "borttagna": 0, "saknade_sidor": 0}
    for ext in extractions:
        ctx.check_cancelled()
        try:
            res = session.execute_write(lambda tx, ext=ext: write_page(tx, ext, ctx=ctx))
        except ValueError as exc:
            summa["saknade_sidor"] += 1
            ctx.log(f"Hoppar över sidan: {exc}", level="warning")
            continue
        summa["sidor"] += 1
        for key in ("observationer", "nya", "inaktuella", "borttagna"):
            summa[key] += res[key]
        if res["inaktuella"] or res["borttagna"]:
            ctx.log(f"{ext['pdf_stem']} sida {ext['sida']}: extraktionen ändrad "
                    f"(text {str(ext['text_hash'])[:12]}) — {res['borttagna']} ogranskade "
                    f"borttagna, {res['inaktuella']} granskade inaktuella", level="debug")

    ctx.check_cancelled()
    lankar = propose_links(session)
    summa.update(lankar)
    ctx.log(f"Ontologi: {summa['sidor']} sidor, {summa['observationer']} observationer "
            f"({summa['nya']} nya, {summa['inaktuella']} inaktuella, "
            f"{summa['borttagna']} borttagna), {lankar['samma_person']} SAMMA_PERSON, "
            f"{lankar['samma_uppgift']} SAMMA_UPPGIFT")
    return summa
