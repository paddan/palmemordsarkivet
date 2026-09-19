#!/usr/bin/env python3
"""MCP-server för Palmemordsarkivet.

Exponerar tre verktyg som Claude kan anropa autonomt:
  - search_archive  — vektor- eller hybridsökning, valfri reranking
  - get_page        — hämta råtext från en specifik sida
  - web_search      — söka utanför arkivet (OpenRouters web-plugin)

Körs som subprocess av ask.py (--mcp) och Utredning.py (utredningsläge).
Startas normalt inte manuellt.
"""

from __future__ import annotations

import os
import sys
from contextlib import suppress
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

try:
    from errors_log import log_error  # noqa: E402

except ImportError:  # pragma: no cover
    def log_error(component: str, item: str, message: str) -> None:
        pass

from mcp.server.fastmcp import FastMCP  # noqa: E402 — kräver src på sys.path ovan

from llm_usage import provider_cost  # noqa: E402 — samma kostnadsregel som räknaren

mcp = FastMCP(
    "palmemordsarkivet",
    instructions=(
        "Sök i Palmemordsarkivet med search_archive. "
        "Använd get_page för att läsa mer kontext kring en specifik sida. "
        "Använd web_search bara när arkivet inte kan avgöra något som går att slå "
        "upp, och märk sådana uppgifter som [webbkälla: domän, titel](url). "
        "Citera arkivmaterial med [Nr X, sida Y]."
    ),
)

# ── Lazy-initierade globaler ──────────────────────────────────────────────────
_table = None
_model = None
_text_dir: Path = ROOT / "generated" / "text"

TOP_K_MIN = 5
TOP_K_MAX = 50
# 50 och inte 20: mätningen i docs/jev-reranker-pilot.md visade att topp 6 i
# MCP:s konfiguration (hybridsökning + BGE) rymde 37 av 60 belägg med 20
# kandidater och 47 av 60 med 50.
TOP_K_DEFAULT = 50
TOP_N_MIN = 1
TOP_N_MAX = 15
TOP_N_DEFAULT = 6

# ── Webbsökning ───────────────────────────────────────────────────────────────
# OpenRouters web-plugin gör själva sökningen och bifogar träffarna som
# url_citation-annoteringar (url, titel, utdrag). Modellens svar används inte —
# bara annoteringarna — så en billig modell och max_tokens=1 räcker. Det gör
# verktyget till ett sökverktyg i stället för en mellanhand som sammanfattar:
# arkivsvaret ska bygga på källutdrag.
#
# Priset är OpenRouters sökavgift per anrop (~$0,007 med Exa auto, upp till 10
# träffar) plus en mycket liten inferenskostnad, och betalas bara när modellen
# faktiskt anropar verktyget. Kostnaden kommer tillbaka till anroparen som
# ``usage.cost``; Utredning-sidan bokför den i tokenräknaren.
WEB_SEARCH_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
WEB_SEARCH_MODEL = "openai/gpt-4o-mini"
WEB_SEARCH_ENGINE = "exa"
# wpu.nu är en spegling av arkivet självt. Träffar därifrån är ingen självständig
# källa, och de tränger ut riktiga nätkällor i resultatlistan. Domen utesluts både
# i sökningen och lokalt, så garantin inte hänger på vilken motor OpenRouter
# väljer (OpenAI-motorn ignorerar t.ex. exclude_domains).
WEB_SEARCH_EXCLUDE_DOMAINS: tuple[str, ...] = ("wpu.nu", "*.wpu.nu")
WEB_SEARCH_KEY = "OPENROUTER_API_KEY"
# Nyckellös körning anropar verktyget i varje tur; raden ska finnas i errors.log
# men bara en gång per process, annars dränks loggen av samma fel.
_nyckel_loggad = False
# Markören som ocr_pages.py infogar vid detekterade maskeringsblock.
MASKAD_MARKER = "[MASKAD]"
MAX_RESULTS_MIN = 3
MAX_RESULTS_MAX = 10
MAX_RESULTS_DEFAULT = 5
# Utdraget är sidans egen text; arkivsvaret behöver igenkänning, inte hela sidan.
WEB_EXCERPT_CHARS = 700

# Operatörens sökval (Utredning-sidans sökinställningar) kommer hit via miljön
# när servern startas som subprocess. Körs servern fristående — från Claude
# Desktop eller `scripts/ask.py --mcp` — finns de inte, och då äger modellen
# sina egna argument precis som förut.
ENV_RERANKER = "MCP_RERANKER"
ENV_TOP_K = "MCP_TOP_K"
ENV_TOP_N = "MCP_TOP_N"
RERANK_MODES = ("bge", "jev", "none")


def _init():
    global _table, _model
    if _table is not None:
        return
    import lancedb
    from sentence_transformers import SentenceTransformer

    db_dir = Path(os.environ.get("DB_DIR", str(ROOT / "generated" / "lancedb")))
    model_name = os.environ.get("EMBED_MODEL", "intfloat/multilingual-e5-large")
    db = lancedb.connect(str(db_dir))
    _table = db.open_table("chunks")
    _model = SentenceTransformer(model_name)


def _int_or_default(value: object, default: int) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def clamp_result_limits(top_k: object, top_n: object) -> tuple[int, int]:
    """Normalisera sökgränser till verktygets tillåtna intervall."""
    k = _int_or_default(top_k, TOP_K_DEFAULT)
    n = _int_or_default(top_n, TOP_N_DEFAULT)
    return (
        min(max(k, TOP_K_MIN), TOP_K_MAX),
        min(max(n, TOP_N_MIN), TOP_N_MAX),
    )


def resolve_search_policy(top_k: object, top_n: object, rerank: bool) -> tuple[str, int, int]:
    """Rerankerläge, top-K och top-N för ett sökande.

    Operatörens val i miljön går före modellens argument för de tre: reranker-
    valet är en driftfråga (kostnad, nätverk, API-nyckel), inte en bedömningsfråga,
    och modellen kan inte veta att Jev debiterar kredit per anrop. Saknas
    miljövariablerna gäller modellens argument och verktygets standardvärden.
    """
    mode = os.environ.get(ENV_RERANKER, "").strip().lower()
    if not mode:
        return ("bge" if rerank else "none", *clamp_result_limits(top_k, top_n))
    if mode not in RERANK_MODES:
        raise RuntimeError(
            f"Okänt {ENV_RERANKER}: {mode!r} (väntade {', '.join(RERANK_MODES)})"
        )
    return (
        mode,
        *clamp_result_limits(
            os.environ.get(ENV_TOP_K, TOP_K_DEFAULT),
            os.environ.get(ENV_TOP_N, TOP_N_DEFAULT),
        ),
    )


def clamp_max_results(value: object) -> int:
    """Normalisera antalet webbträffar till verktygets tillåtna intervall."""
    n = _int_or_default(value, MAX_RESULTS_DEFAULT)
    return min(max(n, MAX_RESULTS_MIN), MAX_RESULTS_MAX)


def excluded_domain(url: str) -> bool:
    """Om adressen hör till en undantagen domän (wpu.nu och dess subdomäner).

    Jämförelsen kräver punktgräns: ``endswith("wpu.nu")`` hade också fällt
    ``notwpu.nu``, alltså en helt annan sajt.
    """
    host = urlsplit(url).netloc.lower().removeprefix("www.")
    return any(
        host == domän or host.endswith("." + domän.removeprefix("*."))
        for domän in WEB_SEARCH_EXCLUDE_DOMAINS
    )


def web_hits(payload: object) -> list[dict[str, str]]:
    """Plocka ut url, titel och utdrag ur OpenRouters annoteringar.

    Träffar från undantagna domäner filtreras bort här också: sökningens
    ``exclude_domains`` är motorns ansvar, och en motor som ignorerar filtret får
    inte läcka in arkivets egen spegling i svaret.
    """
    if not isinstance(payload, dict):
        return []
    try:
        message = payload["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return []
    if not isinstance(message, dict):
        return []
    träffar: list[dict[str, str]] = []
    for ann in message.get("annotations") or []:
        citat = (ann or {}).get("url_citation") or {}
        url = str(citat.get("url") or "")
        if not url or excluded_domain(url):
            continue
        träffar.append(
            {
                "url": url,
                "title": str(citat.get("title") or urlsplit(url).netloc),
                "excerpt": str(citat.get("content") or ""),
            }
        )
    return träffar


def format_web_hits(query: str, hits: list[dict[str, str]]) -> str:
    """Raderna som går till modellen — aldrig i arkivets [Nr X, sida Y]-format.

    Varje träff skrivs som den färdiga citatsträngen, så modellen kan kopiera den
    i stället för att hitta på en egen form: källan (och därmed webbsidan) ska
    synas i svaret, inte bara i verktygets resultat.
    """
    if not hits:
        return f"Webbsökning: {query!r} gav inga webbträffar."
    rader = [
        f"Webbsökning: {query!r} → {len(hits)} träffar. Källor utanför arkivet "
        "(webbmaterial, inte arkivmaterial).",
        "",
    ]
    for träff in hits:
        domän = urlsplit(träff["url"]).netloc or träff["url"]
        rader.append(f"[webbkälla: {domän}, {träff['title']}]({träff['url']})")
        utdrag = träff["excerpt"].strip()
        if utdrag:
            if len(utdrag) > WEB_EXCERPT_CHARS:
                utdrag = utdrag[:WEB_EXCERPT_CHARS] + " […]"
            rader.append(utdrag)
        rader.append("")
    return "\n".join(rader).strip()


def web_search_metrics(query: str, hits: list[dict[str, str]], cost: float | None) -> None:
    """Webbsökningens mätvärden till stderr.

    Körs verktyget i Claude-vägens subprocess finns ingen kanal tillbaka, så
    stderr är den plats där kostnaden går att hitta i efterhand (samma skäl som
    för Jev). Frågan loggas, aldrig arkivinnehåll.
    """
    kostnad = f"{cost:.6f} USD" if cost is not None else "kostnad okänd"
    print(f"web_search: {len(hits)} träffar, {kostnad}, query={query!r}", file=sys.stderr)


def _mask_hint(text: str) -> str:
    """Deterministisk knuff när ett verktygssvar innehåller en maskering.

    Promptregler om maskeringar tappas bort mitt i en lång verktygsloop — modellen
    svarade att CHP betydde "Centrala högskoleförbundet" om en vapenannons i
    stället för att söka. Raden ligger därför i verktygssvaret, där masken
    faktiskt läses, och säger både vad som är förbjudet (rekonstruera masken) och
    vad som ska göras i stället (slå upp sammanhanget).
    """
    if MASKAD_MARKER not in text:
        return ""
    return (
        f"\n\nObs: texten innehåller {MASKAD_MARKER} (maskerat parti). Innehållet "
        "bakom maskeringen får varken gissas eller rekonstrueras — men maskeringen är "
        "ett skäl att söka: kontrollera rollen eller företeelsen runt den (vilka som "
        "varit ordförande i klubben, var firman låg, vad förkortningen står för) med "
        "web_search, och redovisa det som en nätuppgift. Slå också upp en "
        "identifierbar företeelse utanför arkivet innan du svarar att något inte "
        "framgår. Osäkerhet är alltid ett skäl att söka: ger materialet inget "
        "entydigt svar, sök innan du svarar. Finns inte web_search bland verktygen, "
        "säg att uppgiften inte framgår."
    )


def _log_jev_metrics(metrics: dict) -> None:
    """Jevs mätvärden till stderr.
    Verktygssvaret går till modellen, och en kostnadsrad där hade riskerat att
    hamna i citeringen. Utredning-sidan bokför inte heller MCP-anrop i
    tokenräknaren, så stderr är den plats där de går att hitta i efterhand.
    """
    delar = [f"Jev {metrics.get('model')}"]
    if metrics.get("cost_usd") is not None:
        delar.append(f"{float(metrics['cost_usd']):.6f} USD")
    if metrics.get("input_tokens") is not None:
        delar.append(f"{metrics['input_tokens']} indatatoken")
    print("rerank: " + ", ".join(delar), file=sys.stderr)


def search_with_settings(
    query: str, *, hybrid: bool, reranker: str, top_k: int, top_n: int
) -> str:
    """Kärnan i :func:`search_archive` med givna sökval.

    Anropas dels av verktyget självt (efter :func:`resolve_search_policy`), dels
    in-process av Utredning-sidans OpenAI-väg, som skickar in sidofältets val
    direkt i stället för via miljön.
    """
    _init()
    from ask import format_context, search, search_hybrid

    hits = search_hybrid(_table, _model, query, top_k) if hybrid else search(
        _table, _model, query, top_k
    )
    if not hits:
        return "Inga träffar."

    if reranker == "none":
        hits = hits[:top_n]
    elif reranker == "jev":
        from ask import rerank_jev

        hits, metrics = rerank_jev(query, hits, top_n)
        _log_jev_metrics(metrics)
    else:
        from ask import rerank as do_rerank

        hits = do_rerank(query, hits, top_n)

    header = f"Sökning: {query!r} → {len(hits)} träffar\n\n"
    context: str = format_context(hits, include_source=True)
    return header + context + _mask_hint(context)


# ── Verktyg ───────────────────────────────────────────────────────────────────

@mcp.tool()
def search_archive(
    query: Annotated[str, "Sökfrågan på svenska"],
    top_k: Annotated[int, "Antal kandidater att hämta (5–50)"] = TOP_K_DEFAULT,
    top_n: Annotated[int, "Antal att behålla efter reranking (1–15)"] = TOP_N_DEFAULT,
    hybrid: Annotated[bool, "Kombinera vektor- och BM25-sökning"] = True,
    rerank: Annotated[bool, "Omranka med cross-encoder för bättre precision"] = True,
) -> str:
    """Sök i Palmemordsarkivet och returnera relevanta textutdrag med källhänvisningar.

    Använd detta verktyg för att hitta information i Palmemordsarkivet.
    Anropa flera gånger med olika söktermer för att täcka ett ämne från flera vinklar.
    """
    reranker, k, n = resolve_search_policy(top_k, top_n, rerank)
    return search_with_settings(query, hybrid=hybrid, reranker=reranker, top_k=k, top_n=n)


@mcp.tool()
def get_page(
    source: Annotated[str, "Filnamn från söktträff, t.ex. '281 — Titel….txt'"],
    page: Annotated[int, "Sidnummer (1-baserat)"],
) -> str:
    """Hämta råtexten från en specifik sida i ett arkivdokument.

    Använd detta för att läsa mer kontext kring en träff från search_archive —
    t.ex. sidorna precis före/efter ett intressant stycke.
    """
    stem = source[:-4] if source.endswith(".txt") else source
    txt = (_text_dir / f"{stem}.txt").resolve()
    if not txt.is_relative_to(_text_dir.resolve()):
        return "Ogiltig filsökväg."
    if not txt.exists():
        return f"Hittade inte {source} i text/-katalogen."

    try:
        full = txt.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"Kunde inte läsa {source}: {e}"

    pages = full.split("\f") if "\f" in full else [full]
    if page < 1 or page > len(pages):
        return f"Sidan {page} finns inte i {source} (har {len(pages)} sidor)."

    text = pages[page - 1].strip()
    if not text:
        return f"Sidan {page} i {source} är tom."
    return f"[{source}, sida {page}]\n\n{text}" + _mask_hint(text)


def search_web(query: str, max_results: object = MAX_RESULTS_DEFAULT) -> tuple[str, float | None]:
    """Sök på nätet via OpenRouters web-plugin.

    Returnerar (text till modellen, anropets kostnad i USD). Kostnaden är
    OpenRouters rapporterade totalsumma för anropet — sökavgiften syns inte i
    något tokenfält, så den måste komma härifrån för att kunna bokföras.

    Nätverks-, HTTP- och tolkningsfel redovisas som text till modellen och
    loggas; de får aldrig bli en tyst tom sökning som ser ut som ett nej.
    """
    key = os.environ.get(WEB_SEARCH_KEY, "").strip()
    if not key:
        # Loggas: annars går en påslagen men nyckellös webbsökning inte att
        # skilja från ett verktyg modellen aldrig valde. En gång räcker.
        global _nyckel_loggad
        if not _nyckel_loggad:
            _nyckel_loggad = True
            log_error("web_search", query, f"{WEB_SEARCH_KEY} saknas i miljön")
        return (
            f"Webbsökning är inte tillgänglig: {WEB_SEARCH_KEY} saknas i miljön.",
            None,
        )
    body = {
        "model": WEB_SEARCH_MODEL,
        # Modellens svar används inte (bara annoteringarna), så ett token räcker.
        "max_tokens": 1,
        "messages": [{"role": "user", "content": query}],
        "plugins": [
            {
                "id": "web",
                "engine": WEB_SEARCH_ENGINE,
                "max_results": clamp_max_results(max_results),
                "exclude_domains": list(WEB_SEARCH_EXCLUDE_DOMAINS),
            }
        ],
    }
    try:
        svar = requests.post(
            WEB_SEARCH_ENDPOINT,
            headers={"Authorization": f"Bearer {key}"},
            json=body,
            timeout=60,
        )
    except requests.RequestException as exc:
        log_error("web_search", query, f"nätverksfel: {type(exc).__name__}")
        return (f"Webbsökningen misslyckades ({type(exc).__name__}).", None)
    if svar.status_code != 200:
        log_error("web_search", query, f"HTTP {svar.status_code}")
        return (f"Webbsökningen misslyckades: HTTP {svar.status_code}.", None)
    try:
        payload = svar.json()
    except ValueError:
        log_error("web_search", query, "ogiltigt JSON-svar")
        return ("Webbsökningen misslyckades: ogiltigt svar från OpenRouter.", None)

    träffar = web_hits(payload)
    kostnad = provider_cost(payload.get("usage") if isinstance(payload, dict) else None)
    web_search_metrics(query, träffar, kostnad)
    return (format_web_hits(query, träffar), kostnad)


@mcp.tool()
def web_search(
    query: Annotated[str, "Sökfrågan på svenska"],
    max_results: Annotated[int, "Antal webbsidor att hämta (3–10)"] = MAX_RESULTS_DEFAULT,
) -> str:
    """Sök på nätet efter information utanför Palmemordsarkivet.

    Använd när materialet innehåller något du inte kan tolka ur arkivet: en
    förkortning eller ett begrepp du inte känner igen, ett namn eller en plats
    som är tvetydig eller ser OCR-skadad ut, en samtida företeelse (ett företag,
    ett vapenmärke, en tidningsannons, en adress) eller [MASKAD]-markeringar vars
    sammanhang är otydligt. Gissa aldrig i stället. Använd det också för att
    kontrollera sådant som går att belägga utanför arkivet — ett företag, en
    adress, ett vapenmärke, en tidningsannons — även när arkivet svarar. Osäkerhet
    är alltid ett skäl att söka: tvekar du, eller ger materialet inget entydigt
    svar, sök innan du svarar. Gäller frågan nuläget — vad som gäller i dag eller
    numera — sök alltid, och datera webbuppgiften.

    Träffarna kommer från öppna webbsidor och är inte arkivmaterial: redovisa dem
    i svaret som [webbkälla: domän, titel](url), aldrig som [Nr X, sida Y], och
    rekonstruera aldrig innehållet bakom en maskering från nätet.
    """
    text, _kostnad = search_web(query, max_results)
    return text


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Stdio-server: avsluta tyst när föräldraprocessen stänger ned.
    with suppress(KeyboardInterrupt):
        mcp.run()
