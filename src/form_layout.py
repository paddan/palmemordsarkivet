"""Blanketter och förhör ovanpå layoutblocken (ren modul, ingen Surya-import).

Surya 2 lämnar sällan blanketten SPANINGSUPPSLAG som ``Form``: etikett och värde
är oftast separata block (``Text``, ``PageHeader`` …) där värdet står under
etiketten, ibland i samma block åtskilda av ``<br/>``. Fälten tolkas därför
geometriskt över sidans alla block, oberoende av Suryas etikett. Kända
blankettvarianter i ``data/formular/*.json`` avgör vilka rader som är etiketter,
rättar OCR-skadade etiketter, pekar ut fritextfält och fast tryckt text som
aldrig är ett ifyllt värde.

Fält tas bara ut när en mall matchar sidan. Utan mall lämnas blocken orörda:
nästan inga sidor i arkivet är blanketter, och numrerade rader i vanlig text
(datum, klockslag, antal) får aldrig flyttas ur läsordningen. En ny
blankettvariant kräver alltså en mall i ``data/formular/``. Mallarna kontrolleras
när de läses in (``TemplateError`` med fil och nyckel).

Här finns också förhörens metadatarader (``Förhörsledare: …``), repliker
(``F: …``), avstavning över radbrott och kryssrutor som egentligen är små
maskeringar; de gäller alla sidor, med eller utan mall. Inget loggas här —
modulen ser dokumenttext; ``trace`` i ``postprocess`` bär bara en textfri
sammanfattning av besluten.

Heuristiker (vad, varför, konstant):

- **Mallval:** mallens rubrikord finns på sidan och minst tre numrerade rader
  godtas som mallens etiketter; en enstaka träff är en slump
  (``MIN_TEMPLATE_LABELS``).
- **Etikett:** numret finns i mallen och hela raden liknar mallens etikett eller
  ett alias, eftersom vittnesmål ofta börjar med tal (``LABEL_ACCEPT``, för
  korta etiketter ``LABEL_ACCEPT_SHORT`` under ``SHORT_LABEL_CHARS`` tecken).
  Ett ensamt nummer tar etiketten från raden under; ett avvisat mallnummer är
  ändå en spärr för värdekedjor.
- **Rättning:** en godtagen etikett som liknar mallens tillräckligt ersätts med
  mallens stavning, så OCR-fel inte syns i fältnamnet (``LABEL_MATCH``).
- **Kvarleva på etikettraden:** text efter mallens etikett på en godtagen rad
  (``07 Avsnitt E``) blir fältets värde när fältet saknar värde underifrån och
  texten är kort (``LABEL_REST_MAX_CHARS``), inte fast tryck och inte ett
  fältnummer; annars står den kvar som vanlig text. Den tappas aldrig. En
  förklarande parentes efter etiketten hör till etiketten.
- **Skyddsnät:** ord på en etikettrad som inte syns i värde, lös text eller en
  skriven etikett (fält med värde eller ikryssad ruta) och som inte är mallens
  etikett eller tryckta parentes enligt en difflib-justering (minst
  ``LABEL_WORD_COVER`` av ordets tecken) blir lös text.
- **Fast tryck:** mallens ``printed`` (bildtexter som "Spaningschef") är aldrig ett
  värde eller ett kryssrutealternativ, eftersom fel värde är värre än inget
  (``PRINTED_MATCH``). En rad räknas också som tryck när varje del mellan rutornas
  linjer (lästa som ``|``) är det (``| datum``, ``Bitr spaningschef | Registrator``).
- **Värde:** en rad under etiketten i samma spalt (``COLUMN_TOLERANCE``), högst
  ``VALUE_MAX_GAP`` × sidhöjden under, högst ``VALUE_MAX_CHARS`` tecken och högst
  ``MAX_VALUE_LINES`` rader; repliker och långa rader avbryter kedjan, eftersom
  de är löptext. Fast tryck mellan etiketten och raden (en ny rutas rubrik)
  avbryter också kedjan, men inte en grannetikett eller tryckt rubrik på samma
  rad som etiketten (``07 Avsnitt`` bredvid ``08 Löpande nr``); löptext och
  avvisade etiketter avbryter alltid.
- **Samma rad:** två rutor står på samma rad när de överlappar lodrätt minst
  ``ROW_OVERLAP`` av den lägre rutans höjd.
- **Två nummer på en etikettrad:** ``05 datum 06 klockan`` blir två etiketter när
  det inre numret finns i mallen, är högre än det första och båda halvorna liknar
  sina etiketter; gränsen i x-led uppskattas ur teckenbredden (blockets bredd
  delad med dess längsta rad).
- **Etikettrad och spalter:** etiketter på samma rad delar sidan i spalter, från
  etikettens vänsterkant till nästa etiketts, men högst ``ROW_VALUE_REACH`` ×
  sidbredden till höger om etikettens högerkant. En rad under etikettraden hör
  till spalten där dess text har sin mitt (inte där den börjar), och får börja
  högst ``ROW_VALUE_LEFT`` × sidbredden till vänster om spaltens etikett. Når
  texten in i grannspalter som saknar eget värde under raden delas den ord för
  ord efter spalterna (``Ingvar Kjellvås 08-7694169`` under 03 och 04), men bara
  när varje del är värdeformad (se *Värdeform*; på en rad med en siffra räknas
  också ett ensamt kort gement ord som ``krinsp`` före numret). Kan raden inte
  delas och är den inte själv värdeformad är den prosa och står kvar som vanlig
  text på sin plats i läsordningen; har
  grannen ett eget värde delas inget, eftersom maskinskrivna värden ofta är
  bredare än etiketten. Ord i raden som som helhet liknar en etikett, ett alias
  eller fast tryck (högst ``MAX_LABEL_RUN`` ord i följd) är tryck och står kvar
  som text. Raden lämnas åt den vanliga kedjan när ett ensamt nummer på
  etikettraden ännu väntar på sin etikett, när den innehåller kryssrutor, eller
  när dess mitt inte ligger i någon spalt.
- **Värdeform:** text med en siffra, eller högst ``VALUE_LIKE_MAX_WORDS`` ord där
  minst ett börjar med versal och högst ett med gemen (namn, koder, ``krinsp
  Lennart Gustafsson``), och ingen mening mitt i. ``nej`` och ``sa hon till slut.`` är prosa.
- **Värde bredvid etiketten:** en värdeformad rad som inte fått något fält och står på samma
  rad som en tom etikett, högst ``ROW_VALUE_REACH`` × sidbredden till höger om
  den och inom etikettens spalt, är fältets värde (sidfotens ``20 Tagit del
  860308``). En etikettrad bakom rutans linje (``| 25 Spanchef: sign``) är aldrig
  ett värde.
- **Alias bredvid numret:** en onumrerad rad som liknar ett alias för fält N och
  står på samma rad som etiketten N, till höger om den, är en egen etikett för N
  (variant 2: ``06`` med ``Promemoria`` under och ``Utskriftsdatum`` bredvid).
- **Fritextfält:** mallens ``free_text`` (t.ex. "18 I sak") samlar inget värde,
  och under dess rad godtas bara etiketter med högre nummer, eftersom
  vittnesmålet står där. En fritextcell i en tabell blir ett eget stycke.
- **Nummerordning:** en högre numrerad etikett får stå högst ``ORDER_MAX_RISE`` ×
  sidhöjden ovanför en lägre; etiketten med flest brott avvisas först.
- **Sidfot:** etiketter över fritextnumret godtas bara som ett kluster av minst
  ``FOOTER_MIN_NUMBERS`` nummer inom ``FOOTER_WINDOW`` × sidhöjden, i strikt
  ordning (``FOOTER_MAX_RISE``), med värden på högst ``FOOTER_VALUE_MAX_CHARS``
  tecken: en vittnesrad kan likna en etikett men inte ett helt ordnat band.
"""

from __future__ import annotations

import dataclasses
import difflib
import json
import re
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

from surya_layout import (
    CHECKBOX_OFF,
    CHECKBOX_ON,
    MASK_MARKER,
    REDACTED_RE,
    BlockKind,
    FormField,
    LayoutBlock,
    fields_text,
)

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ROOT / "data" / "formular"

MASK = MASK_MARKER
VALUE_MAX_GAP = 0.035  # andel av sidhöjden mellan etikett (eller förra värdet) och värde
VALUE_MAX_CHARS = 200
COLUMN_TOLERANCE = 0.015  # andel av sidbredden
LABEL_MATCH = 0.75  # minsta likhet för att rätta en etikett till mallens
LABEL_ACCEPT = 0.6  # minsta likhet för att en numrerad rad ska räknas som mallens etikett
LABEL_ACCEPT_SHORT = 0.8  # samma för korta malletiketter ("I sak", "datum")
SHORT_LABEL_CHARS = 8
MIN_TEMPLATE_LABELS = 3  # en mall gäller bara när minst så många etiketter godtas
PRINTED_MATCH = 0.85  # minsta likhet mot mallens fasta tryck
# blanketten trycker fälten i nummerordning: en högre numrerad etikett får stå högst
# så här mycket (andel av sidhöjden) ovanför en lägre, annars är en av dem prosa
ORDER_MAX_RISE = 0.10
MAX_VALUE_LINES = 3  # ett ifyllt värde är kort; fler rader under en etikett är löptext
# sidfotsetiketter (nummer över fritextnumret) godtas bara som ett tätt, ordnat kluster,
# eftersom en vittnesrad kan likna en enskild sidfotsetikett men inte ett helt band
FOOTER_WINDOW = 0.08  # andel av sidhöjden ovanför den lägsta sidfotskandidatens överkant
FOOTER_MIN_NUMBERS = 3  # minsta antal olika sidfotsnummer i klustret
FOOTER_MAX_RISE = 0.01  # strikt ordning i klustret
FOOTER_VALUE_MAX_CHARS = 60
# text efter etiketten på etikettraden ("07 Avsnitt E") blir fältets värde bara när
# den är kort; längre text är löptext som råkat hamna på raden
LABEL_REST_MAX_CHARS = 60
# två rutor står på samma rad när de överlappar lodrätt minst så stor andel av den lägre
# rutans höjd (etiketterna 03–06 i variant 1, eller en etikett och ett värde bredvid den)
ROW_OVERLAP = 0.5
# andel av sidbredden: ett värde får börja högst så långt till höger om etikettens
# högerkant och ändå höra till etikettens spalt (sidfotens "20 Tagit del 860308")
ROW_VALUE_REACH = 0.08
# andel av sidbredden: ett maskinskrivet värde under en etikettrad får börja så långt
# till vänster om sin etiketts vänsterkant (sida 3: "87-07-17" börjar före "05 datum")
ROW_VALUE_LEFT = 0.03
VALUE_LIKE_MAX_WORDS = 4  # ett namn eller en kod utan siffror är kort
MAX_LABEL_RUN = 8  # längsta ordföljd i en värderad som prövas mot mallens tryck
MAX_FIELD_NUMBER = 30
_SKIP_KINDS = {BlockKind.PAGE_FOOTER, BlockKind.CODE, BlockKind.FORMULA, BlockKind.FIGURE}
_NUMBER_RE = re.compile(r"^(\d{2})(?:\s+(.*))?$", re.S)
_BOX_RE = re.compile(f"[{CHECKBOX_OFF}{CHECKBOX_ON}]")


@dataclass(frozen=True)
class FormTemplate:
    name: str
    match: tuple[str, ...]
    labels: dict[str, str]
    free_text: tuple[str, ...] = ()
    aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)  # andra tryckta etiketter
    printed: tuple[str, ...] = ()  # fast tryck som aldrig är ett värde


# --- Mallar -------------------------------------------------------------------------


def load_templates(path: Path = TEMPLATE_DIR) -> list[FormTemplate]:
    """Alla blankettmallar i katalogen (en fil kan hålla flera varianter).

    Höjer ``TemplateError`` (en ``ValueError``) med fil och nyckel för en ogiltig mall.
    """
    return list(_load_templates_cached(Path(path)))


_TEMPLATE_KEYS = {"name", "match", "labels", "free_text", "aliases", "printed"}


class TemplateError(ValueError):
    """En blankettmall i ``data/formular/`` som inte går att använda."""


@cache
def _load_templates_cached(path: Path) -> tuple[FormTemplate, ...]:
    out: list[FormTemplate] = []
    files = sorted(path.glob("*.json")) if path.is_dir() else [path]
    for file in files:
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TemplateError(f"Blankettmallen {file} går inte att läsa som JSON: {exc}") from exc
        items = data.get("templates") if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise TemplateError(f"Blankettmallen {file}: nyckeln 'templates' ska vara en lista med mallar")
        out += [_template_from(file, k, t) for k, t in enumerate(items)]
    return tuple(out)


def _template_from(file: Path, index: int, t: Any) -> FormTemplate:
    """En mall ur JSON, kontrollerad en gång vid inläsning så att ett skrivfel
    syns direkt med fil och nyckel i stället för som saknade fält på sidorna."""

    def fail(key: str, why: str) -> TemplateError:
        return TemplateError(f"Blankettmallen {file} (mall {index}): nyckeln '{key}' {why}")

    def strings(key: str, value: Any) -> tuple[str, ...]:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise fail(key, "ska vara en lista med strängar")
        return tuple(value)

    if not isinstance(t, dict):
        raise fail("templates", "ska bara innehålla objekt")
    for key in t:
        if key not in _TEMPLATE_KEYS:
            raise fail(str(key), f"är okänd (tillåtna: {', '.join(sorted(_TEMPLATE_KEYS))})")
    name = t.get("name")
    if not isinstance(name, str) or not name.strip():
        raise fail("name", "saknas eller är tom")
    labels = t.get("labels")
    if not isinstance(labels, dict) or not labels:
        raise fail("labels", "saknas eller är tom")
    for number, label in labels.items():
        if not re.fullmatch(r"\d{2}", number) or not 1 <= int(number) <= MAX_FIELD_NUMBER:
            raise fail(f"labels.{number}", f"ska vara ett tvåsiffrigt fältnummer 01–{MAX_FIELD_NUMBER}")
        if not isinstance(label, str) or not label.strip():
            raise fail(f"labels.{number}", "ska vara en etikettext")
    free_text = strings("free_text", t.get("free_text", []))
    for number in free_text:
        if number not in labels:
            raise fail("free_text", f"pekar på fält {number}, som inte finns i 'labels'")
    aliases_raw = t.get("aliases", {})
    if not isinstance(aliases_raw, dict):
        raise fail("aliases", "ska vara ett objekt fältnummer -> lista med etiketter")
    aliases = {}
    for number, values in aliases_raw.items():
        if number not in labels:
            raise fail(f"aliases.{number}", "pekar på ett fält som inte finns i 'labels'")
        aliases[number] = strings(f"aliases.{number}", values)
    return FormTemplate(
        name=name,
        match=strings("match", t.get("match", [])),
        labels=dict(labels),
        free_text=free_text,
        aliases=aliases,
        printed=strings("printed", t.get("printed", [])),
    )


def _norm(text: str) -> str:
    """Jämförelseform: gemener, utan ``*``, ``|`` läst som ``I``, enkla mellanslag."""
    return re.sub(r"\s+", " ", text.replace("*", "").replace("|", "I").lower()).strip()


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


_TRAILING_PAREN_RE = re.compile(r"\s*\([^()]*\)\s*$")
# förklarande parentes mitt i raden ("Postadress (utdelningsadress och ortsadress), …");
# kräver blanksteg inuti så att "Upprättat(d) av" lämnas
_INNER_PAREN_RE = re.compile(r"\s*\([^()]*\s[^()]*\)")


def _whole(text: str) -> str:
    """Hela etikettraden i jämförelseform, utan avslutande parentes, förklarande
    parenteser mitt i raden och ``*`` (samma behandling på båda sidor)."""
    text = _TRAILING_PAREN_RE.sub("", text.replace("*", ""))
    return _norm(_INNER_PAREN_RE.sub("", text))


def _similarity(raw: str, canon: str) -> float:
    """Hela radens likhet med mallens etikett – ingen prefixjämförelse, eftersom
    löptext som börjar som en etikett ("Löpande räkning visade att …") annars
    liknar "Löpande nr"."""
    r, c = _whole(raw), _whole(canon)
    if not r or not c:
        return 0.0
    return difflib.SequenceMatcher(None, r, c).ratio()


def _threshold(canon: str) -> float:
    return LABEL_ACCEPT_SHORT if len(_whole(canon)) < SHORT_LABEL_CHARS else LABEL_ACCEPT


def _is_turn(text: str) -> bool:
    """Raden är en replik (``F: …``, ``Fråga: …``)."""
    return split_turns(text, allow_masked=False)[0][0] is not None


def _canon_labels(tpl: FormTemplate, number: str) -> list[str]:
    if number not in tpl.labels:
        return []
    return [tpl.labels[number], *tpl.aliases.get(number, ())]


def _accepts(tpl: FormTemplate, number: str, raw_label: str) -> bool:
    """Numret finns i mallen och etiketten liknar mallens etikett (eller ett alias):
    ett nummer först på raden räcker inte, eftersom vittnesmål ofta börjar med tal
    ("15 minuter senare …"). Korta malletiketter kräver högre likhet (få tecken ger
    höga kvoter av en slump); en tom etikett godtas inte här (ett ensamt nummer
    prövas mot raden under, se ``_label_els``)."""
    if not _norm(raw_label):
        return False
    return any(_similarity(raw_label, c) >= _threshold(c) for c in _canon_labels(tpl, number))


# en etikett får bara svälja ord som till minst så stor del matchar mallens etikett;
# annars är ordet ett värde eller annan text efter etiketten (beslut BF)
LABEL_WORD_MIN_MATCH = 0.5


def _label_cuts(raw: str) -> list[int]:
    """Ställen där etiketten kan sluta: före blanksteg och skiljetecken, vid radens
    slut och där en bokstav följs av en siffra (hopskrivet värde: ``Avsnitt12``)."""
    cuts = [
        i
        for i in range(1, len(raw))
        if raw[i].isspace() or raw[i] in "(:;,." or (raw[i - 1].isalpha() and raw[i].isdigit())
    ]
    return [*cuts, len(raw)]


def _covers_words(prefix: str, canon: str) -> bool:
    """Prefixets sista ord matchar (i en difflib-justering mot mallens etikett) till
    minst ``LABEL_WORD_MIN_MATCH``, så att etiketten inte sväljer ett värde som
    råkar dela några bokstäver med etikettens slut. Ord inne i etiketten får vara
    OCR-skadade (``Brott med vilkt …``); de rättas till mallens stavning."""
    pn, cn = _norm(prefix), _norm(canon)
    last = list(re.finditer(r"\S+", pn))[-1]
    matched: set[int] = set()
    for _a, b, size in difflib.SequenceMatcher(None, cn, pn, autojunk=False).get_matching_blocks():
        matched.update(range(b, b + size))
    word = range(last.start(), last.end())
    return sum(j in matched for j in word) >= LABEL_WORD_MIN_MATCH * len(word)


def _label_match(raw: str, canon: str) -> tuple[float, str, str]:
    """(likhet, mallens etikett, rest).

    Raden justeras mot mallens etikett: etiketten är det radprefix (se
    ``_label_cuts``) som liknar mallens etikett mest och vars sista ord täcks av
    justeringen; ``rest`` är allt efter det, även ett hopskrivet värde
    (``Avsnitt12`` → ``12``) eller text efter en förkortad etikett. Vid lika
    likhet vinner det kortare prefixet, så hellre en för lång rest (som aldrig
    tappas) än en för lång etikett (som ersätts av mallens).
    """
    best = (0.0, canon, raw)
    for cut in _label_cuts(raw):
        prefix = raw[:cut]
        if not _norm(prefix) or not _covers_words(prefix, canon):
            continue
        r = _ratio(prefix, canon)
        if r > best[0]:
            best = (r, canon, raw[cut:])
    return best


_LEADING_PAREN_RE = re.compile(r"^[\s.,;:]*\([^()]*\)")


def _split_rest(rest: str) -> tuple[str, str]:
    """``(etikettsvans, kvarleva)`` för texten efter mallens etikett.

    Inledande förklarande parenteser (``I sak. (anteckna …)``, ``Arbetsplats
    (arbetsgivare, postadress) Volvo AB``) är tryckt etikett och blir etikettsvans.
    Resten är en kvarleva: ett värde som Surya lagt på etikettraden
    (``07 Avsnitt E``) eller annan text, som aldrig får försvinna.
    """
    tail = ""
    while m := _LEADING_PAREN_RE.match(rest):
        tail += m.group(0)
        rest = rest[m.end():]
    remainder = rest.strip().lstrip(":;,. ").strip()
    if not remainder:
        return tail + rest, ""
    return tail, remainder


_WORD_RE = re.compile(r"[^\W\d_]+|\d+")
_PAREN_GROUP_RE = re.compile(r"\([^()]*\)?")
# ett ord på etikettraden är mallens (tryckta) text när en difflib-justering mot
# mallens etikett eller tryckta parentes täcker minst så stor del av ordets tecken
# (beslut BI); ett inskjutet ord ("Eftersson", "Volvo") täcks inte och blir lös text
LABEL_WORD_COVER = 0.8


def _word_list(text: str) -> list[str]:
    """Bokstavs- och sifferföljder var för sig, gemener (``Avsnitt12`` -> två ord)."""
    return [w.lower() for w in _WORD_RE.findall(text)]


def _lower_same_length(text: str) -> str:
    """Gemener tecken för tecken, så att positionerna stämmer med originalet."""
    return "".join((c.lower() or c)[0] for c in text)


# OCR byter ofta enstaka tecken (rätt -> rått, där -> dår): en ersättning av högst så
# många tecken mot lika många räknas som täckt, ett inskjutet ord gör det aldrig
MAX_SUBSTITUTION = 2


def _aligned(text: str, ref: str, offset: int = 0) -> tuple[set[int], set[int]]:
    """``(lika, utbytta)``: positioner i ``text`` (plus ``offset``) som en
    difflib-justering mot ``ref`` täcker med lika tecken respektive med korta
    ersättningar av lika längd (OCR-fel). Infogad text täcks aldrig."""
    t, r = _lower_same_length(text), _lower_same_length(ref)
    equal: set[int] = set()
    swapped: set[int] = set()
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, r, t, autojunk=False).get_opcodes():
        if op == "equal":
            equal.update(range(offset + j1, offset + j2))
        elif op == "replace" and i2 - i1 == j2 - j1 <= MAX_SUBSTITUTION:
            swapped.update(range(offset + j1, offset + j2))
    return equal, swapped


def _template_cover(text: str, canons: list[str], tpl: FormTemplate) -> tuple[set[int], set[int]]:
    """Tecken på etikettraden som mallen täcker: justeringen mot den etikett (eller
    det alias) som liknar raden mest, och för varje parentes som är mallens fasta
    tryck justeringen mot just den tryckta parentesen – ord för ord, aldrig hela
    parentesen på en gång."""
    equal: set[int] = set()
    swapped: set[int] = set()
    refs: list[tuple[str, str, int]] = []
    if canons:
        refs.append((text, max(canons, key=lambda c: _ratio(text, c)), 0))
    printed = [p for p in tpl.printed if p.startswith("(")]
    for m in _PAREN_GROUP_RE.finditer(text):
        ref = _matching_printed_paren(m.group(0), printed)
        if ref is not None:
            refs.append((m.group(0), ref, m.start()))
    for part, ref, offset in refs:
        e, sw = _aligned(part, ref, offset)
        equal |= e
        swapped |= sw
    return equal, swapped


def _unrendered_words(text: str, covered: tuple[set[int], set[int]], rendered: list[str]) -> str:
    """Ord på etikettraden som varken är mallens text eller syns någon annanstans.

    Ett ord är mallens text när ``covered`` (``_template_cover``: lika och utbytta
    tecken) täcker minst ``LABEL_WORD_COVER`` av dess tecken och minst hälften av
    dem är lika tecken (eller ordet är ett enda tecken) – annars räcker några
    utspridda bokstäver för att ett inskjutet ord ("Volvo" mot "telefon") ska se
    täckt ut. ``rendered`` är orden som faktiskt syns:
    värdet, lös text från raden och – bara när fältet skrivs till Markdown –
    etiketten. Svaret är de saknade orden i radens ordning (tomt när inget saknas);
    de blir lös text, så att inget ord på en etikettrad försvinner (beslut BG/BI).
    """
    have: dict[str, int] = {}
    for w in rendered:
        have[w] = have.get(w, 0) + 1
    missing = []
    for m in _WORD_RE.finditer(text):
        w = m.group(0).lower()
        if have.get(w, 0) > 0:
            have[w] -= 1
            continue
        span = range(m.start(), m.end())
        n_equal = sum(i in covered[0] for i in span)
        n_cover = n_equal + sum(i in covered[1] for i in span)
        if n_cover >= LABEL_WORD_COVER * len(w) and (2 * n_equal >= len(w) or len(w) == 1):
            continue
        missing.append(m.group(0))
    return " ".join(missing)


def _matching_printed_paren(text: str, printed: list[str]) -> str | None:
    """Mallens tryckta parentes som ``text`` motsvarar (likhet ≥ ``PRINTED_MATCH``),
    annars ``None``. Utan difflibs autojunk, eftersom de tryckta parenteserna är
    längre än 200 tecken (``18 I sak``) och vanliga tecken då annars räknas bort."""
    t = _norm(text)
    scored = [(difflib.SequenceMatcher(None, t, _norm(p), autojunk=False).ratio(), p) for p in printed]
    best = max(scored, default=(0.0, ""))
    return best[1] if best[0] >= PRINTED_MATCH else None


def _is_printed(text: str, tpl: FormTemplate) -> bool:
    """Texten är mallens fasta tryck: hela raden, eller varje del mellan rutornas
    lodräta linjer, som OCR ofta läser som ``|`` (``| datum``, ``Bitr spaningschef |
    Registrator``)."""
    if not _norm(text):
        return False
    if any(_ratio(text, p) >= PRINTED_MATCH for p in tpl.printed):
        return True
    parts = [p for p in text.split("|") if _norm(p)]
    return "|" in text and bool(parts) and all(
        any(_ratio(part, p) >= PRINTED_MATCH for p in tpl.printed) for part in parts
    )


def _choose_template(
    candidates: list[tuple[str, str]], page_words: str, templates: list[FormTemplate]
) -> FormTemplate | None:
    """Mallen vars rubrikord finns på sidan och som godtar flest av sidans
    numrerade rader som etiketter (vid lika: högst medellikhet)."""
    best: tuple[int, float, int, FormTemplate] | None = None
    for i, tpl in enumerate(templates):
        if not all(m.upper() in page_words for m in tpl.match):
            continue
        accepted = [(n, t) for n, t in candidates if _accepts(tpl, n, t)]
        if len(accepted) < MIN_TEMPLATE_LABELS:
            continue
        sims = [max(_similarity(t, c) for c in _canon_labels(tpl, n)) for n, t in accepted]
        key = (len(accepted), sum(sims) / len(sims), -i, tpl)
        if best is None or key[:3] > best[:3]:
            best = key
    return best[3] if best else None


# --- Element (block eller <br/>-rad) ----------------------------------------------------


@dataclass
class _El:
    block: int
    line: int | None  # None = tabellcell eller hela blocket
    text: str
    bbox: tuple[float, float, float, float]
    number: str | None = None
    label: str = ""
    label_el: _El | None = None  # raden under ett ensamt nummer, som bär etikettexten
    # uppskattad teckenbredd: blockets bredd delad med dess längsta rad (texten antas
    # vänsterställd), för att placera ord och inre fältnummer i x-led
    char_w: float = 0.0

    @property
    def is_label(self) -> bool:
        return self.number is not None

    @property
    def starts_with_box(self) -> bool:
        return bool(_BOX_RE.match(self.text))

    @property
    def cy(self) -> float:
        return (self.bbox[1] + self.bbox[3]) / 2


@dataclass
class _Field:
    el: _El
    number: str
    raw_label: str
    values: list[str] = field(default_factory=list)
    options: list[tuple[str, bool]] = field(default_factory=list)
    chain: list[_El] = field(default_factory=list)
    free: bool = False
    resolved: bool = False  # etiketten är redan bestämd (ingen rad under tas som etikett)
    label_src: _El | None = None  # raden som bär etikettexten (annars etikettraden själv)


def _number_parts(text: str) -> list[tuple[str, str]]:
    """Fältnummer i början av en rad: ``[(nummer, etikett)]``.

    ``05 06 Utskriftsdatum`` ger två etiketter (``05`` tom, ``06`` med texten).
    """
    m = _NUMBER_RE.match(text.strip())
    if m is None or not 1 <= int(m.group(1)) <= MAX_FIELD_NUMBER:
        return []
    rest = (m.group(2) or "").strip()
    second = _NUMBER_RE.match(rest)
    if second is not None and 1 <= int(second.group(1)) <= MAX_FIELD_NUMBER:
        return [(m.group(1), ""), (second.group(1), (second.group(2) or "").strip())]
    return [(m.group(1), rest)]


def _split_boxes(text: str) -> tuple[str, list[tuple[str, bool]]]:
    """Text före första kryssrutan och alternativen ``(text, ikryssad)``."""
    parts = _BOX_RE.split(text)
    boxes = _BOX_RE.findall(text)
    options = [(p.strip(), b == CHECKBOX_ON) for b, p in zip(boxes, parts[1:], strict=True)]
    return parts[0].strip(), options


def _clean_label(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("*", "")).strip().rstrip(":").rstrip()


_INLINE_HYPHEN_RE = re.compile(r"(?<=\w)- (?=[a-zåäöéü])(?!(?:och|eller|som|till|samt)\b)")


def _join_inline_hyphens(text: str) -> str:
    """Avstavning där radbrottet redan blivit mellanslag (tabellceller, hopslagna värden)."""
    return _INLINE_HYPHEN_RE.sub("", text)


def _elements(blocks: list[LayoutBlock]) -> list[_El]:
    """Varje rad i textblocken med en uppskattad bbox (raderna delar blockets höjd)."""
    out: list[_El] = []
    for i, b in enumerate(blocks):
        if b.kind in _SKIP_KINDS or b.bbox is None or b.rows is not None or b.fields is not None:
            continue
        lines = b.text.split("\n")
        x0, y0, x1, y1 = b.bbox
        step = (y1 - y0) / max(1, len(lines))
        char_w = (x1 - x0) / max(1, *(len(ln.strip()) for ln in lines))
        for j, ln in enumerate(lines):
            if not ln.strip():
                continue
            out.append(_El(i, j, ln.strip(), (x0, y0 + j * step, x1, y0 + (j + 1) * step), char_w=char_w))
    return out


def _same_row(a: _El, b: _El) -> bool:
    """Rutorna överlappar lodrätt minst ``ROW_OVERLAP`` av den lägre rutans höjd."""
    overlap = min(a.bbox[3], b.bbox[3]) - max(a.bbox[1], b.bbox[1])
    lower = min(a.bbox[3] - a.bbox[1], b.bbox[3] - b.bbox[1])
    return lower > 0 and overlap >= ROW_OVERLAP * lower


def _text_right(e: _El) -> float:
    """Uppskattad högerkant för radens text (blockets ruta kan vara bredare)."""
    if e.char_w <= 0:
        return e.bbox[2]
    return min(e.bbox[2], e.bbox[0] + len(e.text) * e.char_w)


def _split_two_labels(e: _El, n1: str, rest: str, tpl: FormTemplate) -> list[_El] | None:
    """En etikettrad med två fältnummer (``05 datum 06 klockan``) som två etiketter.

    Det inre numret ska finnas i mallen och vara högre än det första, och båda
    halvorna ska likna sina nummers etiketter. Gränsen i x-led uppskattas ur
    teckenbredden; ``None`` när raden inte är två etiketter.
    """
    offset = len(e.text) - len(rest)
    for m in re.finditer(r"(?<=\s)(\d{2})(?=\s+\S)", rest):
        n2 = m.group(1)
        if n2 not in tpl.labels or int(n2) <= int(n1):
            continue
        left, right = rest[: m.start()].strip(), rest[m.end():].strip()
        if not (_accepts(tpl, n1, left) and _accepts(tpl, n2, right)):
            continue
        x = e.bbox[0] + (offset + m.start()) * e.char_w if e.char_w > 0 else (e.bbox[0] + e.bbox[2]) / 2
        x = min(max(x, e.bbox[0] + 1), e.bbox[2] - 1)
        x0, y0, x1, y1 = e.bbox
        return [
            dataclasses.replace(e, bbox=(x0, y0, x, y1), number=n1, label=left),
            dataclasses.replace(e, bbox=(x, y0, x1, y1), number=n2, label=right),
        ]
    return None


def _strip_boxes(text: str) -> str:
    return re.sub(r"\s+", " ", _BOX_RE.sub(" ", text)).strip()


def _label_els(
    els: list[_El], tpl: FormTemplate, next_below: Any
) -> tuple[list[_El], list[_El]]:
    """Etikettelement och spärrar.

    En rad med en etikett godtas när etiketten liknar
    mallens. En rad med två nummer (``05 06 Utskriftsdatum``) blir ett etikettpar
    bara när texten liknar det andra numrets etikett. Ett ensamt nummer blir en
    etikett bara när raden närmast under i samma spalt liknar numrets etikett;
    den raden bär då etikettexten. Övriga numrerade rader är vanlig text, men en
    rad vars nummer finns i mallen är ändå en spärr för värdekedjor, så att en
    OCR-skadad etikett ("10 dak") inte låter fältet ovanför svälja brödtexten.
    """
    out: list[_El] = []
    barriers: list[_El] = []
    for e in els:
        parts = _number_parts(e.text)
        if not parts:
            continue
        (n1, l1), *second = parts
        if second:
            n2, l2 = second[0]
            if n1 in tpl.labels and _accepts(tpl, n2, l2):
                out += [dataclasses.replace(e, number=n1, label=""), dataclasses.replace(e, number=n2, label=l2)]
                continue
        elif _norm(l1):
            pair = _split_two_labels(e, n1, l1, tpl)
            if pair is not None:
                out += pair
                continue
            if _accepts(tpl, n1, l1):
                out.append(dataclasses.replace(e, number=n1, label=l1))
                continue
        else:
            nb = next_below(e)
            if nb is not None and _accepts(tpl, n1, _strip_boxes(nb.text)):
                out.append(dataclasses.replace(e, number=n1, label="", label_el=nb))
                continue
        if n1 in tpl.labels:
            barriers.append(e)
    return out, barriers


# mening mitt i texten: ! ? ; följt av mer text, eller . : följt av ett gement ord
_SENTENCE_INSIDE_RE = re.compile(r"[!?;]\s+\S|[.:]\s+[a-zåäöéü]")


def _value_like(text: str) -> bool:
    """Texten ser ut som ett ifyllt värde och inte som prosa.

    Den innehåller en siffra (datum, klockslag, telefon, diarienummer), eller består
    av högst ``VALUE_LIKE_MAX_WORDS`` ord där minst ett börjar med versal och högst
    ett börjar med gemen (namn, initialer, koder som ``EBE``, en titel som
    ``krinsp``). Ingen mening får sluta mitt i texten. ``nej``, ``sa hon till slut.``,
    ``springa mot`` och ``Hon gick sedan hem.`` är prosa.
    """
    words = text.split()
    if not words or _SENTENCE_INSIDE_RE.search(text):
        return False
    if any(ch.isdigit() for ch in text):
        return True
    upper = sum(w[0].isupper() for w in words)
    lower = sum(w[0].isalpha() and w[0].islower() for w in words)
    return len(words) <= VALUE_LIKE_MAX_WORDS and upper >= 1 and lower <= 1


def _lone_title(words: list[str]) -> bool:
    """Ett ensamt kort gement ord (en titel som ``krinsp``) före ett nummer på samma
    rad; används bara när raden har en siffra (``krinsp 08-7694169``)."""
    return len(words) == 1 and words[0].isalpha() and len(words[0]) < SHORT_LABEL_CHARS


def _label_word_runs(words: list[str], tpl: FormTemplate) -> set[int]:
    """Index för ord i en värderad som är mallens tryck: en följd av högst
    ``MAX_LABEL_RUN`` ord som som helhet liknar en etikett, ett alias eller fast
    tryck ("Förhörsprot Promemoria 920114/GT" under 05/06: bara datumet är ett
    värde). De står kvar som text i stället för att bli fältvärden."""
    canons = [c for n in tpl.labels for c in _canon_labels(tpl, n)]
    out: set[int] = set()
    i = 0
    while i < len(words):
        for n in range(min(MAX_LABEL_RUN, len(words) - i), 0, -1):
            text = " ".join(words[i : i + n])
            if any(_similarity(text, c) >= _threshold(c) for c in canons) or _is_printed(text, tpl):
                out.update(range(i, i + n))
                i += n
                break
        else:
            i += 1
    return out


def _alias_labels(els: list[_El], labels: list[_El], tpl: FormTemplate, free: set[str]) -> list[_El]:
    """Mallens alias för ett fält som egen etikett bredvid fältets nummer.

    Variant 2 trycker "Utskriftsdatum" i en ruta till höger om "06", vars etikett
    ("Promemoria") står under numret. En onumrerad rad som som helhet liknar ett
    alias för fält N blir en etikett för N när den står på samma rad som en godtagen
    etikett N och till höger om den; värdet under den hör då till fält N.
    """
    used = {id(e.label_el) for e in labels if e.label_el is not None}
    out = []
    for c in els:
        if _number_parts(c.text) or id(c) in used:
            continue
        for number, names in tpl.aliases.items():
            if number in free or not any(_similarity(c.text, a) >= _threshold(a) for a in names):
                continue
            if any(e.number == number and _same_row(e, c) and c.bbox[0] > e.bbox[2] for e in labels):
                out.append(dataclasses.replace(c, number=number, label=c.text))
                break
    return out


def _order_outliers(labels: list[_El], rise: float, tol: float = 0.0) -> list[_El]:
    """Etiketter som bryter nummerordningen (blanketten trycker fälten i ordning,
    så en etikett i fel ordning är oftast prosa som liknar en etikett).

    Ett brott är ett par där etiketten med högre nummer står mer än ``rise``
    ovanför den med lägre. Etiketten med flest brott avvisas, och räkningen görs
    om tills inga brott återstår. Vid lika antal brott avvisas, i tur och ordning,
    den vars nummer ligger längst från de rumsliga grannarnas (närmaste etikett
    ovanför och under i samma spalt, som står i fel ordning mot den), den som
    står längst i y från sina konsekventa nummergrannar, och till sist den med
    högst nummer – så en lokalt konsekutiv följd riktiga etiketter står kvar.
    """
    kept = list(labels)
    out: list[_El] = []

    def num(e: _El) -> int:
        return int(e.number or 0)

    def violates(hi: _El, lo: _El) -> bool:
        return num(hi) > num(lo) and lo.bbox[1] - hi.bbox[1] > rise

    def same_column(a: _El, b: _El) -> bool:
        return a.bbox[0] <= b.bbox[2] + tol and b.bbox[0] <= a.bbox[2] + tol

    def local_disorder(e: _El) -> int:
        """Nummeravstånd till närmaste spaltgranne ovanför och under, när grannen
        står i fel ordning (ovanför med högre eller lika nummer, under med lägre)."""
        col = [o for o in kept if o is not e and same_column(e, o)]
        above = [o for o in col if o.bbox[1] < e.bbox[1]]
        below = [o for o in col if o.bbox[1] > e.bbox[1]]
        cost = 0
        if above:
            a = max(above, key=lambda o: o.bbox[1])
            cost += abs(num(e) - num(a)) if num(a) >= num(e) else 0
        if below:
            b = min(below, key=lambda o: o.bbox[1])
            cost += abs(num(e) - num(b)) if num(b) <= num(e) else 0
        return cost

    def neighbour_gap(e: _El) -> float:
        """Avstånd i y till närmaste nummergranne bland etiketterna som inte bryter
        ordningen mot ``e`` (en konsekvent etikett står nära sina grannar)."""
        n = num(e)
        peers = [o for o in kept if o is not e and not violates(e, o) and not violates(o, e)]
        lower = [o for o in peers if num(o) < n]
        higher = [o for o in peers if num(o) > n]
        gaps = []
        if lower:
            m = max(num(o) for o in lower)
            gaps += [abs(e.bbox[1] - o.bbox[1]) for o in lower if num(o) == m]
        if higher:
            m = min(num(o) for o in higher)
            gaps += [abs(e.bbox[1] - o.bbox[1]) for o in higher if num(o) == m]
        return min(gaps, default=float("inf"))

    while True:
        counts = [sum(violates(e, o) or violates(o, e) for o in kept) for e in kept]
        if not counts or max(counts) == 0:
            return out
        worst = max(
            range(len(kept)),
            key=lambda i: (counts[i], local_disorder(kept[i]), neighbour_gap(kept[i]), num(kept[i])),
        )
        out.append(kept.pop(worst))


def _footer_rejects(labels: list[_El], free_max: int, height: float, tol: float) -> list[_El]:
    """Sidfotsetiketter (nummer över fritextnumret) som inte hör till sidfoten.

    Sidfoten är ett fast, tätt och strikt ordnat band längst ned; en vittnesrad
    kan likna en enskild sidfotsetikett men inte ett helt sådant band.

    Sidfoten är ett kluster: minst tre olika nummer vars överkant ligger inom
    ``FOOTER_WINDOW`` × sidhöjden från den lägsta kandidatens. Kandidater ovanför
    fönstret är vanlig text. I klustret är ordningen strikt (``FOOTER_MAX_RISE``)
    och brott löses som i ``_order_outliers``; av två lika nummer behålls det
    lägsta på sidan. Utan kluster blir inga sidfotsfält alls.
    """
    footer = [e for e in labels if int(e.number or 0) > free_max]
    if not footer:
        return []
    lowest = max(e.bbox[1] for e in footer)
    window = [e for e in footer if e.bbox[1] >= lowest - FOOTER_WINDOW * height]
    out = [e for e in footer if not any(e is w for w in window)]
    out += _order_outliers(window, FOOTER_MAX_RISE * height, tol)
    cluster = [e for e in window if not any(e is r for r in out)]
    by_number: dict[str, _El] = {}
    for e in sorted(cluster, key=lambda e: e.bbox[1]):
        if e.number in by_number:
            out.append(by_number[e.number])  # dubblett: den lägre på sidan behålls
        by_number[e.number or ""] = e
    if len(by_number) < FOOTER_MIN_NUMBERS:
        return footer
    return out


def _table_fields(
    blocks: list[LayoutBlock], tpl: FormTemplate
) -> list[tuple[int, list[tuple[str, str, str]], list[str]]]:
    """Tabeller där varje icke-tom cell är etikett eller värdet under en etikett.

    Returnerar ``(blockindex, [(nummer, etikett, värde)], kvarvarande texter)``;
    andra tabeller lämnas. Ett fritextfälts värdecell förbrukas aldrig: den är
    löptext (ett vittnesmål), inte ett fältvärde, och blir ett eget stycke efter
    blankettblocket i ``kvarvarande texter``.
    """
    out = []
    for i, b in enumerate(blocks):
        if b.rows is None or b.fields is not None:
            continue
        grid = [[c.text.strip() for c in row] for row in b.rows]

        def label_of(grid: list[list[str]], r: int, c: int) -> tuple[str, str] | None:
            parts = _number_parts(grid[r][c])
            if len(parts) != 1:
                return None
            number, label = parts[0]
            if not _norm(label):  # ensamt nummer: cellen under ska vara etiketten
                below = grid[r + 1][c] if r + 1 < len(grid) and c < len(grid[r + 1]) else ""
                return parts[0] if _accepts(tpl, number, below) else None
            return parts[0] if _accepts(tpl, number, label) else None

        used: set[tuple[int, int]] = set()
        kept: list[tuple[int, int]] = []  # fritextfältens värdeceller
        found: list[tuple[str, str, str]] = []
        for r, row in enumerate(grid):
            for c in range(len(row)):
                parts = label_of(grid, r, c)
                if parts is None:
                    continue
                used.add((r, c))
                number, label = parts
                value = ""
                below = grid[r + 1][c] if r + 1 < len(grid) and c < len(grid[r + 1]) else ""
                if below and not _number_parts(below) and not _is_printed(below, tpl):
                    if label and number in tpl.free_text:
                        kept.append((r + 1, c))
                    else:
                        used.add((r + 1, c))
                        if label:
                            value = below
                        else:
                            label = below
                found.append((number, _clean_label(label), value))
        cells = {(r, c) for r, row in enumerate(grid) for c, t in enumerate(row) if t}
        if found and cells <= used | set(kept):
            out.append((i, found, [grid[r][c] for r, c in sorted(kept) if (r, c) not in used]))
    return out


# --- Fälttolkning ---------------------------------------------------------------------


def extract_form_fields(
    blocks: list[LayoutBlock],
    templates: list[FormTemplate],
    page_height: int | None = None,
    trace: dict[str, Any] | None = None,
) -> tuple[list[LayoutBlock], list[FormField]]:
    """Ta ut blankettfält ur sidans block (oavsett ``kind``).

    Returnerar övriga block (omnumrerade, med ett ``FORM``-block där det första
    etikettblocket stod) och fälten. Fält tas bara ut när en mall matchar sidan;
    utan mall eller etiketter returneras blocken oförändrade, eftersom numrerade
    rader i vanlig text (datum, tider, antal) annars skulle flyttas ur läsordningen.
    ``trace`` får mallens namn och antal godtagna/avvisade etiketter, spärrar och
    fält, samt antal kvarlevor på etikettrader som blev värden
    (``label_rest_values``) respektive stod kvar som text (``label_rest_text``),
    etikettrader med två nummer (``label_pairs``), aliasetiketter
    (``alias_labels``), värden som tagits ur en etikettrads spalter
    (``row_values``, varav delade: ``row_splits``), prosarader under en etikettrad
    som lämnades som text (``row_prose``) och värden bredvid etiketten
    (``right_values``); ingen dokumenttext.
    """
    stats: dict[str, Any] = {
        "template": None, "accepted_labels": 0, "fields": 0, "rejected_labels": 0, "barriers": 0,
        "label_rest_values": 0, "label_rest_text": 0, "label_words_text": 0,
        "label_pairs": 0, "alias_labels": 0, "row_values": 0, "row_splits": 0, "right_values": 0,
        "row_prose": 0,
    }
    if trace is not None:
        trace.update(stats)
        stats = trace
    els = _elements(blocks)
    candidates = [p for e in els for p in _number_parts(e.text)[:1]]
    candidates += [
        p for b in blocks if b.rows is not None for row in b.rows for c in row for p in _number_parts(c.text)[:1]
    ]
    if not candidates:
        return blocks, []
    page_words = " ".join(b.text for b in blocks).upper()
    template = _choose_template(
        [(n, _clean_label(t)) for n, t in candidates], page_words, templates
    )
    if template is None:
        return blocks, []
    stats["template"] = template.name
    boxes = [b.bbox for b in blocks if b.bbox is not None]
    height = page_height or max((bb[3] for bb in boxes), default=0) or 1
    width = max((bb[2] for bb in boxes), default=0) or 1
    tol = COLUMN_TOLERANCE * width
    max_gap = VALUE_MAX_GAP * height

    def overlaps(a: _El, b: _El) -> bool:
        return a.bbox[0] <= b.bbox[2] + tol and b.bbox[0] <= a.bbox[2] + tol

    def below(a: _El, e: _El) -> bool:
        ah = a.bbox[3] - a.bbox[1]
        return e.bbox[1] >= a.bbox[1] + ah / 2 and e.bbox[1] - a.bbox[3] <= max_gap

    def beside(a: _El, e: _El) -> bool:
        same_row = a.bbox[1] < e.bbox[3] and e.bbox[1] < a.bbox[3]
        return a.starts_with_box and e.starts_with_box and same_row and 0 <= e.bbox[0] - a.bbox[2] <= max_gap

    def next_below(e: _El) -> _El | None:
        """Närmaste onumrerade rad under ``e`` i samma spalt (som börjar högst
        toleransen till vänster om ``e``, så en stämpel i marginalen inte räknas)."""
        below_els = [
            c
            for c in els
            if c is not e
            and not _number_parts(c.text)
            and not _is_turn(c.text)
            and c.bbox[0] >= e.bbox[0] - tol
            and below(e, c)
            and overlaps(e, c)
        ]
        return min(below_els, key=lambda c: c.bbox[1] - e.bbox[3], default=None)

    labels, barriers = _label_els(els, template, next_below)
    stats["barriers"] = len(barriers)
    free = set(template.free_text)

    # under fritextfältets rad står löptext: där godtas bara etiketter med högre
    # nummer (sidfoten), så att en vittnesrad som "05 datum minns han inte" är text
    free_lines = [e for e in [*labels, *barriers] if _number_parts(e.text)[0][0] in free]
    cutoff = min((e.bbox[1] for e in free_lines), default=None)
    free_max = max((int(n) for n in free), default=0)

    def after_cutoff(y: float, number: str) -> bool:
        return cutoff is not None and y > cutoff and int(number) <= free_max

    rejected = [e for e in labels if after_cutoff(e.bbox[1], e.number or "0")]
    # sidfotsetiketterna (19–25 i variant 1) godtas bara som ett kluster (se _footer_rejects)
    if free:
        kept = [e for e in labels if not any(e is r for r in rejected)]
        rejected += _footer_rejects(kept, free_max, height, tol)
    # ordningsbrott löses genom att etiketten med flest brott avvisas, en i taget,
    # så att en ensam felplacerad etikett faller och inte den riktiga följden
    kept = [e for e in labels if not any(e is r for r in rejected)]
    rejected += _order_outliers(kept, ORDER_MAX_RISE * height, tol)
    stats["rejected_labels"] = len(rejected)
    if rejected:
        labels = [e for e in labels if not any(e is r for r in rejected)]
        barriers += rejected
    aliases = _alias_labels(els, labels, template, free)
    labels += aliases
    stats["alias_labels"] = len(aliases)
    # rader med två fältnummer som blev två etiketter (samma rad bär båda)
    stats["label_pairs"] = len(labels) - len({(e.block, e.line) for e in labels})
    tables = [
        t
        for t in _table_fields(blocks, template)
        if not any(after_cutoff(blocks[t[0]].bbox[1] if blocks[t[0]].bbox else 0, n) for n, _, _ in t[1])
    ]
    stats["accepted_labels"] = len(labels) + sum(len(found) for _, found, _ in tables)
    if not labels and not tables:
        return blocks, []
    # repliker och långa rader är löptext och avbryter därför värdekedjor
    stoppers = [e for e in els if not _number_parts(e.text) and (_is_turn(e.text) or len(e.text) > VALUE_MAX_CHARS)]
    # fast tryck (en rutas rubrik, "Uppgiftslämnare (efternamn och förnamn)") inleder en
    # ny ruta och avbryter därför värdekedjor som passerar den
    printed_els = [e for e in els if not _number_parts(e.text) and _is_printed(_strip_boxes(e.text), template)]

    consumed: set[tuple[int, int | None]] = {(e.block, e.line) for e in labels}
    fields = []
    for e in labels:
        number = e.number or ""
        if e.label_el is not None:
            # ett ensamt nummer: raden under är den tryckta etiketten (ev. med kryssruta)
            prefix, options = _split_boxes(e.label_el.text)
            f = _Field(
                e, number, "" if options else prefix, options=options, free=number in free, resolved=True,
                label_src=e.label_el,
            )
            f.chain.append(e.label_el)
            consumed.add((e.label_el.block, e.label_el.line))
        else:
            prefix, options = _split_boxes(e.label)
            f = _Field(e, number, prefix, options=options, free=number in free)
        fields.append(f)

    def blocked(f: _Field, a: _El, e: _El) -> bool:
        """En etikett, spärr, löptext eller tryckt rubrik mellan ``a`` och ``e`` i
        samma spalt avbryter kedjan; en godtagen etikett på samma rad som ``a``
        (grannfältet bredvid) eller en tryckt rubrik på samma rad ("telefon*") gör det
        inte, men en spärr (avvisad etikett) eller löptext gör det alltid."""
        def exempt(other: _El, is_label: bool) -> bool:
            return is_label and _same_row(other, a)  # grannetiketter och tryckta rubriker, aldrig löptext eller spärrar

        return any(
            other.bbox != f.el.bbox and overlaps(other, e) and a.cy < other.cy < e.cy and not exempt(other, is_label)
            for others, is_label in ((labels, True), (printed_els, True), (barriers, False), (stoppers, False))
            for other in others
        )

    def collectable(e: _El) -> bool:
        # en etikettrad bakom rutans linje ("| 25 Spanchef: sign") är inte heller ett värde
        if len(e.text) > VALUE_MAX_CHARS or _number_parts(e.text.lstrip("| ")) or _is_turn(e.text):
            return False
        prefix, options = _split_boxes(e.text)
        if options and not all(lbl for lbl, _ in options):
            return False  # en kryssruta utan text är inget alternativ
        return not any(_is_printed(t, template) for t in [prefix, *(lbl for lbl, _ in options)])

    reach = ROW_VALUE_REACH * width
    label_keys = set(consumed)
    # kvarlevor på förbrukade rader (etikettrader, tryckta ord i en värderad):
    # (block, rad) -> text i läsordning
    leftovers: dict[tuple[int, int | None], list[str]] = {}

    def can_take(f: _Field, e: _El) -> bool:
        """Fältet kan ta ``e`` som (ännu ett) värde enligt fritext-, sidfots- och kedjereglerna."""
        if f.free or len(f.values) >= MAX_VALUE_LINES:
            return False
        if free and int(f.number) > free_max and len(e.text) > FOOTER_VALUE_MAX_CHARS:
            return False
        if cutoff is not None and f.el.bbox[1] < cutoff < e.bbox[1]:
            return False
        return bool(f.raw_label) or f.resolved  # ett ensamt nummer väntar på sin etikett

    def right_edge(f: _Field) -> float:
        src = f.label_src
        return max(f.el.bbox[2], src.bbox[2] if src is not None and _same_row(src, f.el) else 0)

    def row_of(f: _Field) -> list[_Field]:
        return sorted((g for g in fields if not g.free and _same_row(g.el, f.el)), key=lambda g: g.el.bbox[0])

    def spans(row: list[_Field]) -> list[tuple[float, float]]:
        """Etikettens spalt: från dess vänsterkant till nästa etikett på raden, men
        högst ``reach`` till höger om etikettens högerkant."""
        out = []
        for k, g in enumerate(row):
            end = right_edge(g) + reach
            if k + 1 < len(row):
                end = min(end, row[k + 1].el.bbox[0] - tol)
            out.append((g.el.bbox[0] - tol, end))
        return out

    def column(x: float, sp: list[tuple[float, float]]) -> int | None:
        return next((k for k, (a, b) in enumerate(sp) if a <= x < b), None)

    def centre(e: _El) -> float:
        return (e.bbox[0] + _text_right(e)) / 2

    def words_x(e: _El) -> list[tuple[str, float, float]]:
        """Radens ord med uppskattad vänster- och högerkant (teckenbredden)."""
        cw = e.char_w if e.char_w > 0 else (e.bbox[2] - e.bbox[0]) / max(1, len(e.text))
        return [(m.group(0), e.bbox[0] + m.start() * cw, e.bbox[0] + m.end() * cw) for m in re.finditer(r"\S+", e.text)]

    def row_above(e: _El) -> list[_Field] | None:
        """Etikettraden (minst två etiketter) närmast ovanför ``e``, när ``e`` står under den."""
        near = [
            (e.bbox[1] - f.el.bbox[3], f)
            for f in fields
            if not f.free and below(f.el, e) and not blocked(f, f.el, e)
        ]
        if not near:
            return None
        row = row_of(min(near, key=lambda t: t[0])[1])
        return row if len(row) >= 2 else None

    def taken_columns(row: list[_Field], e: _El) -> set[int]:
        """Spalter som har ett eget värde under raden (en annan rad än ``e``). Här
        räcker att radens mitt står mellan etikettens och nästa etiketts vänsterkant."""
        sp = [(g.el.bbox[0] - tol, row[k + 1].el.bbox[0] - tol if k + 1 < len(row) else float("inf"))
              for k, g in enumerate(row)]
        out = set()
        for c in els:
            if c is e or (c.block, c.line) in label_keys or _BOX_RE.search(c.text) or not collectable(c):
                continue
            if any(below(g.el, c) and not blocked(g, g.el, c) for g in row):
                k = column(centre(c), sp)
                if k is not None:
                    out.add(k)
        return out

    def row_assign(e: _El) -> bool:
        """Värde under en etikettrad: hör till spalten där radens text har sin mitt.
        Når texten in i grannspalter som saknar eget värde delas den ord för ord efter
        spalterna ("Ingvar Kjellvås 08-7694169" under 03 och 04)."""
        if _BOX_RE.search(e.text):
            return False  # kryssrutor tolkas av den vanliga kedjan (alternativ, inte värde)
        row = row_above(e)
        if row is None or any(not g.raw_label and not g.resolved for g in row):
            return False  # ett ensamt nummer på raden väntar på sin etikett från raden under
        sp = spans(row)
        own = column(centre(e), sp)
        if own is None or not can_take(row[own], e):
            return False
        words = words_x(e)
        printed = _label_word_runs([w for w, _, _ in words], template)
        if all(i in printed or not any(ch.isalnum() for ch in w) for i, (w, _, _) in enumerate(words)):
            return False  # bara tryckt text (och skiljetecken): ingen radregel
        cols = [column((x0 + x1) / 2, sp) for _, x0, x1 in words]
        split = None not in cols and len(set(cols)) > 1 and cols == sorted(c or 0 for c in cols)
        if split:
            taken = taken_columns(row, e)
            split = all(k == own or (k not in taken and can_take(row[k], e)) for k in set(cols) if k is not None)
        if split:
            # delas bara i värdeformade delar (siffror, koder, namn); prosa hålls ihop
            parts: dict[int, list[str]] = {}
            for (word, _, _), k in zip(words, cols, strict=True):
                parts.setdefault(k or 0, []).append(word)
            has_digit = any(ch.isdigit() for ch in e.text)
            split = all(_value_like(" ".join(ws)) or (has_digit and _lone_title(ws)) for ws in parts.values())
            if not split and not _value_like(e.text):
                # prosa som når över flera spalter är inget värde: raden står kvar som
                # vanlig text på sin plats i läsordningen (beslut BL)
                stats["row_prose"] += 1
                return True
        if not split:
            cols = [own] * len(words)
        if e.bbox[0] < row[cols[0] or 0].el.bbox[0] - ROW_VALUE_LEFT * width:
            return False  # börjar långt till vänster om sin spalt: inte radens värde
        groups: list[tuple[int, list[tuple[str, float, float]]]] = []
        kept_text: list[str] = []  # tryckta ord står kvar som text på radens plats
        for i, (w, k) in enumerate(zip(words, cols, strict=True)):
            if i in printed:
                if i - 1 in printed and kept_text:
                    kept_text[-1] += " " + w[0]
                else:
                    kept_text.append(w[0])
                continue
            if groups and groups[-1][0] == k and i - 1 not in printed:
                groups[-1][1].append(w)
            else:
                groups.append((k or 0, [w]))
        if kept_text:
            leftovers.setdefault((e.block, e.line), []).extend(kept_text)
        stats["row_values"] += 1
        stats["row_splits"] += len(groups) > 1
        for k, ws in groups:
            f = row[k]
            part = e if len(groups) == 1 else dataclasses.replace(e, bbox=(ws[0][1], e.bbox[1], ws[-1][2], e.bbox[3]))
            f.values.append(" ".join(w for w, _, _ in ws))
            f.chain.append(part)
        consumed.add((e.block, e.line))
        return True

    def right_of(e: _El) -> _Field | None:
        """Ett tomt fält vars etikett står på samma rad till vänster om ``e``, högst
        ``reach`` bort och inom etikettens spalt (sidfotens "20 Tagit del  860308")."""
        if _BOX_RE.search(e.text) or not _value_like(e.text):
            return None  # bara värdeformad text (siffror, koder, namn), aldrig prosa
        best: tuple[float, _Field] | None = None
        for f in fields:
            if f.values or f.options or not can_take(f, e) or not _same_row(f.el, e):
                continue
            gap = e.bbox[0] - right_edge(f)
            if not -tol <= gap <= reach:
                continue
            row = row_of(f)
            k = next(i for i, g in enumerate(row) if g is f)
            if k + 1 < len(row) and centre(e) >= row[k + 1].el.bbox[0] - tol:
                continue
            if best is None or gap < best[0]:
                best = (gap, f)
        return best[1] if best else None

    rest = sorted((e for e in els if (e.block, e.line) not in consumed), key=lambda e: (e.bbox[1], e.bbox[0]))
    for e in rest:
        if not collectable(e):
            continue
        if row_assign(e):
            continue
        best: tuple[float, float, _Field] | None = None
        for f in fields:
            if f.free or f.el.bbox[0] > e.bbox[0] + tol:
                continue
            if free and int(f.number) > free_max and len(e.text) > FOOTER_VALUE_MAX_CHARS:
                continue  # sidfotsvärden är korta (signatur, datum); repliker är aldrig värden
            if cutoff is not None and f.el.bbox[1] < cutoff < e.bbox[1]:
                continue  # kedjan korsar aldrig fritextfältets rad
            if len(f.values) >= MAX_VALUE_LINES and not _split_boxes(e.text)[1]:
                continue
            gaps = []
            for a in [f.el, *f.chain]:
                if below(a, e) and overlaps(a, e) and not blocked(f, a, e):
                    gaps.append(e.bbox[1] - a.bbox[3])
                elif beside(a, e):
                    gaps.append(0.0)
            if gaps:
                key = (f.el.bbox[0], -min(gaps), f)
                if best is None or key[:2] > best[:2]:
                    best = key
        if best is None:
            right = right_of(e)
            if right is not None:
                stats["right_values"] += 1
                right.values.append(e.text)
                right.chain.append(e)
                consumed.add((e.block, e.line))
            continue
        f = best[2]
        prefix, options = _split_boxes(e.text)
        if not f.raw_label and not f.chain and not options and not f.resolved:
            f.raw_label = prefix  # numret står ovanför den tryckta etiketten
            f.label_src = e
        else:
            if prefix:
                f.values.append(prefix)
            f.options += options
        f.chain.append(e)
        consumed.add((e.block, e.line))

    result: list[tuple[str, FormField, list[FormField], tuple[int, int, int, int] | None]] = []
    table_left: dict[int, list[str]] = {}
    rest_stats = {"label_rest_values": 0, "label_rest_text": 0, "label_words_text": 0}

    def label_for(number: str, raw_label: str) -> tuple[str, str, bool]:
        """(etikett, kvarleva, rå): mallens stavning och texten efter den på raden.

        ``rå`` betyder att etiketten inte liknade mallens tillräckligt för att
        rättas och står kvar som den lästes (hela raden, ingen kvarleva).
        """
        raw_label = _join_inline_hyphens(_clean_label(raw_label))
        canons = _canon_labels(template, number)
        if not canons:
            return raw_label, "", bool(raw_label)
        if not raw_label:
            return canons[0], "", False
        ratio, canon, rest = max((_label_match(raw_label, c) for c in canons), key=lambda m: m[0])
        if ratio < LABEL_MATCH:
            return raw_label, "", True
        tail, remainder = _split_rest(rest)
        return _clean_label(canon + tail), remainder, False

    def loose_text(number: str, value: str, label: str, remainder: str, raw: bool) -> str | None:
        """Text som ska stå kvar som vanlig text: en kvarleva som inte blev värdet,
        eller en orättad etikett vars fält är tomt (ett tomt fält skrivs inte till
        Markdown och får aldrig vara textens enda bärare)."""
        if remainder and value != remainder:
            return remainder
        if raw and not value:
            return f"{number} {label}"
        return None

    def safety_net(
        number: str, raw_label: str, label: str, value: str, options: list[tuple[str, bool]], loose: list[str]
    ) -> str:
        """Beslut BG/BI: ord på etikettraden som inte syns någonstans (inte i värdet,
        inte i lös text och inte i etiketten på ett fält som skrivs till Markdown) och
        som justeringen mot mallen inte täcker. Ett fält skrivs bara när det har ett
        värde eller en ikryssad ruta (``surya_layout._form_md``)."""
        rendered_field = bool(value.strip()) or any(c for _, c in options)
        shown = _word_list(value) + [w for t in loose for w in _word_list(t)]
        if rendered_field:
            shown += _word_list(label)
        text = _join_inline_hyphens(_clean_label(raw_label))
        covered = _template_cover(text, _canon_labels(template, number), template)
        return _unrendered_words(text, covered, shown)

    def source_line(number: str, raw_label: str, label: str, value: str) -> str:
        """Etikettradens råa text när något ord i den saknas i fältets rad
        (``NN etikett: värde``), annars tom: ren text får aldrig tappa ett ord från
        raden, vad Markdown än gör (beslut BI)."""
        raw = _join_inline_hyphens(_clean_label(raw_label))
        have: dict[str, int] = {}
        for w in _word_list(f"{number} {label} {value}"):
            have[w] = have.get(w, 0) + 1
        for w in _word_list(raw):
            if have.get(w, 0) <= 0:
                return f"{number} {raw}"
            have[w] -= 1
        return ""

    def place_rest(number: str, value: str, remainder: str) -> str:
        """Fältets värde: kvarlevan blir värdet när fältet inte fick något värde
        underifrån och kvarlevan ser ut som ett ifyllt värde (kort, inte fast
        tryck, inte ett fältnummer med etikett som ``04 telefon`` – ett ensamt tal som
        ``12`` är ett värde). Annars returneras ``value`` och anroparen
        behåller kvarlevan som vanlig text."""
        if (
            remainder
            and not value
            and number not in free
            and len(remainder) <= LABEL_REST_MAX_CHARS
            and not _is_printed(remainder, template)
            and not (_number_parts(remainder) and not re.fullmatch(r"\d{2}", remainder))
        ):
            rest_stats["label_rest_values"] += 1
            return _join_inline_hyphens(remainder)
        return value

    for f in fields:
        value = _join_inline_hyphens(" ".join(v for v in f.values if v)) if not f.free else ""
        label, remainder, raw = label_for(f.number, f.raw_label)
        new_value = place_rest(f.number, value, remainder)
        loose = loose_text(f.number, new_value, label, remainder, raw and not f.options)
        texts = [loose] if loose is not None else []
        extra = safety_net(f.number, f.raw_label, label, new_value, f.options, texts)
        if extra:
            texts.append(extra)
            rest_stats["label_words_text"] += 1
        if texts:
            src = f.label_src or f.el
            leftovers.setdefault((src.block, src.line), []).extend(texts)
            rest_stats["label_rest_text"] += loose is not None
        head = FormField(f.number, label, new_value, None, source_line(f.number, f.raw_label, label, new_value))
        opts = [FormField(f.number, _join_inline_hyphens(lbl), "", checked) for lbl, checked in f.options]
        result.append((f.number, head, opts, _union([f.el.bbox, *(c.bbox for c in f.chain)])))
    for t, found, _ in tables:
        for number, lbl, value in found:
            value = "" if number in free else _join_inline_hyphens(value)
            label, remainder, raw = label_for(number, lbl)
            new_value = place_rest(number, value, remainder)
            loose = loose_text(number, new_value, label, remainder, raw)
            texts = [loose] if loose is not None else []
            extra = safety_net(number, lbl, label, new_value, [], texts)
            if extra:
                texts.append(extra)
                rest_stats["label_words_text"] += 1
            if texts:
                table_left.setdefault(t, []).extend(texts)
                rest_stats["label_rest_text"] += loose is not None
            # tabellceller har ingen egen geometri: fältets ruta är tabellens
            head = FormField(number, label, new_value, None, source_line(number, lbl, label, new_value))
            result.append((number, head, [], blocks[t].bbox))
    stats.update(rest_stats)
    result.sort(key=lambda r: int(r[0]))
    out_fields = [x for _, head, opts, _ in result for x in (head, *opts)]
    out_parts = tuple(bb for _, _, opts, bb in result for _ in range(1 + len(opts)))
    stats["fields"] = len(out_fields)

    # bygg om blocklistan: förbrukade rader bort, FORM-blocket där första etiketten stod
    table_rest = {i: table_left.get(i, []) + rest_texts for i, _, rest_texts in tables}
    label_blocks = {e.block for e in labels} | set(table_rest)
    first = min(label_blocks)
    used_blocks = {k[0] for k in consumed} | set(table_rest)
    bbox = _union([blocks[i].bbox for i in used_blocks if blocks[i].bbox is not None])
    form = LayoutBlock(
        BlockKind.FORM, fields_text(out_fields), 0, bbox, fields=tuple(out_fields), parts=out_parts
    )
    out: list[LayoutBlock] = []
    for i, b in enumerate(blocks):
        if i == first:
            out.append(form)
        if i in table_rest:
            # fritextcellernas löptext står kvar, efter blankettblocket
            out += [LayoutBlock(BlockKind.PARAGRAPH, t, 0, b.bbox) for t in table_rest[i]]
            continue
        if i in used_blocks:
            lines = b.text.split("\n")
            # en förbrukad etikettrad lämnar sin kvarleva (om den inte blev ett värde)
            left = [
                t
                for j, ln in enumerate(lines)
                if ln.strip()
                for t in ([ln] if (i, j) not in consumed else leftovers.get((i, j), []))
            ]
            if left:
                out.append(dataclasses.replace(b, text="\n".join(left)))
            continue
        out.append(b)
    return [dataclasses.replace(b, order=k) for k, b in enumerate(out)], out_fields


def _union(boxes: list[Any]) -> tuple[int, int, int, int] | None:
    """Minsta heltalsruta runt rutorna (``None`` utan rutor)."""
    if not boxes:
        return None
    return (
        int(round(min(b[0] for b in boxes))),
        int(round(min(b[1] for b in boxes))),
        int(round(max(b[2] for b in boxes))),
        int(round(max(b[3] for b in boxes))),
    )


# --- Maskeringar -----------------------------------------------------------------------


def _gray(image: Any) -> Any:
    import numpy as np

    if hasattr(image, "convert"):
        image = image.convert("L")
    arr = np.asarray(image)
    if arr.ndim == 3:
        arr = arr[..., :3].mean(axis=2)
    return arr


def filled_dark_boxes(image: Any, bbox: tuple[int, int, int, int]) -> int:
    """Antal fyllda mörka rektanglar inom bbox.

    En rektangel räknas när den är minst 60 % av radhöjden hög, minst lika bred
    som 60 % av radhöjden och till minst 80 % mörk. Radhöjden är medianen av de
    horisontella band som innehåller mörka pixlar. En ihålig ram (riktig
    kryssruta) och smala staplar (``l``, ``|``) räknas inte.
    """
    import numpy as np

    arr = _gray(image)
    h, w = arr.shape[:2]
    x0, y0, x1, y1 = (int(round(v)) for v in bbox)
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
    if x1 <= x0 or y1 <= y0:
        return 0
    dark = arr[y0:y1, x0:x1] < 128
    rows = dark.sum(axis=1) >= 2
    bands: list[int] = []
    run = 0
    for v in [*rows.tolist(), False]:
        if v:
            run += 1
        elif run:
            bands.append(run)
            run = 0
    if not bands:
        return 0
    row_h = float(np.median(bands))
    min_h = 0.6 * row_h
    # längsta lodräta mörka följd per kolumn
    longest = np.zeros(dark.shape[1], dtype=np.int32)
    current = np.zeros(dark.shape[1], dtype=np.int32)
    for row in dark:
        current = (current + 1) * row
        np.maximum(longest, current, out=longest)
    tall = np.concatenate([[False], longest >= min_h, [False]])
    edges = np.flatnonzero(np.diff(tall.astype(np.int8)))
    count = 0
    for c0, c1 in zip(edges[0::2], edges[1::2], strict=True):
        if c1 - c0 < min_h:
            continue
        seg = dark[:, c0:c1]
        filled_rows = np.flatnonzero(seg.mean(axis=1) >= 0.5)
        if filled_rows.size == 0:
            continue
        r0, r1 = int(filled_rows[0]), int(filled_rows[-1]) + 1
        if r1 - r0 >= min_h and seg[r0:r1].mean() >= 0.8:
            count += 1
    return count


def resolve_checkbox_masks(blocks: list[LayoutBlock], image: Any) -> list[LayoutBlock]:
    """``☐`` i block som inte är blankettfält blir ``[MASKAD]`` när blockets bbox
    i sidbilden har minst lika många fyllda mörka rektanglar som kryssrutor.

    Surya skriver små maskeringar i dialog som kryssrutor; en riktig kryssruta är
    en ihålig ram. Utan bild lämnas allt orört.
    """
    if image is None:
        return blocks
    out = []
    for b in blocks:
        n = b.text.count(CHECKBOX_OFF)
        if n and b.fields is None and b.rows is None and b.bbox is not None and filled_dark_boxes(image, b.bbox) >= n:
            b = dataclasses.replace(b, text=b.text.replace(CHECKBOX_OFF, MASK))
        out.append(b)
    return out


def replace_redacted(blocks: list[LayoutBlock]) -> list[LayoutBlock]:
    """Suryas ``[REDACTED]`` heter ``[MASKAD]`` i arkivet (även i tabellceller).

    Skiftläge och blanksteg innanför hakarna spelar ingen roll (``[Redacted]``,
    ``[ redacted ]``): piloten fann ``[Redacted]`` på en blankett.
    """

    def mask(text: str) -> str:
        return str(REDACTED_RE.sub(MASK, text))

    out = []
    for b in blocks:
        rows = b.rows
        if rows is not None:
            rows = tuple(tuple(dataclasses.replace(c, text=mask(c.text)) for c in row) for row in rows)
        out.append(dataclasses.replace(b, text=mask(b.text), rows=rows))
    return out


# --- Text ----------------------------------------------------------------------------------

# "sprit- eller narkotika": ett bindestreck före och/eller m.fl. är ingen avstavning
_HYPHEN_RE = re.compile(r"(?<=\w)-\n(?=[a-zåäöéü])(?!(?:och|eller|som|till|samt)\b)")


def dehyphenate(text: str) -> str:
    """Slå ihop avstavning över radbrott när nästa rad börjar med liten bokstav."""
    return _HYPHEN_RE.sub("", text)


_TURN_RE = re.compile(r"^(Fråga|Svar|[A-ZÅÄÖ](?:[A-ZÅÄÖ .\-]{0,28}[A-ZÅÄÖ])?):\s*(.*)$")


def split_turns(text: str, *, allow_masked: bool = True) -> list[tuple[str | None, str]]:
    """Dela text i repliker ``(talarmarkör, repliktext)``.

    Markören är en versalförkortning (``F``, ``M``, ``FL``), ``Fråga``/``Svar``
    eller ett namn i versaler (högst 30 tecken) följt av kolon. ``[MASKAD]``
    räknas som (maskerad) talare bara först i texten och bara när resten börjar
    med versal (``[MASKAD] Ja.``, inte ``[MASKAD] har den 24 oktober``), och bara
    med ``allow_masked``. Rader utan markör hör till repliken före; text före
    första markören får markören None.
    """
    turns: list[tuple[str | None, list[str]]] = []
    for i, line in enumerate(text.split("\n")):
        stripped = line.strip()
        m = _TURN_RE.match(stripped)
        after_mask = stripped[len(MASK) :].lstrip(" :") if stripped.startswith(MASK) else ""
        if m:
            turns.append((m.group(1), [m.group(2)]))
        elif allow_masked and i == 0 and after_mask[:1].isupper():
            turns.append((MASK, [after_mask]))
        elif turns:
            turns[-1][1].append(stripped)
        else:
            turns.append((None, [stripped]))
    return [(who, "\n".join(x for x in parts if x)) for who, parts in turns]


HEADER_LABELS = (
    "Protokoll fört vid förhör med",
    "Protokoll över förhör hållet med",
    "Protokoll över förhör med",
    "Förhöret hållet",
    "Förhörsledare",
    "Förhörsvittne",
    "Förhörsperson",
    "Förhörsvakt",
    "Tolk",
)


def interrogation_header_fields(text: str) -> list[FormField]:
    """Förhörets metadatarader som fält. Text som inte börjar med en känd
    metadataetikett ger en tom lista; rader utan etikett fortsätter fältet före."""
    entries: list[tuple[str, list[str]]] = []
    for line in text.split("\n"):
        stripped = line.strip()
        label = next(
            (lbl for lbl in HEADER_LABELS if re.match(re.escape(lbl) + r"(?:\s*:|\s|$)", stripped)),
            None,
        )
        if label is not None:
            entries.append((label, [stripped[len(label) :].lstrip(" :")]))
        elif entries:
            entries[-1][1].append(stripped)
        else:
            return []
    return [FormField(None, lbl, " ".join(p for p in parts if p), None) for lbl, parts in entries]


def _header_blocks(blocks: list[LayoutBlock]) -> list[LayoutBlock]:
    """Stycken med enbart förhörsmetadata blir fältlistor (intilliggande slås ihop)."""
    out: list[LayoutBlock] = []
    for b in blocks:
        fields = (
            interrogation_header_fields(b.text)
            if b.kind is BlockKind.PARAGRAPH and b.fields is None
            else []
        )
        if not fields or not all(f.value for f in fields):
            out.append(b)
            continue
        prev = out[-1] if out else None
        if prev is not None and prev.fields and prev.fields[0].number is None:
            merged = prev.fields + tuple(fields)
            bbox = prev.bbox
            if bbox is not None and b.bbox is not None:
                bbox = (min(bbox[0], b.bbox[0]), min(bbox[1], b.bbox[1]), max(bbox[2], b.bbox[2]), max(bbox[3], b.bbox[3]))
            parts = (prev.parts or (prev.bbox,) * len(prev.fields)) + (b.bbox,) * len(fields)
            out[-1] = dataclasses.replace(prev, text=fields_text(merged), bbox=bbox, fields=merged, parts=parts)
        else:
            out.append(
                LayoutBlock(
                    BlockKind.FORM, fields_text(fields), 0, b.bbox, fields=tuple(fields), parts=(b.bbox,) * len(fields)
                )
            )
    return out


def _dehyphenate_block(b: LayoutBlock) -> LayoutBlock:
    """Avstavning i blocktext och tabellceller (där radbrottet redan blivit mellanslag)."""
    if b.kind is BlockKind.CODE:
        return b
    rows = b.rows
    if rows is not None:
        rows = tuple(
            tuple(dataclasses.replace(c, text=_join_inline_hyphens(c.text)) for c in row) for row in rows
        )
        return dataclasses.replace(b, text="\n".join("\t".join(c.text for c in row) for row in rows), rows=rows)
    return dataclasses.replace(b, text=dehyphenate(b.text))


def postprocess(
    blocks: list[LayoutBlock],
    image: Any = None,
    page_height: int | None = None,
    trace: dict[str, Any] | None = None,
) -> list[LayoutBlock]:
    """Maskeringar, avstavning, blankettfält, förhörsmetadata och kryssrutemasker.

    ``trace`` fylls med besluten utan dokumenttext (se ``extract_form_fields``)
    plus ``masks_resolved``: antal kryssrutor som blev ``[MASKAD]``.
    """
    blocks = replace_redacted(blocks)
    blocks = [_dehyphenate_block(b) for b in blocks]
    blocks, _ = extract_form_fields(blocks, load_templates(), page_height=page_height, trace=trace)
    blocks = _header_blocks(blocks)
    boxes_before = sum(b.text.count(CHECKBOX_OFF) for b in blocks)
    blocks = resolve_checkbox_masks(blocks, image)
    if trace is not None:
        trace["masks_resolved"] = boxes_before - sum(b.text.count(CHECKBOX_OFF) for b in blocks)
    return [dataclasses.replace(b, order=i) for i, b in enumerate(blocks)]
