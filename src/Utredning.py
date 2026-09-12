"""Enkelt webgränssnitt för palmemordsarkivet via Streamlit.

Kör med:
    .venv/bin/python scripts/web.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import lancedb
import streamlit as st
from sentence_transformers import SentenceTransformer

MCP_SERVER = Path(__file__).resolve().parent / "rag" / "mcp_server.py"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backends as _backends  # noqa: E402
import casebook_ui as _casebook_ui  # noqa: E402
import citations as _citations  # noqa: E402
import config as _llm_config  # noqa: E402
import db as _state_db  # noqa: E402
import facets as _facets  # noqa: E402
import llm_usage as _llm_usage  # noqa: E402
import search_fuzzy as _search_fuzzy  # noqa: E402
from errors_log import log_error  # noqa: E402
from graph import answer_entities as _answer_entities  # noqa: E402
from graph import viz as _viz  # noqa: E402

try:
    from st_link_analysis import EdgeStyle, NodeStyle, st_link_analysis
    _HAS_LINK_ANALYSIS = True
except ImportError:  # pragma: no cover — ingår i extran .[graph]
    _HAS_LINK_ANALYSIS = False

# importera funktioner ur rag/ask.py utan att köra dess main()
sys.path.insert(0, str(Path(__file__).resolve().parent / "rag"))
from ask import (  # noqa: E402
    CLAUDE_MODEL,
    EMBED_MODEL,
    MCP_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    TABLE,
    format_context,
    rerank,
    search,
    stop_notice,
)
from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ThinkingConfigAdaptive,
    ToolUseBlock,
    query,
)

ROOT = Path(__file__).resolve().parents[1]

st.set_page_config(page_title="Palmemordsarkivet", layout="wide")


@st.cache_resource(show_spinner="Laddar embedding-modell…")
def load():
    db = lancedb.connect(str(ROOT / "generated" / "lancedb"))
    table = db.open_table(TABLE)
    embed = SentenceTransformer(EMBED_MODEL)
    return table, embed


@st.cache_resource(show_spinner=False)
def build_nr_to_pdf() -> dict[str, Path]:
    """Cachad nr → PDF-mapping (logiken bor i citations.build_nr_to_pdf)."""
    mapping: dict[str, Path] = _citations.build_nr_to_pdf(ROOT)
    return mapping


def find_pdf(source_txt: str) -> Path | None:
    """Hitta original-PDF för en chunk. Föredrar ocr/ (sökbar)."""
    stem = source_txt[:-4] if source_txt.endswith(".txt") else source_txt
    for d in ("generated/ocr", "downloaded/files", "downloaded/wpu_files"):
        p = ROOT / d / f"{stem}.pdf"
        if p.is_file():
            return p
    return None


def find_txt(source_txt: str) -> Path | None:
    """Hitta extraherad textfil för en chunk."""
    stem = source_txt[:-4] if source_txt.endswith(".txt") else source_txt
    p = ROOT / "generated" / "text" / f"{stem}.txt"
    return p if p.is_file() else None


casebook_conn = _casebook_ui.state_conn()


@st.cache_data(show_spinner=False)
def _load_facets() -> dict:
    """Entitetsfacetter ur kunskapsgrafen (doc_entities). Cachas per session."""
    facets: dict = _facets.entity_facets(_casebook_ui.state_conn())
    return facets


@st.cache_data(show_spinner=False)
def _load_facet_index() -> dict:
    """Cachat ``casefold(namn) -> {pdf_stem}`` så doc_entities inte parsas om
    per vald facett och sökning."""
    index: dict = _facets.entity_stem_index(_casebook_ui.state_conn())
    return index


@st.cache_resource(show_spinner="Bygger fuzzy-index (engångskostnad ~30 s)…")
def _load_fuzzy_corpus():
    """Hela chunk-korpusen + token-index för OCR-tolerant fuzzy-sökning.

    Tungt (~100 MB text) men byggs bara en gång och bara om användaren slår på
    fuzzy-sökningen. Vi projicerar kolumnerna i sökningen i stället för
    ``to_pandas()`` så vektorkolumnen (~660 MB) aldrig materialiseras."""
    cols = ["text", "source", "page", "chunk_idx", "nr", "titel", "anmarkning"]
    rows = table.search().select(cols).limit(table.count_rows()).to_list()
    return rows, _search_fuzzy.build_index(rows)


def _interleave_hits(primary: list[dict], extra: list[dict]) -> list[dict]:
    """Varva två träfflistor (round-robin), dedupa på (source, page, chunk_idx).

    Round-robin (i stället för append) så fuzzy-träffarna får plats bland
    topp-N även utan reranker — annars tar ``hits[:top_n]`` bara vektorträffar."""
    import itertools  # noqa: PLC0415

    seen: set = set()
    out: list[dict] = []
    for h in itertools.chain.from_iterable(itertools.zip_longest(primary, extra)):
        if h is None:
            continue
        key = (h.get("source"), h.get("page"), h.get("chunk_idx"))
        if key not in seen:
            seen.add(key)
            out.append(h)
    return out


def extract_cited_sources(answer: str) -> list[dict]:
    """Bygg källlista ur ett MCP-svar genom att parsa unika Nr-citat."""
    sources: list[dict] = _citations.extract_cited_sources(answer, build_nr_to_pdf())
    return sources


def linkify_citations(text: str, known_sources: set[str] | None = None) -> str:
    """Förvandla "Nr X, sida Y" till inline-knappar — se citations.linkify_citations."""
    linked: str = _citations.linkify_citations(
        text, build_nr_to_pdf(), known_sources=known_sources
    )
    return linked


table, embed_model = load()
# Kompakt sidhuvud (delas med övriga sidor via casebook_ui); indexstorleken
# står i metadelen i stället för på en egen rad.
_casebook_ui.render_page_header(
    "Palmemordsarkivet",
    f"fråga arkivet med källhänvisningar · index: {table.count_rows():,} chunks",
)

_casebook_ui.render_pdf_opener(ROOT)

# Backend-katalogen bor i src/backends.py (delas med llm_config_cli). Claude-
# defaulten knyts lokalt till ask.CLAUDE_MODEL så Utredning-sidans val följer den modell
# RAG-svaren faktiskt körs med, även om katalogens default skulle divergera.
# Värdena sammanfaller idag; override:n görs utan att mutera den delade dicten.
BACKENDS = {
    **_backends.BACKENDS,
    "Claude": {**_backends.BACKENDS["Claude"], "model": CLAUDE_MODEL},
}

_BACKEND_KEYS = list(BACKENDS.keys())

# Verktygsscheman för OpenAI-kompatibla backends. Any-typade eftersom SDK:ns
# TypedDicts är omständliga att matcha exakt för JSON-schema-liknande strukturer.
OPENAI_TOOLS: list[Any] = [
    {
        "type": "function",
        "function": {
            "name": "search_archive",
            "description": (
                "Sök i Palmemordsarkivet och returnera relevanta textutdrag med källhänvisningar. "
                "Anropa flera gånger med olika söktermer för att täcka ett ämne från flera vinklar."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Sökfrågan på svenska"},
                    "top_k": {
                        "type": "integer",
                        "description": "Antal kandidater att hämta (5–50)",
                        "default": 20,
                    },
                    "top_n": {
                        "type": "integer",
                        "description": "Antal att behålla efter reranking (1–15)",
                        "default": 6,
                    },
                    "hybrid": {
                        "type": "boolean",
                        "description": "Kombinera vektor- och BM25-sökning",
                        "default": True,
                    },
                    "rerank": {
                        "type": "boolean",
                        "description": "Omranka med cross-encoder för bättre precision",
                        "default": True,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_page",
            "description": (
                "Hämta råtexten från en specifik sida i ett arkivdokument. "
                "Använd för att läsa mer kontext kring en träff från search_archive."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "Filnamn från söktträff, t.ex. '281 — Titel….txt'",
                    },
                    "page": {"type": "integer", "description": "Sidnummer (1-baserat)"},
                },
                "required": ["source", "page"],
            },
        },
    },
]


def _record_usage(cfg: dict, usage: dict | None, cost: float | None = None) -> None:
    """Bokför ett avslutat LLM-anrop i sessionens räknare och i state.db.

    Kostnaden används som leverantören rapporterar den när den finns (Claude
    Agent SDK:s ``total_cost_usd``), annars räknas den ur profilens priser.
    Saknas båda bokförs token ändå, och raden märks då som ofullständig."""
    # Även ett anrop helt utan rapporterad usage bokförs: en endpoint som inte
    # skickar usage ska räknas som ett anrop, inte döljas som "0 anrop". Utan
    # token blir kostnaden okänd i stället för noll.
    if cost is None and usage:
        cost = _llm_usage.cost_usd(usage, cfg.get("prices") or {})
    # st.session_state nås direkt: sidofältets panel (och därmed den här
    # funktionens anrop) körs innan modulens ``ss``-alias finns.
    _llm_usage.add_usage(
        st.session_state.setdefault("llm_usage_session", {}), usage, cost
    )
    profile = str(st.session_state.get("llm_profile") or "")
    if not profile:
        # Utan profilnamn finns ingen räknare att spara mot.
        return
    try:
        _state_db.add_llm_usage(
            _casebook_ui.state_conn(),
            profile=profile,
            model=str(cfg.get("model") or ""),
            input_tokens=int((usage or {}).get("input", 0)),
            output_tokens=int((usage or {}).get("output", 0)),
            cache_hit_tokens=int((usage or {}).get("cache_hit", 0)),
            cost=cost,
        )
    except Exception as exc:  # pragma: no cover - räknaren får aldrig fälla svaret
        log_error("llm_usage", profile, f"kunde inte spara räknaren: {exc}")
    # Ritas efter bokföringen så även profiltotalen (state.db) är färsk.
    _render_usage_panel(cfg)


def _render_usage_panel(cfg: dict) -> None:
    """Rita token- och kostnadsräknaren i sidofältets platshållare.

    Anropas både när sidan ritas och när ett anrop bokförts — då skrivs samma
    yta (``st.empty()``) om, så siffrorna tickar utan omladdning. Totalen
    kommer ur state.db (ackumulerat över sessioner, per profil), sessionens
    andel ur ``st.session_state``."""
    if _usage_slot is None:  # sidofältet har inte ritats än
        return
    profile = str(st.session_state.get("llm_profile") or "")
    totals = _state_db.get_llm_usage(_casebook_ui.state_conn(), profile)
    rader = [
        "<span class='palme-usage-titel'>Token & kostnad</span>",
        f"Totalt: {_llm_usage.format_summary(_llm_usage.totals_from_row(totals))}",
        "Session: "
        f"{_llm_usage.format_summary(st.session_state.get('llm_usage_session') or {})}",
    ]
    if cfg.get("kind") != "claude" and not (cfg.get("prices") or {}):
        rader.append("Priser saknas — sätt dem i Admin → Inställningar.")
    # Två $ i samma markdown-block blir LaTeX-matte hos Streamlit. Här ritas
    # panelen som HTML (för den mindre stilen), och där gäller inte markdowns
    # backslash-escape — dollartecknet skrivs som HTML-entitet i stället.
    _usage_slot.markdown(
        "<div class='palme-usage'>"
        + "<br>".join(rader).replace("$", "&#36;")
        + "</div>",
        unsafe_allow_html=True,
    )


st.session_state.setdefault("do_rerank", True)

# Räknarens plats längst ner i sidofältet. Ett st.empty() (inte container) så
# att samma yta kan skrivas om när ett anrop bokförts — sidofältet ritas före
# frågan och skulle annars visa förra anropets siffror.
_usage_slot = None

with st.sidebar:
    st.header("Inställningar")
    _all_llm = _llm_config.load_all()
    _profile_names = list(_all_llm["profiles"].keys())
    if "llm_profile" not in st.session_state or st.session_state["llm_profile"] not in _profile_names:
        st.session_state["llm_profile"] = _all_llm["default"]
    _profile_name = st.selectbox("LLM-profil", _profile_names, key="llm_profile")
    _profile = _all_llm["profiles"][_profile_name]
    try:
        backend = _llm_config.resolve_runtime_profile(_profile, BACKENDS)
    except ValueError as exc:
        st.error(f"LLM-profilen {_profile_name!r} kan inte användas: {exc}")
        st.stop()
    backend_name = backend["backend_name"]
    _profile_runtime_key = _llm_config.profile_cache_key(_profile_name, _profile)
    st.caption("Hantera profiler i **Admin → Inställningar → LLM-inställningar**.")
    show_graph = st.toggle(
        "Visa kunskapsgraf",
        value=True,
        key="show_graph",
        help="Visar en hopfällbar grafsektion under svaret. Själva grafen "
        "(entitetsextraktion med vald LLM-backend + Neo4j) byggs först när du "
        "öppnar den — inte automatiskt efter varje svar. "
        "Kräver att Neo4j är igång (.venv/bin/python scripts/neo4j.py).",
    )
    _usage_slot = st.empty()

_render_usage_panel(backend)


if backend["kind"] == "claude":
    if not (
        os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")
    ):
        st.error("Sätt `CLAUDE_CODE_OAUTH_TOKEN` eller `ANTHROPIC_API_KEY` i miljön.")
        st.stop()
elif backend.get("env") and not os.environ.get(backend["env"]):
    st.error(f"Sätt `{backend['env']}` i miljön för att använda {backend_name}.")
    st.stop()

# Cacha senaste sökning + svar i session_state så att klick på "Öppna PDF"
# (som triggar streamlit-rerun) inte tvingar fram en ny Claude-anrop.
ss = st.session_state
ss.setdefault("question", "")
ss.setdefault("hits", None)
ss.setdefault("answer", "")
# MCP-chatt: history-lista med {"role", "text", "sources"} och resume-id för Claude.
ss.setdefault("chat_history", [])
ss.setdefault("mcp_session_id", None)
ss.setdefault("openai_chat_messages", [])
ss.setdefault("answer_centers", [])

def _mcp_tool_label(name: str, inp: dict) -> str:
    """Människoläsbar etikett för ett MCP-verktygsanrop (namnet är prefixat
    ``mcp__arkiv__``)."""
    short = name.rsplit("__", 1)[-1]
    if short == "search_archive":
        return f'🔍 Söker: "{inp.get("query", "")}"'
    if short == "get_page":
        return f'📄 Läser: {inp.get("source", "")}, sida {inp.get("page", "")}'
    return f"🔧 {short}"


def _truncated_notice(reason: str | None, component: str, model: str) -> str | None:
    """Avklippt-svar-varning + fellogg när leverantören stoppat mitt i texten.

    Loggas som fel (inte debug) eftersom ett avklippt svar är en avvikelse som
    ska gå att hitta i errors.log i efterhand — annars syns den bara som en
    oförklarlig halv mening i gränssnittet."""
    notice: str | None = stop_notice(reason)
    if notice:
        log_error(component, f"finish_reason={reason}", f"avklippt svar från {model}")
    return notice


async def stream_mcp(
    q: str, status_box, text_placeholder, parts: list[str], resume_id: str | None,
    cfg: dict,
) -> tuple[str | None, int]:
    """Utredningsläge: Claude anropar search_archive/get_page autonomt.

    Skriver varje verktygsanrop till ``status_box`` så användaren ser att
    sökningarna faktiskt körs (kan ta 1–3 min). Returnerar Claudes session_id
    (så nästa fråga kan resume:a samma konversation) och antalet verktygsanrop."""
    db_dir = ROOT / "generated" / "lancedb"
    env = {
        "DB_DIR": str(db_dir),
        **{
            k: v
            for k, v in os.environ.items()
            if k
            in (
                "CLAUDE_CODE_OAUTH_TOKEN",
                "ANTHROPIC_API_KEY",
                "PATH",
                "HOME",
                "VIRTUAL_ENV",
                "EMBED_MODEL",
            )
        },
    }
    options = ClaudeAgentOptions(
        system_prompt=MCP_SYSTEM_PROMPT,
        model=cfg["model"],
        mcp_servers={
            "arkiv": {"command": sys.executable, "args": [str(MCP_SERVER)], "env": env}
        },
        allowed_tools=["mcp__arkiv__search_archive", "mcp__arkiv__get_page"],
        thinking=ThinkingConfigAdaptive(type="adaptive"),
        effort="high",
        max_turns=10,
        setting_sources=[],
        resume=resume_id,
    )
    new_session_id: str | None = None
    tool_count = 0
    async for message in query(prompt=q, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    parts.append(block.text)
                    text_placeholder.markdown("".join(parts))
                elif isinstance(block, ToolUseBlock):
                    tool_count += 1
                    status_box.write(_mcp_tool_label(block.name, block.input or {}))
        elif isinstance(message, ResultMessage):
            new_session_id = message.session_id
            _record_usage(cfg, _llm_usage.usage_from_claude(message.usage), message.total_cost_usd)
            # Claude Code kan avsluta mitt i en mening: dels vid max_tokens
            # (stop_reason), dels vid max_turns/andra fel (is_error + subtype).
            reason = message.subtype if message.is_error else message.stop_reason
            notice = _truncated_notice(reason, "ask.claude", cfg["model"])
            if notice:
                parts.append(f"\n\n{notice}")
                text_placeholder.markdown("".join(parts))
    return new_session_id, tool_count


async def stream_claude(user_msg: str, placeholder, parts: list[str], cfg: dict) -> None:
    options = ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        model=cfg["model"],
        allowed_tools=[],
        thinking=ThinkingConfigAdaptive(type="adaptive"),
        effort="high",
        max_turns=1,
        setting_sources=[],
    )
    async for message in query(prompt=user_msg, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    parts.append(block.text)
                    placeholder.markdown("".join(parts))
        elif isinstance(message, ResultMessage):
            _record_usage(cfg, _llm_usage.usage_from_claude(message.usage), message.total_cost_usd)
            reason = message.subtype if message.is_error else message.stop_reason
            notice = _truncated_notice(reason, "ask.claude", cfg["model"])
            if notice:
                parts.append(f"\n\n{notice}")
                placeholder.markdown("".join(parts))


async def stream_openai(user_msg: str, placeholder, parts: list[str], cfg) -> None:
    from openai import AsyncOpenAI, NotFoundError  # noqa: PLC0415

    # Samma nyckeluppslagning som MCP-läget: sidofältets fält vinner över env.
    api_key = cfg.get("api_key") or "ollama"
    base_url = cfg["base_url"]
    model = cfg["model"]
    reason: str | None = None
    usage = None
    async with AsyncOpenAI(api_key=api_key or "ollama", base_url=base_url) as client:
        try:
            stream = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                stream=True,
                # Be om tokenräkningen i ett eget slut-chunk (stöds av OpenAI,
                # DeepSeek och Ollama).
                stream_options={"include_usage": True},
            )
        except NotFoundError as exc:
            hint = ""
            if "page not found" in str(exc).lower() or "404" in str(exc):
                hint = f"\n\n**Tips:** Kontrollera att endpoint-URL:en är rätt (`{base_url}`). Ollamas standard är `http://localhost:11434/v1`."
            elif "not found" in str(exc).lower():
                hint = f"\n\n**Tips:** Modellen `{model}` finns inte. Kör `ollama pull {model}` eller välj ett annat modellnamn."
            st.error(f"404 från {base_url}: {exc}{hint}")
            return
        async for chunk in stream:
            # Usage kommer i ett sista chunk som saknar choices — läs den innan
            # vi hoppar över tomma val.
            if chunk.usage is not None:
                usage = chunk.usage
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if delta is not None:
                parts.append(delta)
                placeholder.markdown("".join(parts))
            # finish_reason kommer ofta i en chunk utan innehåll — spara den
            # och bedöm efter strömmen så även andra stopporsaker än "length"
            # (t.ex. DeepSeek-resursbrist) syns i svaret.
            reason = chunk.choices[0].finish_reason or reason
        _record_usage(cfg, _llm_usage.usage_from_openai(usage))
        notice = _truncated_notice(reason, "ask.openai", model)
        if notice:
            parts.append(f"\n\n{notice}")
            placeholder.markdown("".join(parts))


async def stream_to_string(hits, q, cfg, placeholder=None) -> str:
    user_msg = f"Utdrag ur arkivet:\n\n{format_context(hits)}\n\n---\n\nFråga: {q}"
    if placeholder is None:
        placeholder = st.empty()
    parts: list[str] = []
    if cfg["kind"] == "claude":
        await stream_claude(user_msg, placeholder, parts, cfg)
    else:
        await stream_openai(user_msg, placeholder, parts, cfg)
    known = {h["source"] for h in hits}
    final = linkify_citations("".join(parts), known_sources=known)
    placeholder.markdown(final, unsafe_allow_html=True)
    return final


async def stream_mcp_to_string(
    q: str, resume_id: str | None, cfg: dict
) -> tuple[str, str | None]:
    status_box = st.status("Söker i arkivet…", expanded=True)
    text_placeholder = st.empty()
    parts: list[str] = []
    try:
        new_id, tool_count = await stream_mcp(
            q, status_box, text_placeholder, parts, resume_id, cfg
        )
    except Exception as exc:
        err_str = str(exc)
        if any(k in err_str.lower() for k in ("authenticate", "403", "exit code 1", "unauthorized")):
            msg = (
                "*Fel: Claude Code är inte inloggad. "
                "Kör `claude auth login` i terminalen och starta om webgränssnittet.*"
            )
        else:
            msg = f"*Fel i utredningsläget: {exc}*"
        status_box.update(label="Fel i utredningsläget", state="error", expanded=False)
        text_placeholder.markdown(msg)
        return msg, None
    final = linkify_citations("".join(parts))
    text_placeholder.markdown(final, unsafe_allow_html=True)
    n = tool_count
    suffix = "ar" if n != 1 else ""
    done = "a" if n != 1 else ""
    status_box.update(
        label=f"{n} sökning{suffix} gjord{done}",
        state="complete",
        expanded=False,
    )
    return final, new_id


def _run_tool(name: str, arguments: dict) -> str:
    import mcp_server  # noqa: PLC0415

    mcp_server._table = table
    mcp_server._model = embed_model
    if name == "search_archive":
        args = dict(arguments)
        args["top_k"], args["top_n"] = mcp_server.clamp_result_limits(
            args.get("top_k", mcp_server.TOP_K_DEFAULT),
            args.get("top_n", mcp_server.TOP_N_DEFAULT),
        )
        result: str = mcp_server.search_archive(**args)
        return result
    if name == "get_page":
        page: str = mcp_server.get_page(**arguments)
        return page
    return f"Okänt verktyg: {name}"


async def stream_openai_mcp(
    status_box,
    text_placeholder,
    parts: list[str],
    cfg: dict,
    messages: list[Any],
) -> None:
    """Utredningsläge för OpenAI-kompatibla backends.

    messages innehåller redan user-meddelandet. Assistentens svar och
    tool-resultat appendas direkt till messages (konversationshistorik).
    """
    import json  # noqa: PLC0415

    from openai import AsyncOpenAI  # noqa: PLC0415
    from openai.types.chat import ChatCompletionMessageFunctionToolCall  # noqa: PLC0415

    api_key = cfg.get("api_key") or "ollama"
    tool_count = 0
    turns_usage: dict = {}
    try:
        async with AsyncOpenAI(api_key=api_key or "ollama", base_url=cfg["base_url"]) as client:
            for _turn in range(10):
                response = await client.chat.completions.create(
                    model=cfg["model"],
                    messages=messages,
                    tools=OPENAI_TOOLS,
                )
                # Icke-strömmande svar: usage per tur, summeras över turerna.
                _llm_usage.add_usage(
                    turns_usage, _llm_usage.usage_from_openai(response.usage), None
                )
                choice = response.choices[0]
                msg = choice.message

                if choice.finish_reason == "tool_calls" and msg.tool_calls:
                    messages.append(
                        {
                            "role": "assistant",
                            "content": msg.content,
                            "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
                        }
                    )
                    for tc in msg.tool_calls:
                        # Vi definierar bara function-verktyg; custom tool calls
                        # (serverdefinierade) har inget .function-attribut.
                        if not isinstance(tc, ChatCompletionMessageFunctionToolCall):
                            continue
                        args = json.loads(tc.function.arguments)
                        tool_count += 1
                        if tc.function.name == "search_archive":
                            label = f'search_archive: "{args.get("query", "")}"'
                        else:
                            label = f'get_page: {args.get("source", "")}, sida {args.get("page", "")}'
                        status_box.write(label)
                        result = _run_tool(tc.function.name, args)
                        messages.append(
                            {
                                "role": "tool",
                                "content": result,
                                "tool_call_id": tc.id,
                            }
                        )
                else:
                    final = msg.content or ""
                    # Endast finish_reason "stop" betyder ett färdigt svar.
                    # "length" och DeepSeeks "insufficient_system_resource"
                    # ger en halv mening som annars visas som komplett.
                    notice = _truncated_notice(
                        choice.finish_reason, "ask.openai-mcp", cfg["model"]
                    )
                    if notice:
                        final = f"{final}\n\n{notice}" if final else notice
                    parts.append(final)
                    text_placeholder.markdown(final)
                    messages.append({"role": "assistant", "content": final})
                    break
            else:
                truncated = "*[Svar avklippt — modellen nådde gränsen för antal verktygsanrop.]*"
                parts.append(truncated)
                text_placeholder.markdown(truncated)
                messages.append({"role": "assistant", "content": truncated})
    except Exception as exc:
        error_msg = f"*Fel vid anrop till {cfg['model']}: {exc}*"
        parts.append(error_msg)
        text_placeholder.markdown(error_msg)

    n = tool_count
    suffix = "ar" if n != 1 else ""
    done = "a" if n != 1 else ""
    status_box.update(
        label=f"{n} sökning{suffix} gjord{done}",
        state="complete",
        expanded=False,
    )
    # Kostnaden räknas en gång för hela frågan (alla turer) ur profilens priser.
    _record_usage(
        cfg,
        turns_usage,
        _llm_usage.cost_usd(turns_usage, cfg.get("prices") or {})
        if turns_usage
        else None,
    )


async def stream_openai_mcp_to_string(cfg: dict, messages: list[Any]) -> str:
    status_box = st.status("Söker i arkivet…", expanded=True)
    text_placeholder = st.empty()
    parts: list[str] = []
    await stream_openai_mcp(status_box, text_placeholder, parts, cfg, messages)
    final = linkify_citations("".join(parts))
    text_placeholder.markdown(final, unsafe_allow_html=True)
    return final


# CSS-animerad "tänker"-indikator: pulsande stjärna + roterande ord.
# Injiceras via unsafe_allow_html i st.empty()-platshållare och skrivs
# automatiskt över av den strömmande texten så fort modellen börjar svara.
_THINKING_HTML = (
    '<div style="display:flex;align-items:center;gap:10px;padding:6px 0;">'
    '<span style="display:inline-block;animation:_ps 1.4s ease-in-out infinite;'
    'color:#a855f7;font-size:1.2em;line-height:1;">✦</span>'
    '<span style="position:relative;display:inline-block;min-width:130px;height:1.4em;">'
    '<span style="position:absolute;top:0;left:0;opacity:0;'
    'animation:_pw 4.8s 0s infinite;font-style:italic;">Söker…</span>'
    '<span style="position:absolute;top:0;left:0;opacity:0;'
    'animation:_pw 4.8s 1.2s infinite;font-style:italic;">Läser dokument…</span>'
    '<span style="position:absolute;top:0;left:0;opacity:0;'
    'animation:_pw 4.8s 2.4s infinite;font-style:italic;">Analyserar…</span>'
    '<span style="position:absolute;top:0;left:0;opacity:0;'
    'animation:_pw 4.8s 3.6s infinite;font-style:italic;">Sammanställer…</span>'
    '</span>'
    '</div>'
    '<style>'
    '@keyframes _ps{0%,100%{opacity:1;transform:scale(1)}'
    '50%{opacity:.15;transform:scale(.45)}}'
    '@keyframes _pw{0%{opacity:0}5%{opacity:.8}20%{opacity:.8}25%,100%{opacity:0}}'
    '</style>'
)


GRAPH_HEIGHT = 620


@st.cache_resource(show_spinner=False)
def _connect_graph(pw: str):
    """Cachad Neo4j-uppkoppling (stabil över reruns). Exceptions cachas inte
    av st.cache_resource, så en nere-Neo4j provas om vid nästa rerun."""
    return _viz.connect(pw)


def _graph_driver():
    """Neo4j-driver för inline-grafen, eller None om grafen är otillgänglig.

    Misslyckade anslutningar cachas inte — startar användaren Neo4j senare
    plockas den upp vid nästa rerun utan cache-rensning."""
    pw = _viz.resolve_password()
    if not pw:
        return None
    try:
        return _connect_graph(pw)
    except Exception:  # noqa: BLE001 — grafen är frivillig, svaret får aldrig falla
        return None


def _compute_answer_centers(answer: str, profile: dict) -> list[dict]:
    """Svar → LLM-entitetslista → center-noder i grafen. Fel → tom lista."""
    driver = _graph_driver()
    if driver is None or not answer.strip():
        return []
    cfg = _answer_entities.resolve_entity_cfg(profile)
    if cfg is None:
        return []
    try:
        names = asyncio.run(_answer_entities.extract_answer_entities(answer, cfg))
        if not names:
            return []
        with driver.session() as s:
            centers: list[dict] = _viz.lookup_centers(s, names)
            return centers
    except Exception as exc:  # noqa: BLE001
        log_error("utredning.graph", answer[:60], str(exc))
        return []


def _render_graph_panel(nodes: list[dict], state_key: str,
                        extra_key: str) -> int:
    """Sidopanelen i grafsektionen: höjdreglage, legend, återställning av
    utfällda noder och PDF-länkar för dokumentnoderna. Returnerar vald
    grafhöjd i px. (Utfällning sker via dubbelklick direkt i grafen.)"""
    import html as _html  # noqa: PLC0415

    height = st.slider("Höjd (px)", 400, 1200, GRAPH_HEIGHT, step=50,
                       key=f"{state_key}_h")

    st.caption("Noder")
    present = {n["type"] for n in nodes}
    legend = [
        f'<span style="color:{_viz.NODE_COLORS[typ]};">'
        f'{"■" if typ == "Dokument" else "●"}</span> {typ}'
        for typ in ("Person", "Plats", "Organisation", "Dokument")
        if typ in present
    ]
    legend.append("<small>★ = svarets entitet / utfälld nod</small>")
    st.markdown("<br>".join(legend), unsafe_allow_html=True)

    if ss[extra_key] and st.button("Återställ", key=f"{state_key}_reset",
                                   use_container_width=True):
        ss[extra_key] = []
        st.rerun()

    # Dokumentnoder som klickbara PDF-länkar — samma ?pdf=-mekanism som
    # citatlänkarna i svaret (laddas i den gömda pdf_opener-iframen).
    doc_nodes = sorted((n for n in nodes if n["type"] == "Dokument"),
                       key=lambda n: n["namn"])
    if doc_nodes:
        st.caption("Dokument")
        links = []
        for n in doc_nodes:
            pdf = find_pdf(n["stem"])
            label = _html.escape(str(n["namn"]))
            links.append(_citations.pdf_anchor(pdf, label, title=n["stem"])
                         if pdf else label)
        st.markdown("<br>".join(links), unsafe_allow_html=True)
    return height


def _render_cytoscape_graph(nodes: list[dict], edges: list[dict],
                            expanded_norms: set[str], height: int,
                            state_key: str, extra_key: str) -> None:
    """Interaktiv Cytoscape-graf (st-link-analysis).

    Dubbelklick på en entitetsnod fäller ut dess grannskap; dubbelklick på en
    dokumentnod öppnar PDF:en i ny webbläsarflik. Expand-händelser dedupas på
    timestamp eftersom komponentens returvärde består över reruns."""
    elements = _viz.to_cytoscape_elements(nodes, edges)
    node_styles = [
        NodeStyle(typ, _viz.NODE_COLORS[typ], "name", _viz.NODE_ICONS[typ])
        for typ in ("Person", "Plats", "Organisation", "Dokument")
    ]
    # labeled=None kringgår en biblioteksbugg: deprecation-varningen triggar
    # på "is not None" så även defaultvärdet False varnar.
    edge_styles = [EdgeStyle("REL", caption="name", labeled=None,
                             directed=True)]
    # Höjden bakas in i nyckeln — komponenten läser height bara vid mount.
    ret = st_link_analysis(
        elements, layout="cose",
        node_styles=node_styles, edge_styles=edge_styles,
        height=height, key=f"{state_key}_cy_{height}",
        node_actions=["expand"],
    )
    st.caption("Dubbelklick: visa entitetsnods grannskap · öppna dokumentnod")

    if not ret or ret.get("action") != "expand":
        return
    ts = ret.get("timestamp")
    if ts == ss.get(f"{state_key}_last_evt"):
        return
    ss[f"{state_key}_last_evt"] = ts
    by_id = {n["id"]: n for n in nodes}
    changed = False
    for nid in ret.get("data", {}).get("node_ids", []):
        n = by_id.get(nid)
        if n is None:
            continue
        if n["type"] == "Dokument":
            pdf = find_pdf(n.get("stem") or "")
            if pdf:
                _casebook_ui.open_pdf_in_browser(pdf)
        elif nid not in expanded_norms:
            ss[extra_key].append({"norm": nid, "namn": n["namn"],
                                  "label": n["type"]})
            changed = True
    if changed:
        st.rerun()


def _render_answer_graph(answer: str, state_key: str) -> list[dict] | None:
    """Rita ego-nätverk för svarets entiteter i en hopfällbar sektion i fullbredd
    mellan svaret och källorna. Returnerar de beräknade center-noderna (eller
    en tom lista) så utredningspärmen kan spara dem. None betyder att grafen
    misslyckades (t.ex. Neo4j stängdes mitt i ritningen).

    **Grafen byggs först när användaren öppnar toggeln** — då, och bara då, körs
    den dyra entitetsextraktionen (LLM) och Neo4j-frågorna. Resultatet cachas
    per svar i session state, så att öppna/stänga eller andra reruns inte
    räknar om det. Sektionen styrs av en keyad toggle i stället för st.expander:
    Cytoscape-komponenten saknar resize-hantering och blir tom om den monteras i
    en hopfälld (dold) container.

    Grundgrafen visar ENDAST svarets entiteter och relationerna mellan dem —
    inte deras hela grannskap (det blev oöverskådligt). Dubbelklick på en nod
    fäller ut dess grannskap (relationer + dokument); de utfällda noderna
    sparas i session state per ``state_key`` och nollställs när svaret byts.
    Sidopanelen ger legend, höjdreglage, återställning och PDF-länkar för
    synliga dokumentnoder."""
    if not _HAS_LINK_ANALYSIS:
        st.caption("Kunskapsgraf otillgänglig — installera grafextran: "
                   "`pip install -e .[graph]`.")
        return []
    driver = _graph_driver()
    if driver is None:
        st.caption("Kunskapsgraf otillgänglig — starta Neo4j med "
                   "`.venv/bin/python scripts/neo4j.py`.")
        return []
    if not answer.strip():
        return []

    centers_key = f"{state_key}_centers"
    answer_key = f"{state_key}_answer"
    extra_key = f"{state_key}_extra"

    # Cache-identiteten måste beräknas innan toggeln läses: annars återanvänds
    # föregående svars entiteter och sparas i utredningspärmen för fel fråga.
    cache_identity = (answer, _profile_runtime_key)

    # Bygg inte grafen förrän användaren öppnar den.
    if not st.toggle("🕸 Visa kunskapsgraf", key=f"{state_key}_open",
                     help="Extraherar svarets entiteter och ritar deras nätverk "
                     "ur kunskapsgrafen. Byggs först när du öppnar den."):
        if ss.get(answer_key) != cache_identity:
            return []
        saved: list[dict] = ss.get(centers_key, [])
        return saved

    # Lat beräkning: kör entitetsextraktionen en gång per svar och cacha den.
    # Nytt svar nollställer även utfällda noder.
    if ss.get(answer_key) != cache_identity:
        with st.spinner("Bygger kunskapsgraf…"):
            ss[centers_key] = _compute_answer_centers(answer, backend)
        ss[answer_key] = cache_identity
        ss[extra_key] = []
    centers: list[dict] = ss.get(centers_key, [])
    ss.setdefault(extra_key, [])

    if not centers:
        st.caption("Inga entiteter ur svaret återfanns i grafen.")
        return centers

    # Utfällda noder kan vara både svars-entiteter och grannar; dedupa
    # hämtningslistan men behåll utfälld-status separat.
    base_ids = {(c["label"], c["norm"]) for c in centers}
    expanded_norms = {c["norm"] for c in ss[extra_key]}
    fetch_centers = centers + [c for c in ss[extra_key]
                               if (c["label"], c["norm"]) not in base_ids]

    all_rels: list[dict] = []
    all_docs: list[dict] = []
    try:
        with driver.session() as s:
            for c in fetch_centers:
                rels, docs = _viz.fetch_ego(s, c["norm"], c["label"], limit=40)
                all_rels.extend(rels)
                for d in docs:
                    all_docs.append({**d, "center_norm": c["norm"]})
    except Exception as exc:  # noqa: BLE001
        log_error("utredning.graph", centers[0]["namn"], str(exc))
        st.caption("Kunskapsgraf otillgänglig — starta Neo4j med "
                   "`.venv/bin/python scripts/neo4j.py`.")
        return None

    # Visa bara det svaret nämner: relationer mellan svarets entiteter.
    # Grannskap (alla relationer + dokument) bara för utfällda noder.
    visible = {c["norm"] for c in fetch_centers}
    all_rels = [r for r in all_rels
                if (r["s_norm"] in visible and r["e_norm"] in visible)
                or r["s_norm"] in expanded_norms
                or r["e_norm"] in expanded_norms]
    all_docs = [d for d in all_docs if d.get("center_norm") in expanded_norms]

    nodes, edges = _viz.assemble_graph(fetch_centers, _viz.dedup_rels(all_rels),
                                       all_docs)
    graph_col, panel_col = st.columns([4, 1])
    with panel_col:
        height = _render_graph_panel(nodes, state_key, extra_key)
    with graph_col:
        _render_cytoscape_graph(nodes, edges, expanded_norms, height,
                                state_key, extra_key)
    return centers


def _render_rag_sources(hits: list, key_prefix: str) -> None:
    with st.expander(f"Källor ({len(hits)})", expanded=False):
        _casebook_ui.render_source_cards(
            ROOT,
            hits,
            casebook_conn,
            key_prefix=f"{key_prefix}_source",
        )


def _render_chat_sources(srcs: list, key_prefix: str) -> None:
    with st.expander(f"Källor ({len(srcs)})", expanded=False):
        _casebook_ui.render_source_cards(
            ROOT,
            srcs,
            casebook_conn,
            key_prefix=f"{key_prefix}_source",
        )


def _render_chat_turn(turn: dict, turn_idx: int) -> None:
    """Rendera en historiktur; assistentturer med centers får grafexpander
    mellan svaret och källorna."""
    with st.chat_message(turn["role"]):
        st.markdown(turn["text"], unsafe_allow_html=True)
        centers = turn.get("centers") or []
        if turn["role"] == "assistant" and show_graph:
            # Lat: grafen (och entitetsextraktionen) byggs först vid öppning.
            centers = _render_answer_graph(turn["text"], f"turn_{turn_idx}")
        if turn["role"] == "assistant":
            prev_q = ""
            if turn_idx > 0 and ss.chat_history[turn_idx - 1]["role"] == "user":
                prev_q = ss.chat_history[turn_idx - 1]["text"]
            _casebook_ui.render_casebook_save(
                casebook_conn,
                question=prev_q,
                answer=turn["text"],
                mode="mcp",
                backend_name=backend_name,
                model=backend["model"],
                sources=turn.get("sources") or [],
                centers=centers,
                key=f"chat_casebook_{turn_idx}",
            )
        srcs = turn.get("sources") or []
        if srcs:
            _render_chat_sources(srcs, f"chat_pdf_{turn_idx}")


def _render_mcp_tab() -> None:
    """Utredningsläget: modellen söker autonomt med MCP-verktygen och minns
    tidigare frågor i konversationen (Claude via ``resume``, OpenAI-kompatibla
    via meddelandehistoriken)."""
    _info, _new = st.columns([4, 1])
    with _info:
        st.caption(
            "Modellen söker själv i arkivet och minns tidigare frågor i "
            "konversationen."
        )
    with _new:
        if st.button("Ny konversation", use_container_width=True):
            st.session_state.chat_history = []
            st.session_state.mcp_session_id = None
            st.session_state.openai_chat_messages = []
            # Rensa graf-state per tur så en ny konversations tur N inte ärver
            # utfällda noder från den gamla.
            for k in [k for k in st.session_state if k.startswith("turn_")]:
                del st.session_state[k]
            st.rerun()

    if backend["kind"] == "claude":
        # Chatt-läge: rendera historiken först, sedan st.chat_input nederst.
        for turn_idx, turn in enumerate(ss.chat_history):
            _render_chat_turn(turn, turn_idx)

        chat_q = st.chat_input("Ställ en fråga till utredningsassistenten…")
        if chat_q and chat_q.strip():
            ss.chat_history.append({"role": "user", "text": chat_q, "sources": []})
            with st.chat_message("user"):
                st.markdown(chat_q)
            with st.chat_message("assistant"):
                answer, new_id = asyncio.run(
                    stream_mcp_to_string(chat_q, ss.mcp_session_id, backend)
                )
                centers: list[dict] | None = []
                if show_graph:
                    # Lat: grafen byggs först när toggeln öppnas. Samma state_key
                    # som turen får i historiken efter append.
                    centers = _render_answer_graph(
                        answer, f"turn_{len(ss.chat_history)}"
                    )
            ss.mcp_session_id = new_id
            ss.chat_history.append(
                {
                    "role": "assistant",
                    "text": answer,
                    "sources": extract_cited_sources(answer),
                    "centers": centers,
                }
            )
            st.rerun()
    else:
        for turn_idx, turn in enumerate(ss.chat_history):
            _render_chat_turn(turn, turn_idx)

        chat_q = st.chat_input("Ställ en fråga till utredningsassistenten…")
        if chat_q and chat_q.strip():
            if not ss.openai_chat_messages:
                ss.openai_chat_messages.append(
                    {"role": "system", "content": MCP_SYSTEM_PROMPT}
                )
            ss.openai_chat_messages.append({"role": "user", "content": chat_q})
            ss.chat_history.append({"role": "user", "text": chat_q, "sources": []})
            with st.chat_message("user"):
                st.markdown(chat_q)
            with st.chat_message("assistant"):
                answer = asyncio.run(
                    stream_openai_mcp_to_string(backend, ss.openai_chat_messages)
                )
                centers = []
                if show_graph:
                    # Lat: grafen byggs först när toggeln öppnas. Samma state_key
                    # som turen får i historiken efter append.
                    centers = _render_answer_graph(
                        answer, f"turn_{len(ss.chat_history)}"
                    )
            ss.chat_history.append(
                {
                    "role": "assistant",
                    "text": answer,
                    "sources": extract_cited_sources(answer),
                    "centers": centers,
                }
            )
            st.rerun()


def _render_rag_tab() -> None:
    """RAG-läget: en fråga, fast pipeline, ett svar. Sökinställningarna nedan
    påverkar bara den här fliken."""
    with st.expander("Sökinställningar", expanded=False):
        _facet_to_name: dict[str, str] = {}

        do_rerank = st.toggle(
            "Använd cross-encoder reranker",
            key="do_rerank",
            help="Långsammare första gången (laddar ~568 MB) men bättre precision.",
        )
        top_k = st.slider(
            "Hämta top-K kandidater",
            5,
            50,
            20,
            help="Antal chunks som vektorsökningen plockar fram ur indexet i första "
            "steget. Högre K → fler alternativ för rerankern att välja bland "
            "(bättre täckning) men långsammare. Utan reranker används bara de "
            "första top-N av dessa.",
        )
        top_n = st.slider(
            "Skicka top-N till AI",
            1,
            15,
            6,
            help="Antal chunks (efter ev. reranking) som faktiskt skickas som "
            "kontext till språkmodellen. Högre N → mer underlag men längre "
            "prompt, högre kostnad och risk att modellen tappar fokus.",
        )

        # Sökfilter (RAG-läget): facetter ur kunskapsgrafen + OCR-tolerant fuzzy.
        st.subheader("Sökfilter")
        _facet_data = _load_facets()
        _facet_options: list[str] = []
        for _typ in _facets.FACET_TYPES:
            for _namn, _cnt in _facet_data.get(_typ, [])[:50]:
                _label = f"{_typ}: {_namn} ({_cnt})"
                _facet_options.append(_label)
                _facet_to_name[_label] = _namn
        selected_facets = st.multiselect(
            "Begränsa till entiteter",
            _facet_options,
            help="Visa bara träffar ur dokument som nämner valda personer/platser/"
            "organisationer (ur kunskapsgrafen). Tomt = ingen begränsning.",
        )
        fuzzy_on = st.toggle(
            "OCR-tolerant fuzzy-sökning",
            value=False,
            help="Lägg till träffar där söktermer förekommer felstavade av OCR "
            "(t.ex. 'Engstrcm' för 'Engström'). Första körningen bygger ett index "
            "(~30 s, ~100 MB minne).",
        )
        fuzzy_threshold = st.slider(
            "Fuzzy-likhet (tröskel)",
            0.50, 0.95, 0.70, step=0.05,
            help="Lägre = fångar fler felstavningar men mer brus. Korta namn med "
            "ett OCR-fel (t.ex. 'Palme'→'Paine') kräver ~0.6; längre ord klarar "
            "högre tröskel. Påverkar bara när fuzzy-sökning är på.",
            disabled=not fuzzy_on,
        )

    with st.form("ask"):
        q = st.text_input("Din fråga", placeholder="Vem är Stig Engström?")
        submitted = st.form_submit_button("Fråga", type="primary")

    if submitted and q.strip():
        ss.question = q
        with st.status("Söker i indexet…", expanded=False) as status:
            # Facetter prefiltrerar vektorsökningen så dokument utanför ofiltrerade
            # topp-K ändå kan ytas; samla valda entiteters stems först.
            facet_stems: set[str] = set()
            if selected_facets:
                facet_index = _load_facet_index()
                for _label in selected_facets:
                    facet_stems |= facet_index.get(
                        _facet_to_name[_label].casefold(), set()
                    )
                status.update(label="Söker bland valda entiteters dokument…")
            where = _facets.sources_where_clause(facet_stems)
            hits = search(table, embed_model, q, top_k, where=where)
            if fuzzy_on:
                status.update(label="Lägger till OCR-toleranta träffar…")
                rows, index = _load_fuzzy_corpus()
                fuzzy_hits = _search_fuzzy.fuzzy_search(
                    rows, q, index=index, top_k=top_k, threshold=fuzzy_threshold
                )
                if facet_stems:
                    fuzzy_hits = _facets.filter_hits_by_stems(fuzzy_hits, facet_stems)
                hits = _interleave_hits(hits, fuzzy_hits)
            if not hits:
                if selected_facets:
                    status.update(
                        label="Inga träffar bland de valda entiteterna — ta bort "
                        "en facett eller höj top-K",
                        state="error",
                    )
                else:
                    status.update(label="Inga träffar", state="error")
                ss.hits, ss.answer = None, ""
                return  # avbryt bara den här fliken, inte chatten
            if do_rerank:
                status.update(label="Omrankar med cross-encoder…")
                hits = rerank(q, hits, top_n)
            else:
                hits = hits[:top_n]
            status.update(
                label=f"Hittade {len(hits)} relevanta chunks", state="complete"
            )
        ss.hits = hits

        st.subheader(f"Svar ({backend_name})")
        # Skapa placeholders i skärmordning (svar → graf → källor) innan
        # asyncio.run() blockerar — _src_slot ersätter omedelbart ev. gamla
        # källexpander från föregående sökning.
        _stream_slot = st.empty()
        _graph_slot = st.empty()
        _src_slot = st.empty()
        _stream_slot.markdown(_THINKING_HTML, unsafe_allow_html=True)
        ss.answer = asyncio.run(stream_to_string(hits, q, backend, _stream_slot))
        with _src_slot.container():
            _render_rag_sources(hits, "sub")
        ss.answer_centers = []
        if show_graph:
            with _graph_slot.container():
                # Lat: entitetsextraktionen körs först när användaren öppnar grafen.
                ss.answer_centers = _render_answer_graph(ss.answer, "rag")
        _casebook_ui.render_casebook_save(
            casebook_conn,
            question=q,
            answer=ss.answer,
            mode="rag",
            backend_name=backend_name,
            model=backend["model"],
            sources=hits,
            centers=ss.answer_centers,
            key="rag_current",
        )

    # Rendera resultat från session_state vid rerun från PDF-knappar (ej ny sökning).
    # Bara i RAG-läget — MCP-chatten renderar sina källor inline per tur.
    if ss.hits and not (submitted and q.strip()):
        st.subheader("Svar")
        st.markdown(ss.answer, unsafe_allow_html=True)
        if show_graph:
            ss.answer_centers = _render_answer_graph(ss.answer, "rag")
        _casebook_ui.render_casebook_save(
            casebook_conn,
            question=ss.question,
            answer=ss.answer,
            mode="rag",
            backend_name=backend_name,
            model=backend["model"],
            sources=ss.hits,
            centers=ss.answer_centers,
            key="rag_cached",
        )
        _render_rag_sources(ss.hits, "cached")


_tab_rag, _tab_mcp = st.tabs(["Fråga arkivet (RAG)", "Utredningsläge (MCP)"])
with _tab_rag:
    _render_rag_tab()
with _tab_mcp:
    _render_mcp_tab()
