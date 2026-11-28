"""Tolkning och kontroll av LLM-svaret för iakttagelser kring mordnatten.

Modulen är ren logik (inga LLM-anrop, ingen databas) och är grinden mot både
påhittade uppgifter och gissade namn bakom maskeringar:

* ett ``utdrag`` som inte står ordagrant på sidan förkastar hela observationen,
* ett vittnesnamn som inte står på sidan blir okänt,
* en platshållare (``[MASKAD]``, ``NN``, ``okänd`` …) blir ett maskat vittne
  utan namn — innehållet bakom en maskering gissas aldrig,
* ``kan_vara`` behålls bara mot nyckelpersoner vars alias står i utdraget,
* ett maskerat vittnes ``roll`` behålls bara om den står på sidan.

Namn och alias måste stå som hela ord. Fritextfälten (``beskrivning``,
``varde_text``, ``roll``, ``osakerhet``, ``tid_text``) är overifierad LLM-text
och får aldrig användas för identitet.

``parse_response`` returnerar ``ParsedPage``. Varje observation är en dict med
exakt dessa nycklar (senare uppgifter förlitar sig på formen)::

    index            0-baserad ordning bland de behållna observationerna
    tid_text         fritext ur sidan eller None
    tid_fran/till    "HH:MM" eller None (båda None om ordningen är omvänd)
    plats            {namn_ra, kartplats_namn, lat, lon, match}
                     match: "exact" | "fuzzy" | "none"
    handelse_id      id ur ankarhändelserna eller None
    vittne           {namn, maskad, kategori, roll}
    observerade      [{beskrivning,
                       signalement: [{typ, varde_text, min, max, enhet}],
                       kan_vara: [nyckelperson-id]}]
    utdrag           ordagrant citat ur sidan
    osakerhet        fritext eller None

Varningarna är korta svenska strängar med ett stabilt prefix före första
kolon (t.ex. ``citat saknas på sidan: …``) så att anropare kan gruppera dem.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass

import map_extract
from ontologi.config import NUMERISKA, SIGNALEMENT_TYPER, OntologiConfig

PLATSHALLARE_RE = re.compile(
    r"^\s*(\[\s*(maskad|maskerad|redacted)\s*\]|(n\.?\s?n\.?\s*)+|okän[dt]\b.*|x+(\s+x+)*)\s*$",
    re.IGNORECASE,
)
_HAKPARENTES_RE = re.compile(r"\[[^\]]*\]")
_MIN_UTDRAG_TECKEN = 20
_MIN_UTDRAG_ORD = 3
_MIN_NAMN = 3

_TID_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
VITTNESKATEGORIER = ("namn", "adress", "telefon", "personnummer", "okänd")
_MAX_UTDRAG = 300

SYSTEM_PROMPT = """Du extraherar iakttagelser kring mordnatten (28 februari 1986) ur en enda sida \
ur utredningsmaterialet om mordet på Olof Palme.

Regler:
- Extrahera bara iakttagelser som rör mordnatten: vem som såg, hörde eller gjorde \
något, var och när, och vad de observerade (personer, signalement, fordon).
- "utdrag" ska vara ett ordagrant citat ur sidan, högst 300 tecken. Det får inte \
omskrivas, rättas eller sammanfattas.
- NN, N.N., okänd, X och [MASKAD] är platshållare: de betyder maskerat eller okänt \
och är aldrig ett namn.
- Skriv ett vittnesnamn bara om det står på sidan. Gissa aldrig ett namn.
- Står det [MASKAD] där vittnet eller en uppgift skulle ha stått: sätt "maskad": true \
och lämna "namn" tomt. Gissa aldrig vad som döljs bakom en maskering.
- "handelse_id" får bara vara ett id ur händelselistan, annars null.
- "kan_vara" får bara innehålla id ur listan över nyckelpersoner, och bara när \
texten själv pekar ut personen (t.ex. "gärningsmannen"): ett id får anges bara när \
något av personens alias står i citatet. Annars lämnas den tom.
- Längd och ålder anges som intervall (min och max, enhet cm respektive år); \
övriga signalement har min och max null.
- Tider anges som HH:MM (24 timmar). Kan tiden inte avgöras: null.
- Returnera bara JSON enligt svarsschemat i användarprompten, ingen annan text."""


def normalize_text(text: str) -> str:
    """Gemener och blanksteg hopvikta till ett mellanslag."""
    return re.sub(r"\s+", " ", text.lower()).strip()


def text_hash(page_text: str) -> str:
    """sha256-hex av den normaliserade sidtexten."""
    return hashlib.sha256(normalize_text(page_text).encode("utf-8")).hexdigest()


def build_prompt(page_text: str, cfg: OntologiConfig) -> str:
    """Användarprompt: händelselista, nyckelpersoner och svarsschema före sidtexten.

    Schemat (``cfg.schema`` ur ``data/ontologi/schema.json``) skickas med
    ordagrant. Ändras prompten eller schemat ska ``schemaversion`` höjas, annars
    extraheras inga redan lästa sidor om.
    """
    handelser = "\n".join(
        f"- {h.id}: {h.namn} ({h.tid_fran}–{h.tid_till})" for h in cfg.handelser
    )
    personer = "\n".join(f"- {p.id}: {p.namn}" for p in cfg.nyckelpersoner)
    return (
        f"Händelser (använd id i handelse_id):\n{handelser}\n\n"
        f"Nyckelpersoner (använd id i kan_vara):\n{personer}\n\n"
        "Svarsschema (JSON Schema). Svaret måste följa det exakt: ett JSON-objekt med "
        "nyckeln observationer, alla obligatoriska nycklar med, inga andra nycklar.\n"
        f"{json.dumps(cfg.schema, ensure_ascii=False)}\n\n"
        f"Sidtext:\n{page_text}"
    )


@dataclass(frozen=True)
class ParsedPage:
    observationer: list[dict]
    varningar: list[str]


def _json_objekt(raw: str) -> object:
    """Plockar första JSON-objektet ur svaret (tål kodstaket och inledande text)."""
    start = raw.find("{")
    if start < 0:
        raise ValueError("inget JSON-objekt")
    obj, _ = json.JSONDecoder().raw_decode(raw[start:])
    return obj


def _kort(text: object, n: int = 60) -> str:
    s = str(text).replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def _ord_finns(norm: str, text_norm: str) -> bool:
    """Sant om ``norm`` står som hela ord i ``text_norm`` (båda normaliserade)."""
    return bool(norm) and re.search(rf"(?<!\w){re.escape(norm)}(?!\w)", text_norm) is not None


def _tal(varde: object) -> int | float | None:
    if isinstance(varde, bool) or not isinstance(varde, (int, float)):
        return None
    if isinstance(varde, float) and not math.isfinite(varde):
        return None
    return varde


def _tider(o: dict, varn: list[str]) -> tuple[str | None, str | None]:
    def giltig(v: object, namn: str) -> str | None:
        if v is None:
            return None
        if isinstance(v, str) and _TID_RE.match(v):
            return v
        varn.append(f"ogiltig tid: {namn}={_kort(v)}")
        return None

    fran = giltig(o.get("tid_fran"), "tid_fran")
    till = giltig(o.get("tid_till"), "tid_till")
    if fran and not till:
        till = fran
    elif till and not fran:
        fran = till
    if fran and till and fran > till:
        varn.append(f"omvänd tid: {fran} > {till}")
        return None, None
    return fran, till


def _plats(raw: object, index: list[dict]) -> dict:
    namn = raw.strip() if isinstance(raw, str) and raw.strip() else None
    m = map_extract.match_place(namn or "", index)
    return {
        "namn_ra": namn,
        "kartplats_namn": m["place_name"],
        "lat": m["lat"],
        "lon": m["lon"],
        "match": m["place_match"],
    }


def _vittne(raw: object, sida_norm: str, varn: list[str]) -> dict:
    v = raw if isinstance(raw, dict) else {}
    kategori = v.get("kategori")
    if kategori not in VITTNESKATEGORIER:
        kategori = "okänd"
    roll = v.get("roll")
    roll = roll.strip() if isinstance(roll, str) and roll.strip() else None
    namn = v.get("namn")
    namn = namn.strip() if isinstance(namn, str) else ""
    norm = normalize_text(namn)
    ut = {"namn": None, "maskad": False, "kategori": kategori, "roll": roll}
    if (
        v.get("maskad") is True
        or PLATSHALLARE_RE.match(namn)
        or any(t in norm for t in ("maskad", "masker", "redact"))
        or _HAKPARENTES_RE.search(namn)
    ):
        ut["maskad"] = True
    elif not any(c.isalpha() for c in namn) and namn:
        varn.append(f"namn saknar bokstäver: {_kort(namn)}")
    elif not namn:
        pass  # inget vittne angivet: okänt, inte maskerat
    elif len(norm) < _MIN_NAMN:
        varn.append(f"namn för kort: {_kort(namn)}")
    elif _ord_finns(norm, sida_norm):
        ut["namn"] = namn
    else:
        varn.append(f"namn saknas på sidan: {_kort(namn)}")
    # Ett maskerat vittnes roll kan bära det maskerade namnet ("granne, Karin
    # Ek"); den behålls därför bara när den står på sidan, med ordgräns som namn.
    if ut["maskad"] and roll is not None and not _ord_finns(normalize_text(roll), sida_norm):
        varn.append(f"roll saknas på sidan: {_kort(roll)}")
        ut["roll"] = None
    return ut


def _signalement(raw: object, varn: list[str]) -> dict | None:
    if not isinstance(raw, dict):
        return None
    typ = raw.get("typ")
    if typ not in SIGNALEMENT_TYPER:
        typ = "övrigt"
    varde = raw.get("varde_text")
    s = {
        "typ": typ,
        "varde_text": varde if isinstance(varde, str) else "",
        "min": None,
        "max": None,
        "enhet": None,
    }
    if typ in NUMERISKA:
        lo, hi = _tal(raw.get("min")), _tal(raw.get("max"))
        if lo is not None and hi is not None and lo > hi:
            varn.append(f"omvänt intervall: {typ} {lo}–{hi}")
            lo = hi = None
        s["min"], s["max"], s["enhet"] = lo, hi, NUMERISKA[typ]
    return s


def _observerade(raw: object, utdrag_norm: str, cfg: OntologiConfig, varn: list[str]) -> list[dict]:
    ut: list[dict] = []
    for rad in raw if isinstance(raw, list) else []:
        if not isinstance(rad, dict):
            varn.append(f"ogiltig post: observerad {_kort(rad)}")
            continue
        sigraw = rad.get("signalement")
        sig = [
            s
            for s in (_signalement(x, varn) for x in (sigraw if isinstance(sigraw, list) else []))
            if s is not None
        ]
        onskade = rad.get("kan_vara")
        behall: list[str] = []
        for pid in onskade if isinstance(onskade, list) else []:
            person = next((p for p in cfg.nyckelpersoner if p.id == pid), None)
            if person is None:
                varn.append(f"kan_vara borttagen: okänt id {_kort(pid)}")
            elif not any(_ord_finns(normalize_text(a), utdrag_norm) for a in person.alias):
                varn.append(f"kan_vara borttagen: alias för {pid} saknas i utdraget")
            elif pid not in behall:
                behall.append(pid)
        beskr = rad.get("beskrivning")
        ut.append(
            {
                "beskrivning": beskr if isinstance(beskr, str) else "",
                "signalement": sig,
                "kan_vara": behall,
            }
        )
    return ut


def _text_eller_none(v: object) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() else None


def parse_response(raw: str, page_text: str, cfg: OntologiConfig) -> ParsedPage:
    """Tolkar och kontrollerar modellens svar mot sidtexten och konfigurationen."""
    varn: list[str] = []
    try:
        data = _json_objekt(raw)
    except ValueError as exc:
        return ParsedPage([], [f"ogiltigt svar: {_kort(exc)}"])
    lista = data.get("observationer") if isinstance(data, dict) else None
    if not isinstance(lista, list):
        return ParsedPage([], ["ogiltigt svar: observationer saknas eller är ingen lista"])

    sida_norm = normalize_text(page_text)
    plats_index = map_extract.build_place_index(cfg.platser)
    handelse_ids = {h.id for h in cfg.handelser}
    ut: list[dict] = []
    for i, o in enumerate(lista):
        if not isinstance(o, dict):
            varn.append(f"observation ignorerad: nr {i} är inget objekt")
            continue
        utdrag = o.get("utdrag")
        if not isinstance(utdrag, str) or not utdrag.strip():
            varn.append(f"citat saknas: observation {i} har inget utdrag")
            continue
        utdrag_norm = normalize_text(utdrag)
        if len(utdrag_norm) < _MIN_UTDRAG_TECKEN or len(utdrag_norm.split()) < _MIN_UTDRAG_ORD:
            varn.append(f"citat för kort: {_kort(utdrag)}")
            continue
        if utdrag_norm not in sida_norm:
            varn.append(f"citat saknas på sidan: {_kort(utdrag)}")
            continue
        if len(utdrag) > _MAX_UTDRAG:
            varn.append(f"långt citat: {len(utdrag)} tecken")
        fran, till = _tider(o, varn)
        hid = o.get("handelse_id")
        if hid is not None and (not isinstance(hid, str) or hid not in handelse_ids):
            varn.append(f"okänd händelse: {_kort(hid)}")
            hid = None
        ut.append(
            {
                "index": len(ut),
                "tid_text": _text_eller_none(o.get("tid_text")),
                "tid_fran": fran,
                "tid_till": till,
                "plats": _plats(o.get("plats"), plats_index),
                "handelse_id": hid,
                "vittne": _vittne(o.get("vittne"), sida_norm, varn),
                "observerade": _observerade(o.get("observerade"), utdrag_norm, cfg, varn),
                "utdrag": utdrag,
                "osakerhet": _text_eller_none(o.get("osakerhet")),
            }
        )
    return ParsedPage(ut, varn)
