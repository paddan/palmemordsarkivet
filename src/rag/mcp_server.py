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

from backends import provider_from_endpoint  # noqa: E402 — delas med adminformuläret
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
# Verktyget är leverantörsoberoende utåt: alla vägar svarar med samma
# url_citation-form (url, titel, utdrag), så format_web_hits, wpu.nu-undantaget
# och kostnadsregeln delas. Det finns ingen gemensam sök-API att luta sig mot —
# varje leverantör har sin mekanism — så valet av leverantör är en inställning:
#
#   openrouter  chat/completions med plugins=[{id: web}] (Exa-motorn)
#   openai      Responses-API:et med verktyget web_search
#   anthropic   Messages-API:et med serververktyget web_search
#
# Modellens svar används aldrig — bara sökträffarna — så en billig modell och
# hårt begränsad utdata räcker. Det gör verktyget till ett sökverktyg i stället
# för en mellanhand som sammanfattar: arkivsvaret ska bygga på källutdrag.
#
# Kostnaden rapporteras bara av OpenRouter (usage.cost). OpenAI och Anthropic
# fakturerar sökningen separat och rapporterar den inte i svaret, så där blir
# kostnaden okänd i räknaren i stället för en påhittad siffra.
WEB_SEARCH_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
WEB_SEARCH_ENGINE = "exa"
WEB_SEARCH_OPENAI_ENDPOINT = "https://api.openai.com/v1/responses"
WEB_SEARCH_ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1/messages"
WEB_SEARCH_ANTHROPIC_VERSION = "2023-06-01"
# Modellen som gör sök-anropet per leverantör, när ingen LLM-profil är vald
# (fristående server eller miljöstyrd körning). I sidan kommer modellen från den
# valda profilen i stället; ENV_WEB_SEARCH_MODEL sätts per fråga och går före.
WEB_SEARCH_DEFAULT_MODELS: dict[str, str] = {
    # Verifierad live i vårt flöde (max_tokens=1 + Exa-annoteringar): nästan hela
    # sök-kostnaden är Exas avgift, så modellen ska bara vara billig och snabb.
    # Mätt: $0,0072 per sökning mot $0,0074 för gpt-4o-mini.
    "openrouter": "openai/gpt-4.1-nano",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-4-5-20251001",
}
ENV_WEB_SEARCH_MODEL = "MCP_WEB_SEARCH_MODEL"
# Sökmodellen är en konfigurerad LLM-profil: operatören väljer bland sina egna, och
# profilens leverantör avgör sök-API:et. Namnet går hit via miljön i Claude-vägens
# subprocess och sätts in-process av Utredning-sidan; filen web_search.json håller
# förstahandsvalet.
ENV_WEB_SEARCH_PROFILE = "MCP_WEB_SEARCH_PROFILE"
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


# wpu.nu är en spegling av arkivet självt. Träffar därifrån är ingen självständig
# källa, och de tränger ut riktiga nätkällor i resultatlistan. Domen utesluts både
# i sökningen och lokalt, så garantin inte hänger på vilken motor som används.
WEB_SEARCH_EXCLUDE_DOMAINS: tuple[str, ...] = ("wpu.nu", "*.wpu.nu")
# Leverantörer i den ordning auto väljer bland när LLM-profilen inte pekar ut en.
WEB_SEARCH_PROVIDERS: tuple[str, ...] = ("openrouter", "openai", "anthropic")
WEB_SEARCH_PROVIDER_ENV = "MCP_WEB_SEARCH_PROVIDER"
# Vald LLM-profil (None = läs ur miljön/generated standard; tom = ingen vald).
_web_search_profile: str | None = None
# Vald leverantör (None = läs ur miljön/profilen).
_web_search_provider: str | None = None
# Taket för antal webbsökningar per fråga (operatörens ratt i sökinställningarna).
# OpenAI-vägen tar bort verktyget ur listan när taket är nått; Claude-vägen får
# taket via miljön och servern svarar att budgeten är slut.
ENV_WEB_SEARCH_BUDGET = "MCP_WEB_SEARCH_BUDGET"
WEB_SEARCH_BUDGET_MIN = 1
WEB_SEARCH_BUDGET_MAX = 20
WEB_SEARCH_BUDGET_DEFAULT = 3
# ponytail: räknaren är processvid, så två samtidiga Streamlit-sessioner delar
# budget. Det är en kostnadsbroms, inte en behörighet — blir appen fleranvänd
# flyttas räknaren till sessionsstate i stället.
_web_search_budget: int | None = None
_web_searches_used = 0
# Nyckellös körning anropar verktyget i varje tur; raden ska finnas i errors.log
# men bara en gång per process, annars dränks loggen av samma fel.
_nyckel_loggad = False
WEB_SEARCH_PROVIDER_DEFAULT = "auto"
PROVIDER_KEYS: dict[str, str] = {
    "openrouter": "OPENROUTER_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}
def search_profiles(profiles: object, catalog: object = None) -> list[dict[str, str]]:
    """Operatörens konfigurerade LLM-profiler som kan söka — i profilordning.

    Listan i gränssnittet ska vara operatörens egna modeller, inte en fast lista
    över API:er, och bara de som har en sök-API (DeepSeek, Ollama och
    custom-endpoints faller bort). Profilens endpoint, nyckel och modell används
    rakt av när den väljs.
    """
    if not isinstance(profiles, dict):
        return []
    valbara: list[dict[str, str]] = []
    for namn, profil in profiles.items():
        if not isinstance(profil, dict):
            continue
        löst = _resolve_profile(profil, catalog)
        if not löst:
            continue
        provider = provider_from_endpoint(löst.get("base_url"), löst.get("kind"))
        if not provider:
            continue
        valbara.append(
            {
                "name": str(namn),
                "provider": provider,
                "model": str(löst.get("model") or ""),
                "key_env": str(löst.get("api_key_env") or PROVIDER_KEYS[provider]),
            }
        )
    return valbara


def _resolve_profile(profil: dict, catalog: object = None) -> dict | None:
    """Slå ihop en sparad LLM-profil med backend-katalogen (via config.py)."""
    try:
        from backends import BACKENDS
        from config import resolve_runtime_profile

        katalog = catalog if isinstance(catalog, dict) and catalog else BACKENDS
        return dict(resolve_runtime_profile(profil, katalog))
    except Exception as exc:  # okänd backend, trasig profil m.m.
        log_error("web_search", str(profil.get("backend_name") or "?"),
                  f"kunde inte lösa profilen: {type(exc).__name__}")
        return None


def web_search_model(provider: str | None = None) -> str:
    """Modellen som gör själva sök-anropet.

    Sökresultaten kommer från sökmotorn, inte från modellen, så en billig modell
    räcker — och operatören får sätta den själv: miljön (satt per fråga av
    Utredning-sidan) går före genererad konfiguration, som går före standarden.
    """
    namn = provider or web_search_provider()
    # En vald LLM-profil bestämmer både leverantör och modell: den väljs bland
    # operatörens egna konfigurationer, och dess modell gör sök-anropet.
    mål = _profile_target()
    if mål and mål["provider"] == namn and mål["model"]:
        return mål["model"]
    # Annars gäller miljövariabeln (satt per fråga); anropas funktionen utan
    # leverantör är det just den man frågar om.
    if provider is None or provider == web_search_provider():
        från_miljö = os.environ.get(ENV_WEB_SEARCH_MODEL, "").strip()
        if från_miljö:
            return från_miljö
    return WEB_SEARCH_DEFAULT_MODELS.get(namn, WEB_SEARCH_DEFAULT_MODELS["openrouter"])


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


def web_search_metrics(
    query: str, hits: list[dict[str, str]], cost: float | None, provider: str = ""
) -> None:
    """Webbsökningens mätvärden till stderr.

    Körs verktyget i Claude-vägens subprocess finns ingen kanal tillbaka, så
    stderr är den plats där kostnaden går att hitta i efterhand (samma skäl som
    för Jev). Frågan loggas, aldrig arkivinnehåll.
    """
    kostnad = f"{cost:.6f} USD" if cost is not None else "kostnad okänd"
    print(
        f"web_search [{provider or web_search_provider()}]: {len(hits)} träffar, "
        f"{kostnad}, query={query!r}",
        file=sys.stderr,
    )


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


def clamp_web_search_budget(value: object) -> int:
    """Normalisera webbsöksbudgeten till 1–20 (otolkbart värde → standard)."""
    n = _int_or_default(value, WEB_SEARCH_BUDGET_DEFAULT)
    return min(max(n, WEB_SEARCH_BUDGET_MIN), WEB_SEARCH_BUDGET_MAX)


def set_web_search_budget(budget: object | None = None) -> int:
    """Sätt frågans budget och nollställ räknaren. Returnerar det gällande taket.

    Anropas av Utredning-sidan när en fråga börjar. ``None`` betyder "läs ur
    miljön", vilket är hur Claude-vägens subprocess får sitt tak.
    """
    global _web_search_budget, _web_searches_used
    källa = budget if budget is not None else os.environ.get(ENV_WEB_SEARCH_BUDGET)
    _web_search_budget = clamp_web_search_budget(källa)
    _web_searches_used = 0
    return _web_search_budget


def web_search_budget() -> int:
    """Gällande tak för frågan."""
    if _web_search_budget is None:
        return set_web_search_budget()
    return _web_search_budget


def web_search_budget_left() -> int:
    """Antal sökningar kvar inom budgeten."""
    return max(web_search_budget() - _web_searches_used, 0)


def _take_web_search_slot() -> bool:
    """Reservera en sökning om budgeten räcker, annars False."""
    global _web_searches_used
    if _web_searches_used >= web_search_budget():
        return False
    _web_searches_used += 1
    return True


def provider_available(provider: str) -> bool:
    """Om leverantörens nyckel finns i miljön."""
    return bool(os.environ.get(PROVIDER_KEYS.get(provider, ""), "").strip())


def resolve_web_search_provider(
    choice: object = None, preferred: str | None = None
) -> str:
    """Vilken leverantör som faktiskt används.

    ``auto`` följer den modell frågan körs på (då behövs ingen extra nyckel),
    annars första leverantör med nyckel. Ett uttryckligt val som saknar nyckel
    faller tillbaka på samma regel i stället för att bli ett tyst nej — anropet
    svarar ändå med vilken nyckel som fattas.
    """
    val = str(choice or "").strip().lower()
    if val in WEB_SEARCH_PROVIDERS and provider_available(val):
        return val
    if preferred in WEB_SEARCH_PROVIDERS and provider_available(preferred):
        return preferred
    for namn in WEB_SEARCH_PROVIDERS:
        if provider_available(namn):
            return namn
    # Ingen nyckel alls: namnge standardleverantörens nyckel i felet.
    return val if val in WEB_SEARCH_PROVIDERS else "openrouter"


def set_web_search_profile(name: object = None) -> str:
    """Sätt LLM-profilen som gör sökningen (None/tom = läs ur miljö/standard)."""
    global _web_search_profile
    val = str(name or "").strip()
    _web_search_profile = val or None
    return web_search_profile()


def web_search_profile() -> str:
    """Gällande profilnamn; tom sträng betyder auto."""
    if _web_search_profile is not None:
        return _web_search_profile
    från_miljö = os.environ.get(ENV_WEB_SEARCH_PROFILE, "").strip()
    if från_miljö:
        return från_miljö
    try:
        from config import load_search_default

        return str(load_search_default())
    except Exception as exc:  # pragma: no cover
        log_error("web_search", "?", f"kunde inte läsa standardvalet: {type(exc).__name__}")
        return ""


def _profile_target() -> dict[str, str] | None:
    """Leverantör, modell, nyckel och nyckelnamn för vald LLM-profil."""
    namn = web_search_profile()
    if not namn:
        return None
    try:
        from config import load_all

        profiler = load_all().get("profiles") or {}
    except Exception as exc:  # pragma: no cover
        log_error("web_search", namn, f"kunde inte läsa profilerna: {type(exc).__name__}")
        return None
    profil = profiler.get(namn)
    if not isinstance(profil, dict):
        log_error("web_search", namn, "profilen finns inte i llm_config.json")
        return None
    löst = _resolve_profile(profil)
    if not löst:
        return None
    provider = provider_from_endpoint(löst.get("base_url"), löst.get("kind"))
    if not provider:
        log_error("web_search", namn, "profilens leverantör har ingen sök-API")
        return None
    return {
        "provider": provider,
        "model": str(löst.get("model") or ""),
        "key": str(löst.get("api_key") or ""),
        "key_env": str(löst.get("api_key_env") or PROVIDER_KEYS[provider]),
    }


def set_web_search_provider(provider: object = None) -> str:
    """Sätt leverantören för den här processen (None = läs ur miljö/auto)."""
    global _web_search_provider
    val = str(provider or "").strip().lower()
    _web_search_provider = val if val else None
    return web_search_provider()


def web_search_provider() -> str:
    """Gällande sökväg: vald LLM-profil går före allt annat."""
    mål = _profile_target()
    if mål:
        return mål["provider"]
    if _web_search_provider is None:
        return resolve_web_search_provider(os.environ.get(WEB_SEARCH_PROVIDER_ENV))
    return resolve_web_search_provider(_web_search_provider)


def web_search_key_env(provider: str) -> str:
    """Miljövariabeln med nyckeln för sökningen (profilens, annars standarden)."""
    mål = _profile_target()
    if mål and mål["provider"] == provider:
        return mål["key_env"]
    return PROVIDER_KEYS.get(provider, PROVIDER_KEYS["openrouter"])


def _request_json(
    url: str, headers: dict[str, str], body: dict, query: str, provider: str
) -> tuple[dict | None, str | None]:
    """POST med JSON-svar. Returnerar (payload, feltext) — ett av dem är None."""
    try:
        svar = requests.post(url, headers=headers, json=body, timeout=60)
    except requests.RequestException as exc:
        log_error("web_search", query, f"nätverksfel ({provider}): {type(exc).__name__}")
        return None, f"Webbsökningen misslyckades ({type(exc).__name__})."
    if svar.status_code != 200:
        log_error("web_search", query, f"HTTP {svar.status_code} ({provider})")
        return None, f"Webbsökningen misslyckades: HTTP {svar.status_code}."
    try:
        payload = svar.json()
    except ValueError:
        log_error("web_search", query, f"ogiltigt JSON-svar ({provider})")
        return None, f"Webbsökningen misslyckades: ogiltigt svar från {provider}."
    return (payload if isinstance(payload, dict) else None), None


def _hits_from_annotations(annoteringar: object) -> list[dict[str, str]]:
    """url_citation-annoteringar (OpenRouter chat/completions, OpenAIs Responses)."""
    träffar: list[dict[str, str]] = []
    if not isinstance(annoteringar, list):
        return träffar
    for ann in annoteringar:
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


def _search_openrouter(
    query: str, max_results: int, key: str, model: str
) -> tuple[list[dict[str, str]], float | None, str | None]:
    payload, fel = _request_json(
        WEB_SEARCH_ENDPOINT,
        {"Authorization": f"Bearer {key}"},
        {
            "model": model,
            # Modellens svar används inte (bara annoteringarna), så ett token räcker.
            "max_tokens": 1,
            "messages": [{"role": "user", "content": query}],
            "plugins": [
                {
                    "id": "web",
                    "engine": WEB_SEARCH_ENGINE,
                    "max_results": max_results,
                    "exclude_domains": list(WEB_SEARCH_EXCLUDE_DOMAINS),
                }
            ],
        },
        query,
        "openrouter",
    )
    if fel:
        return [], None, fel
    assert payload is not None
    annoteringar: object = None
    try:
        annoteringar = payload["choices"][0]["message"].get("annotations")
    except (KeyError, IndexError, TypeError, AttributeError):
        log_error("web_search", query, "kunde inte tolka svaret från OpenRouter")
    return (
        _hits_from_annotations(annoteringar),
        provider_cost(payload.get("usage")),
        None,
    )


def _search_openai(
    query: str, max_results: int, key: str, model: str
) -> tuple[list[dict[str, str]], float | None, str | None]:
    """OpenAIs Responses-API med serververktyget web_search.

    Best effort: kodvägen är skriven mot det dokumenterade svarsformatet
    (``output[*].content[*].annotations[*].url_citation``) men har inte kunnat
    köras live mot OpenAI från det här repot — verifierad med mockad klient.
    Kostnaden rapporteras inte i svaret, alltså okänd i räknaren.
    """
    payload, fel = _request_json(
        WEB_SEARCH_OPENAI_ENDPOINT,
        {"Authorization": f"Bearer {key}"},
        {
            "model": model,
            "input": query,
            "tools": [{"type": "web_search"}],
            "tool_choice": {"type": "web_search"},
            "max_output_tokens": 16,
        },
        query,
        "openai",
    )
    if fel:
        return [], None, fel
    assert payload is not None
    annoteringar: list = []
    for post in payload.get("output") or []:
        for innehåll in (post or {}).get("content") or []:
            annoteringar.extend((innehåll or {}).get("annotations") or [])
    return _hits_from_annotations(annoteringar), None, None


def _search_anthropic(
    query: str, max_results: int, key: str, model: str
) -> tuple[list[dict[str, str]], float | None, str | None]:
    """Anthropics Messages-API med serververktyget web_search.

    Best effort: skriven mot ``web_search_tool_result``-blocken
    (``content[*].url``/``title``) men inte live-verifierad härifrån. Anthropic
    returnerar inget sidutdrag, bara url och titel — utdraget uteblir därför.
    Kostnaden rapporteras inte i svaret, alltså okänd i räknaren.
    """
    payload, fel = _request_json(
        WEB_SEARCH_ANTHROPIC_ENDPOINT,
        {
            "x-api-key": key,
            "anthropic-version": WEB_SEARCH_ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
        {
            "model": model,
            "max_tokens": 16,
            "messages": [{"role": "user", "content": query}],
            "tools": [
                {
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": 1,
                }
            ],
            "tool_choice": {"type": "tool", "name": "web_search"},
        },
        query,
        "anthropic",
    )
    if fel:
        return [], None, fel
    assert payload is not None
    träffar: list[dict[str, str]] = []
    for block in payload.get("content") or []:
        if (block or {}).get("type") != "web_search_tool_result":
            continue
        for resultat in (block or {}).get("content") or []:
            url = str((resultat or {}).get("url") or "")
            if not url or excluded_domain(url):
                continue
            träffar.append(
                {
                    "url": url,
                    "title": str((resultat or {}).get("title") or urlsplit(url).netloc),
                    "excerpt": "",
                }
            )
    return träffar, None, None


WEB_SEARCH_BACKENDS = {
    "openrouter": _search_openrouter,
    "openai": _search_openai,
    "anthropic": _search_anthropic,
}


def search_web(query: str, max_results: object = MAX_RESULTS_DEFAULT) -> tuple[str, float | None]:
    """Sök på nätet via vald leverantör.

    Returnerar (text till modellen, anropets kostnad i USD). Kostnaden är
    leverantörens rapporterade totalsumma när den finns — sökavgiften syns inte i
    något tokenfält, så den måste komma härifrån för att kunna bokföras.

    Nätverks-, HTTP- och tolkningsfel redovisas som text till modellen och
    loggas; de får aldrig bli en tyst tom sökning som ser ut som ett nej.
    """
    if not _take_web_search_slot():
        return (
            f"Webbsöksbudgeten för den här frågan är slut ({web_search_budget()} "
            "sökningar). Sammanfatta med det underlag du redan har i stället för att "
            "söka igen.",
            None,
        )
    provider = web_search_provider()
    mål = _profile_target()
    nyckel = (mål["key"] if mål and mål["provider"] == provider else "").strip()
    nyckel_namn = web_search_key_env(provider)
    if not nyckel:
        nyckel = os.environ.get(nyckel_namn, "").strip()
    if not nyckel:
        # Loggas: annars går en påslagen men nyckellös webbsökning inte att
        # skilja från ett verktyg modellen aldrig valde. En gång räcker.
        global _nyckel_loggad
        if not _nyckel_loggad:
            _nyckel_loggad = True
            log_error("web_search", query, f"{nyckel_namn} saknas i miljön")
        return (
            "Webbsökning är inte tillgänglig: "
            f"{nyckel_namn} saknas i miljön (leverantör: {provider}).",
            None,
        )

    träffar, kostnad, fel = WEB_SEARCH_BACKENDS[provider](
        query, clamp_max_results(max_results), nyckel, web_search_model(provider)
    )
    if fel:
        return fel, None
    web_search_metrics(query, träffar, kostnad, provider)
    return format_web_hits(query, träffar), kostnad


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
