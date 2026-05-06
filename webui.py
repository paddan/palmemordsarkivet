"""Enkelt webgränssnitt för palmemordsarkivet via Streamlit.

Kör med:
    ./web.sh
eller manuellt:
    .venv/bin/streamlit run webui.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

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

st.set_page_config(page_title="Palmemordsarkivet", layout="wide")
st.title("Palmemordsarkivet")
st.caption("Fråga arkivet — sökning + Claude Opus 4.7 med källhänvisningar.")


@st.cache_resource(show_spinner="Laddar embedding-modell…")
def load():
    db = lancedb.connect("rag/lancedb")
    table = db.open_table(TABLE)
    embed = SentenceTransformer(EMBED_MODEL)
    return table, embed


if not (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")):
    st.error(
        "Sätt `CLAUDE_CODE_OAUTH_TOKEN` (Pro/Max) eller `ANTHROPIC_API_KEY` i miljön "
        "innan du startar webgränssnittet."
    )
    st.stop()

table, embed_model = load()
st.caption(f"Index: {table.count_rows():,} chunks · modell: {CLAUDE_MODEL}")

with st.sidebar:
    st.header("Inställningar")
    do_rerank = st.toggle("Använd cross-encoder reranker", value=True,
                          help="Långsammare första gången (laddar ~568 MB) men bättre precision.")
    top_k = st.slider("Hämta top-K kandidater", 5, 50, 20)
    top_n = st.slider("Skicka top-N till Claude", 1, 15, 6)

q = st.text_input("Din fråga", placeholder="Vem är Stig Engström?")
go = st.button("Fråga", type="primary", disabled=not q.strip())

if go:
    with st.status("Söker i indexet…", expanded=False) as status:
        hits = search(table, embed_model, q, top_k)
        if not hits:
            status.update(label="Inga träffar", state="error")
            st.stop()
        if do_rerank:
            status.update(label="Omrankar med cross-encoder…")
            hits = rerank(q, hits, top_n)
        else:
            hits = hits[:top_n]
        status.update(label=f"Hittade {len(hits)} relevanta chunks", state="complete")

    with st.expander(f"Källor ({len(hits)})", expanded=False):
        for h in hits:
            st.markdown(f"**Nr {h['nr']}, sida {h['page']}** — {h['titel'][:80]}")
            text = h["text"]
            st.text(text[:400] + ("…" if len(text) > 400 else ""))

    st.subheader("Svar")

    async def stream_answer():
        user_msg = f"Utdrag ur arkivet:\n\n{format_context(hits)}\n\n---\n\nFråga: {q}"
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
                        yield block.text

    def sync_stream():
        loop = asyncio.new_event_loop()
        agen = stream_answer()
        try:
            while True:
                try:
                    yield loop.run_until_complete(agen.__anext__())
                except StopAsyncIteration:
                    break
        finally:
            loop.close()

    st.write_stream(sync_stream())
