"""Enkelt webgränssnitt för palmemordsarkivet via Streamlit.

Kör med:
    ./web.sh
eller manuellt:
    .venv/bin/streamlit run webui.py
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import subprocess
import sys
from pathlib import Path

MCP_SERVER = Path(__file__).resolve().parent / "rag" / "mcp_server.py"

import lancedb
import streamlit as st
from sentence_transformers import SentenceTransformer

# importera funktioner ur rag/ask.py utan att köra dess main()
sys.path.insert(0, str(Path(__file__).resolve().parent / "rag"))
from ask import (  # type: ignore  # noqa: E402
    CLAUDE_MODEL,
    EMBED_MODEL,
    MCP_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    TABLE,
    format_context,
    rerank,
    search,
)
from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ThinkingConfigAdaptive,
    query,
)

ROOT = Path(__file__).resolve().parents[1]

st.set_page_config(page_title="Palmemordsarkivet", layout="wide")
st.title("Palmemordsarkivet")
st.caption("Fråga arkivet — sökning + AI med källhänvisningar.")


@st.cache_resource(show_spinner="Laddar embedding-modell…")
def load():
    db = lancedb.connect(str(ROOT / "generated" / "lancedb"))
    table = db.open_table(TABLE)
    embed = SentenceTransformer(EMBED_MODEL)
    return table, embed


@st.cache_resource(show_spinner=False)
def build_nr_to_pdf() -> dict[str, Path]:
    """Bygg nr → PDF-sökväg från filsystemet (ocr/ föredras framför files/)."""
    mapping: dict[str, Path] = {}
    # generated/ocr/ skriver över downloaded/files/ och downloaded/wpu_files/ → föredras (har OCR-textlager)
    for d in ("downloaded/files", "downloaded/wpu_files", "generated/ocr"):
        folder = ROOT / d
        if not folder.is_dir():
            continue
        for pdf in folder.glob("*.pdf"):
            nr = pdf.stem.split(" — ")[0].strip()
            if nr:
                mapping[nr] = pdf
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


# Nr kan vara digitalt med valfritt antal led av "." eller "," (t.ex. 281,10 eller 1322.7).
CITE_RE = re.compile(r"Nr ([\w\-]+(?:[.,][\w\-]+)*),\s*sida (\d+)")


def extract_cited_sources(answer: str) -> list[dict]:
    """Bygg källlista ur ett MCP-svar genom att parsa unika Nr-citat."""
    nr_to_pdf = build_nr_to_pdf()
    seen: dict[str, dict] = {}
    for m in CITE_RE.finditer(answer):
        nr = m.group(1)
        if nr in seen or nr not in nr_to_pdf:
            continue
        pdf = nr_to_pdf[nr]
        parts = [p.strip() for p in pdf.stem.split(" — ")]
        seen[nr] = {
            "source": pdf.stem + ".txt",
            "page": None,
            "nr": nr,
            "titel": parts[1] if len(parts) > 1 else pdf.stem,
        }
    return list(seen.values())


def linkify_citations(text: str) -> str:
    """Förvandla "Nr X, sida Y" till små inline-knappar som öppnar PDF lokalt
    via ?pdf=<base64>-handlern högst upp i scriptet (oberoende av session_state).
    Fungerar i både RAG- och MCP-läge — slår upp nr → PDF direkt i filsystemet.
    """
    nr_to_pdf = build_nr_to_pdf()

    style = (
        "display:inline-block;padding:1px 6px;margin:0 2px;"
        "border:1px solid rgba(128,128,128,0.4);border-radius:6px;"
        "font-size:0.82em;background:rgba(128,128,128,0.12);"
        "color:inherit;text-decoration:none;"
        "font-family:ui-monospace,SFMono-Regular,monospace;"
    )

    def repl(m: re.Match) -> str:
        nr, page = m.group(1), m.group(2)
        if nr not in nr_to_pdf:
            return m.group(0)
        token = (
            base64.urlsafe_b64encode(str(nr_to_pdf[nr]).encode()).decode().rstrip("=")
        )
        href = f"?pdf={token}"
        return (
            f'<a href="{href}" target="pdf_opener" '
            f'style="{style}" title="Öppna PDF">{m.group(0)}</a>'
        )

    return CITE_RE.sub(repl, text)


table, embed_model = load()
st.caption(f"Index: {table.count_rows():,} chunks")

# Gömd iframe: citatlänkar har target="pdf_opener" och laddas hit istället för
# i huvudsidan. Streamlit serverar requesten i iframen, kör pdf-handlern högst
# upp i scriptet (öppnar PDF via subprocess) — huvudsidan rörs inte.
st.markdown(
    '<iframe name="pdf_opener" style="display:none" '
    'width="0" height="0" tabindex="-1" aria-hidden="true"></iframe>',
    unsafe_allow_html=True,
)

BACKENDS = {
    "Claude Opus 4.7": {"kind": "claude", "model": CLAUDE_MODEL},
    "OpenAI GPT-5": {
        "kind": "openai",
        "model": "gpt-5",
        "base_url": "https://api.openai.com/v1",
        "env": "OPENAI_API_KEY",
    },
    "OpenAI GPT-4o": {
        "kind": "openai",
        "model": "gpt-4o",
        "base_url": "https://api.openai.com/v1",
        "env": "OPENAI_API_KEY",
    },
    "DeepSeek V4": {
        "kind": "openai",
        "model": "deepseek-chat",
        "base_url": "https://api.deepseek.com/v1",
        "env": "DEEPSEEK_API_KEY",
    },
    "DeepSeek Reasoner": {
        "kind": "openai",
        "model": "deepseek-reasoner",
        "base_url": "https://api.deepseek.com/v1",
        "env": "DEEPSEEK_API_KEY",
    },
    "OpenAI-kompatibel (custom)": {
        "kind": "openai",
        "model": "llama3.1:8b",
        "base_url": "http://localhost:1234/v1",
        "env": None,
        "configurable": True,
    },
}

OPENAI_TOOLS = [
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

st.session_state.setdefault("mcp_mode", False)
st.session_state.setdefault("do_rerank", True)


def _on_mcp_change() -> None:
    st.session_state.do_rerank = not st.session_state.get("mcp_mode")


def _on_rerank_change() -> None:
    if st.session_state.get("mcp_mode"):
        st.session_state.mcp_mode = False
        st.session_state.do_rerank = True


with st.sidebar:
    st.header("Inställningar")
    backend_name = st.selectbox("AI-modell", list(BACKENDS.keys()), index=0)
    backend = BACKENDS[backend_name]
    if backend.get("configurable"):
        backend = {
            **backend,
            "base_url": st.text_input(
                "Endpoint-URL",
                value=backend["base_url"],
                help="OpenAI-kompatibel /v1-endpoint (Ollama, LM Studio, "
                "llama.cpp, vLLM, fjärr-OpenAI-API, ...)",
            ),
            "model": st.text_input(
                "Modellnamn",
                value=backend["model"],
                help="T.ex. `llama3.1:8b` (Ollama), `gpt-4o-mini`, eller "
                "vad providern kräver",
            ),
            "api_key_override": st.text_input(
                "API-nyckel (valfritt)",
                value="",
                type="password",
                help="Lämna tomt för Ollama/lokala servrar utan auth",
            ),
        }
    mcp_mode = st.toggle(
        "Utredningsläge (MCP)",
        key="mcp_mode",
        help="Modellen söker autonomt med egna verktyg — bättre på komplexa frågor, men långsammare. "
        "I detta läge får du en chatt där modellen minns tidigare frågor.",
        disabled=backend["kind"] not in ("claude", "openai"),
        on_change=_on_mcp_change,
    )
    if mcp_mode and st.button("Ny konversation", use_container_width=True):
        st.session_state.chat_history = []
        st.session_state.mcp_session_id = None
        st.session_state.openai_chat_messages = []
        st.rerun()
    do_rerank = st.toggle(
        "Använd cross-encoder reranker",
        key="do_rerank",
        help="Långsammare första gången (laddar ~568 MB) men bättre precision. "
        "Klicka för att byta till RAG-läget när utredningsläget är aktivt.",
        on_change=_on_rerank_change,
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

# Klick på inline-citatknapp i svaret: ?pdf=<base64-encoded path> → öppna PDF.
# PDF-sökvägen kodas direkt i URL:en så det fungerar även om session_state
# försvinner vid full sidladdning. Validerar att path ligger under ROOT.
qp = st.query_params
if "pdf" in qp:
    try:
        token = qp["pdf"]
        token += "=" * (-len(token) % 4)
        path = Path(base64.urlsafe_b64decode(token).decode()).resolve()
        # is_relative_to följer den fullt resolvade strängen — slipper symlink-fel
        # där path.parents kan innehålla ROOT trots att resolve() pekar utanför.
        if (
            path.is_file()
            and path.suffix.lower() == ".pdf"
            and path.is_relative_to(ROOT.resolve())
        ):
            subprocess.Popen(["open", str(path)])
    except (ValueError, OSError):
        pass
    st.query_params.clear()


async def stream_mcp(
    q: str, placeholder, parts: list[str], resume_id: str | None
) -> str | None:
    """Utredningsläge: Claude anropar search_archive/get_page autonomt.

    Returnerar Claudes session_id så att nästa fråga kan resume:a samma
    konversation (Claude minns tidigare frågor och tool-resultat)."""
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
        model=CLAUDE_MODEL,
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
    async for message in query(prompt=q, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    parts.append(block.text)
                    placeholder.markdown("".join(parts))
        elif isinstance(message, ResultMessage):
            new_session_id = message.session_id
    return new_session_id


async def stream_claude(user_msg: str, placeholder, parts: list[str]) -> None:
    options = ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        model=CLAUDE_MODEL,
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


async def stream_openai(user_msg: str, placeholder, parts: list[str], cfg) -> None:
    from openai import AsyncOpenAI  # noqa: PLC0415

    api_key = os.environ.get(cfg["env"]) if cfg.get("env") else "ollama"
    client = AsyncOpenAI(api_key=api_key or "ollama", base_url=cfg["base_url"])
    stream = await client.chat.completions.create(
        model=cfg["model"],
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        stream=True,
    )
    async for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta.content
        if delta is None:
            continue
        parts.append(delta)
        placeholder.markdown("".join(parts))
        if chunk.choices[0].finish_reason == "length":
            parts.append("\n\n*[svar avklippt — öka kontextgränsen]*")
            placeholder.markdown("".join(parts))


async def stream_to_string(hits, q, cfg) -> str:
    user_msg = f"Utdrag ur arkivet:\n\n{format_context(hits)}\n\n---\n\nFråga: {q}"
    placeholder = st.empty()
    parts: list[str] = []
    if cfg["kind"] == "claude":
        await stream_claude(user_msg, placeholder, parts)
    else:
        await stream_openai(user_msg, placeholder, parts, cfg)
    final = linkify_citations("".join(parts))
    placeholder.markdown(final, unsafe_allow_html=True)
    return final


async def stream_mcp_to_string(q: str, resume_id: str | None) -> tuple[str, str | None]:
    placeholder = st.empty()
    parts: list[str] = []
    new_id = await stream_mcp(q, placeholder, parts, resume_id)
    final = linkify_citations("".join(parts))
    placeholder.markdown(final, unsafe_allow_html=True)
    return final, new_id


def _run_tool(name: str, arguments: dict) -> str:
    import mcp_server  # type: ignore  # noqa: PLC0415

    mcp_server._table = table
    mcp_server._model = embed_model
    if name == "search_archive":
        return mcp_server.search_archive(**arguments)
    if name == "get_page":
        return mcp_server.get_page(**arguments)
    return f"Okänt verktyg: {name}"


async def stream_openai_mcp(
    status_box,
    text_placeholder,
    parts: list[str],
    cfg: dict,
    messages: list[dict],
) -> None:
    """Utredningsläge för OpenAI-kompatibla backends.

    messages innehåller redan user-meddelandet. Assistentens svar och
    tool-resultat appendas direkt till messages (konversationshistorik).
    """
    import json  # noqa: PLC0415

    from openai import AsyncOpenAI  # noqa: PLC0415

    api_key = cfg.get("api_key_override") or (
        os.environ.get(cfg["env"]) if cfg.get("env") else "ollama"
    )
    client = AsyncOpenAI(api_key=api_key or "ollama", base_url=cfg["base_url"])

    tool_count = 0
    try:
        for _turn in range(10):
            response = await client.chat.completions.create(
                model=cfg["model"],
                messages=messages,
                tools=OPENAI_TOOLS,
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
                parts.append(final)
                text_placeholder.markdown(final)
                messages.append({"role": "assistant", "content": final})
                break
        else:
            msg = "*[Svar avklippt — modellen nådde gränsen för antal verktygsanrop.]*"
            parts.append(msg)
            text_placeholder.markdown(msg)
            messages.append({"role": "assistant", "content": msg})
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


async def stream_openai_mcp_to_string(cfg: dict, messages: list[dict]) -> str:
    status_box = st.status("Söker i arkivet…", expanded=True)
    text_placeholder = st.empty()
    parts: list[str] = []
    await stream_openai_mcp(status_box, text_placeholder, parts, cfg, messages)
    final = linkify_citations("".join(parts))
    text_placeholder.markdown(final, unsafe_allow_html=True)
    return final


if mcp_mode:
    if backend["kind"] == "claude":
        # Chatt-läge: rendera historiken först, sedan st.chat_input nederst.
        for turn_idx, turn in enumerate(ss.chat_history):
            with st.chat_message(turn["role"]):
                st.markdown(turn["text"], unsafe_allow_html=True)
                srcs = turn.get("sources") or []
                if srcs:
                    with st.expander(f"Källor ({len(srcs)})", expanded=False):
                        for i, h in enumerate(srcs):
                            pdf = find_pdf(h["source"])
                            stem = (
                                h["source"][:-4]
                                if h["source"].endswith(".txt")
                                else h["source"]
                            )
                            with st.container(border=True):
                                cols = st.columns([5, 2])
                                with cols[0]:
                                    st.markdown(f"**{stem}**")
                                with cols[1]:
                                    if pdf and st.button(
                                        "Öppna PDF",
                                        key=f"chat_pdf_{turn_idx}_{i}",
                                        use_container_width=True,
                                    ):
                                        subprocess.Popen(["open", str(pdf)])

        chat_q = st.chat_input("Ställ en fråga till utredningsassistenten…")
        if chat_q and chat_q.strip():
            ss.chat_history.append({"role": "user", "text": chat_q, "sources": []})
            with st.chat_message("user"):
                st.markdown(chat_q)
            with st.chat_message("assistant"):
                answer, new_id = asyncio.run(
                    stream_mcp_to_string(chat_q, ss.mcp_session_id)
                )
            ss.mcp_session_id = new_id
            ss.chat_history.append(
                {
                    "role": "assistant",
                    "text": answer,
                    "sources": extract_cited_sources(answer),
                }
            )
            st.rerun()
    else:
        for turn_idx, turn in enumerate(ss.chat_history):
            with st.chat_message(turn["role"]):
                st.markdown(turn["text"], unsafe_allow_html=True)
                srcs = turn.get("sources") or []
                if srcs:
                    with st.expander(f"Källor ({len(srcs)})", expanded=False):
                        for i, h in enumerate(srcs):
                            pdf = find_pdf(h["source"])
                            stem = (
                                h["source"][:-4]
                                if h["source"].endswith(".txt")
                                else h["source"]
                            )
                            with st.container(border=True):
                                cols = st.columns([5, 2])
                                with cols[0]:
                                    st.markdown(f"**{stem}**")
                                with cols[1]:
                                    if pdf and st.button(
                                        "Öppna PDF",
                                        key=f"chat_pdf_{turn_idx}_{i}",
                                        use_container_width=True,
                                    ):
                                        subprocess.Popen(["open", str(pdf)])

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
            ss.chat_history.append(
                {
                    "role": "assistant",
                    "text": answer,
                    "sources": extract_cited_sources(answer),
                }
            )
            st.rerun()
else:
    with st.form("ask"):
        q = st.text_input("Din fråga", placeholder="Vem är Stig Engström?")
        submitted = st.form_submit_button("Fråga", type="primary")

    if submitted and q.strip():
        ss.question = q
        with st.status("Söker i indexet…", expanded=False) as status:
            hits = search(table, embed_model, q, top_k)
            if not hits:
                status.update(label="Inga träffar", state="error")
                ss.hits, ss.answer = None, ""
                st.stop()
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
        ss.answer = asyncio.run(stream_to_string(hits, q, backend))

# Rendera resultat från session_state (även efter rerun från PDF-knappar).
# Bara i RAG-läget — MCP-chatten renderar sina källor inline per tur.
if ss.hits and not mcp_mode:
    if not (submitted and q.strip()):
        # på rerun: visa cachat svar (redan linkifierat)
        st.subheader("Svar")
        st.markdown(ss.answer, unsafe_allow_html=True)

    with st.expander(f"Källor ({len(ss.hits)})", expanded=False):
        for i, h in enumerate(ss.hits):
            pdf = find_pdf(h["source"])
            txt = find_txt(h["source"])
            stem = h["source"][:-4] if h["source"].endswith(".txt") else h["source"]
            with st.container(border=True):
                cols = st.columns([5, 2, 2])
                with cols[0]:
                    st.markdown(f"**{stem}**")
                    if h.get("page"):
                        st.caption(f"sida {h['page']}")
                with cols[1]:
                    if pdf and st.button(
                        "Öppna PDF", key=f"open_pdf_{i}", use_container_width=True
                    ):
                        subprocess.Popen(["open", str(pdf)])
                with cols[2]:
                    if txt and st.button(
                        "Öppna text", key=f"open_txt_{i}", use_container_width=True
                    ):
                        subprocess.Popen(["open", str(txt)])
