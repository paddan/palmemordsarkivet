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
    SYSTEM_PROMPT,
    TABLE,
    format_context,
    rerank,
    search,
)
from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ClaudeAgentOptions,
    TextBlock,
    ThinkingConfigAdaptive,
    query,
)

ROOT = Path(__file__).resolve().parents[1]

st.set_page_config(page_title="Palmemordsarkivet", layout="wide")
st.title("Palmemordsarkivet")
st.caption("Fråga arkivet — sökning + Claude Opus 4.7 med källhänvisningar.")


@st.cache_resource(show_spinner="Laddar embedding-modell…")
def load():
    db = lancedb.connect(str(ROOT / "rag" / "lancedb"))
    table = db.open_table(TABLE)
    embed = SentenceTransformer(EMBED_MODEL)
    return table, embed


@st.cache_resource(show_spinner=False)
def build_nr_to_pdf() -> dict[str, Path]:
    """Bygg nr → PDF-sökväg från filsystemet (ocr/ föredras framför files/)."""
    mapping: dict[str, Path] = {}
    for d in ("files", "ocr"):  # ocr/ skriver över files/ → föredras
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
    for d in ("ocr", "files"):
        p = ROOT / d / f"{stem}.pdf"
        if p.is_file():
            return p
    return None


def find_txt(source_txt: str) -> Path | None:
    """Hitta extraherad textfil för en chunk."""
    stem = source_txt[:-4] if source_txt.endswith(".txt") else source_txt
    p = ROOT / "text" / f"{stem}.txt"
    return p if p.is_file() else None


# Nr kan vara digitalt med valfritt antal led av "." eller "," (t.ex. 281,10 eller 1322.7).
CITE_RE = re.compile(r"Nr (\d+(?:[.,]\d+)*),\s*sida (\d+)")


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
        token = base64.urlsafe_b64encode(str(nr_to_pdf[nr]).encode()).decode().rstrip("=")
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
    "OpenAI GPT-5": {"kind": "openai", "model": "gpt-5",
                     "base_url": "https://api.openai.com/v1",
                     "env": "OPENAI_API_KEY"},
    "OpenAI GPT-4o": {"kind": "openai", "model": "gpt-4o",
                      "base_url": "https://api.openai.com/v1",
                      "env": "OPENAI_API_KEY"},
    "DeepSeek V4": {"kind": "openai", "model": "deepseek-chat",
                    "base_url": "https://api.deepseek.com/v1",
                    "env": "DEEPSEEK_API_KEY"},
    "DeepSeek Reasoner": {"kind": "openai", "model": "deepseek-reasoner",
                          "base_url": "https://api.deepseek.com/v1",
                          "env": "DEEPSEEK_API_KEY"},
    "OpenAI-kompatibel (custom)": {"kind": "openai", "model": "llama3.1:8b",
                                   "base_url": "http://localhost:11434/v1",
                                   "env": None, "configurable": True},
}

with st.sidebar:
    st.header("Inställningar")
    backend_name = st.selectbox("AI-modell", list(BACKENDS.keys()), index=0)
    backend = BACKENDS[backend_name]
    if backend.get("configurable"):
        backend = {
            **backend,
            "base_url": st.text_input(
                "Endpoint-URL", value=backend["base_url"],
                help="OpenAI-kompatibel /v1-endpoint (Ollama, LM Studio, "
                     "llama.cpp, vLLM, fjärr-OpenAI-API, ...)"),
            "model": st.text_input(
                "Modellnamn", value=backend["model"],
                help="T.ex. `llama3.1:8b` (Ollama), `gpt-4o-mini`, eller "
                     "vad providern kräver"),
            "api_key_override": st.text_input(
                "API-nyckel (valfritt)", value="", type="password",
                help="Lämna tomt för Ollama/lokala servrar utan auth"),
        }
    mcp_mode = st.toggle(
        "Utredningsläge (MCP)",
        value=False,
        help="Claude söker autonomt med egna verktyg — bättre på komplexa frågor, men långsammare.",
        disabled=backend["kind"] != "claude",
    )
    do_rerank = st.toggle("Använd cross-encoder reranker", value=True,
                          help="Långsammare första gången (laddar ~568 MB) men bättre precision.",
                          disabled=mcp_mode)
    top_k = st.slider(
        "Hämta top-K kandidater", 5, 50, 20,
        help="Antal chunks som vektorsökningen plockar fram ur indexet i första "
             "steget. Högre K → fler alternativ för rerankern att välja bland "
             "(bättre täckning) men långsammare. Utan reranker används bara de "
             "första top-N av dessa.")
    top_n = st.slider(
        "Skicka top-N till AI", 1, 15, 6,
        help="Antal chunks (efter ev. reranking) som faktiskt skickas som "
             "kontext till språkmodellen. Högre N → mer underlag men längre "
             "prompt, högre kostnad och risk att modellen tappar fokus.")

if backend["kind"] == "claude":
    if not (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")):
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

# Klick på inline-citatknapp i svaret: ?pdf=<base64-encoded path> → öppna PDF.
# PDF-sökvägen kodas direkt i URL:en så det fungerar även om session_state
# försvinner vid full sidladdning. Validerar att path ligger under ROOT.
qp = st.query_params
if "pdf" in qp:
    try:
        token = qp["pdf"]
        token += "=" * (-len(token) % 4)
        path = Path(base64.urlsafe_b64decode(token).decode()).resolve()
        if (
            path.is_file()
            and path.suffix.lower() == ".pdf"
            and ROOT in path.parents
        ):
            subprocess.Popen(["open", str(path)])
    except Exception:
        pass
    st.query_params.clear()


with st.form("ask"):
    q = st.text_input("Din fråga", placeholder="Vem är Stig Engström?",
                      value=ss.question)
    submitted = st.form_submit_button("Fråga", type="primary")


async def stream_mcp(q: str, placeholder, parts: list[str]) -> None:
    """Utredningsläge: Claude anropar search_archive/get_page autonomt."""
    db_dir = ROOT / "rag" / "lancedb"
    env = {
        "DB_DIR": str(db_dir),
        **{k: v for k, v in os.environ.items()
           if k in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY",
                    "PATH", "HOME", "VIRTUAL_ENV", "EMBED_MODEL")},
    }
    from ask import MCP_SYSTEM_PROMPT  # type: ignore
    options = ClaudeAgentOptions(
        system_prompt=MCP_SYSTEM_PROMPT,
        model=CLAUDE_MODEL,
        mcp_servers={"arkiv": {"command": sys.executable,
                                "args": [str(MCP_SERVER)], "env": env}},
        allowed_tools=["mcp__arkiv__search_archive", "mcp__arkiv__get_page"],
        thinking=ThinkingConfigAdaptive(type="adaptive"),
        effort="high",
        max_turns=10,
        setting_sources=[],
    )
    async for message in query(prompt=q, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    parts.append(block.text)
                    placeholder.markdown("".join(parts))


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


async def stream_mcp_to_string(q: str) -> str:
    placeholder = st.empty()
    parts: list[str] = []
    await stream_mcp(q, placeholder, parts)
    final = linkify_citations("".join(parts))
    placeholder.markdown(final, unsafe_allow_html=True)
    return final


if submitted and q.strip():
    ss.question = q

    if mcp_mode and backend["kind"] == "claude":
        st.subheader("Svar (utredningsläge)")
        ss.hits = []
        ss.answer = asyncio.run(stream_mcp_to_string(q))
    else:
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
            status.update(label=f"Hittade {len(hits)} relevanta chunks", state="complete")
        ss.hits = hits

        st.subheader(f"Svar ({backend_name})")
        ss.answer = asyncio.run(stream_to_string(hits, q, backend))

# Rendera resultat från session_state (även efter rerun från PDF-knappar)
if ss.hits:
    if not submitted:
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
                    st.caption(f"sida {h['page']}")
                with cols[1]:
                    if pdf and st.button("Öppna PDF", key=f"open_pdf_{i}",
                                         use_container_width=True):
                        subprocess.Popen(["open", str(pdf)])
                with cols[2]:
                    if txt and st.button("Öppna text", key=f"open_txt_{i}",
                                         use_container_width=True):
                        subprocess.Popen(["open", str(txt)])
