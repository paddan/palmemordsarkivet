"""Enkelt webgränssnitt för palmemordsarkivet via Streamlit.

Kör med:
    ./web.sh
eller manuellt:
    .venv/bin/streamlit run webui.py
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import urllib.parse
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

ROOT = Path(__file__).resolve().parent

st.set_page_config(page_title="Palmemordsarkivet", layout="wide")
st.title("Palmemordsarkivet")
st.caption("Fråga arkivet — sökning + Claude Opus 4.7 med källhänvisningar.")


@st.cache_resource(show_spinner="Laddar embedding-modell…")
def load():
    db = lancedb.connect("rag/lancedb")
    table = db.open_table(TABLE)
    embed = SentenceTransformer(EMBED_MODEL)
    return table, embed


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


def linkify_citations(text: str, hits) -> str:
    """Förvandla varje "Nr X, sida Y" till en klickbar HTML-länk.

    Kodar PDF-stem direkt i URL:en så att klick fungerar även om sessionen
    återställs (t.ex. nytt fönster). Använder target=_self så Streamlit
    kan rerun:a samma flik och hantera klicket.
    """
    nr_to_stem = {}
    for h in hits:
        stem = h["source"][:-4] if h["source"].endswith(".txt") else h["source"]
        nr_to_stem[str(h["nr"])] = stem

    def repl(m: re.Match) -> str:
        nr, page = m.group(1), m.group(2)
        if nr not in nr_to_stem:
            return m.group(0)
        stem_q = urllib.parse.quote(nr_to_stem[nr])
        href = f"?stem={stem_q}&page={page}"
        return f'<a href="{href}" target="_self">{m.group(0)}</a>'

    return CITE_RE.sub(repl, text)


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

# Cacha senaste sökning + svar i session_state så att klick på "Öppna PDF"
# (som triggar streamlit-rerun) inte tvingar fram en ny Claude-anrop.
ss = st.session_state
ss.setdefault("question", "")
ss.setdefault("hits", None)
ss.setdefault("answer", "")

# Hantera klick på citat-länkar (?stem=...&page=Y) — öppna PDF lokalt.
qp = st.query_params
if "stem" in qp:
    pdf = find_pdf(qp["stem"] + ".txt")
    if pdf:
        subprocess.Popen(["open", str(pdf)])
    st.query_params.clear()

with st.form("ask"):
    q = st.text_input("Din fråga", placeholder="Vem är Stig Engström?",
                      value=ss.question)
    submitted = st.form_submit_button("Fråga", type="primary")


async def stream_to_string(hits, q) -> str:
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
    placeholder = st.empty()
    parts: list[str] = []
    async for message in query(prompt=user_msg, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    parts.append(block.text)
                    placeholder.markdown("".join(parts))
    final = linkify_citations("".join(parts), hits)
    placeholder.markdown(final, unsafe_allow_html=True)
    return final


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
        status.update(label=f"Hittade {len(hits)} relevanta chunks", state="complete")
    ss.hits = hits

    st.subheader("Svar")
    ss.answer = asyncio.run(stream_to_string(hits, q))

# Rendera resultat från session_state (även efter rerun från PDF-knappar)
if ss.hits:
    if not submitted:
        # på rerun: visa cachat svar
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
