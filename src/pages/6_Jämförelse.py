"""Vittnesjämförelse — låt AI lyfta fram var källorna säger emot varandra.

Ett "korsförhörsläge": ange ett ämne, hämta flera källor och låt språkmodellen
ställa dem mot varandra i stället för att syntetisera bort konflikterna. Följer
samma backend-val som Utredning (sparas i generated/llm_config.json).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import lancedb
import streamlit as st
from sentence_transformers import SentenceTransformer

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "rag"))

from ask import CLAUDE_MODEL, EMBED_MODEL, TABLE, rerank, search  # noqa: E402
from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ClaudeAgentOptions,
    TextBlock,
    ThinkingConfigAdaptive,
    query,
)

import backends as _backends  # noqa: E402
import casebook_ui as _casebook_ui  # noqa: E402
import citations as _citations  # noqa: E402
import compare as _compare  # noqa: E402
import config as _llm_config  # noqa: E402

st.set_page_config(page_title="Palmemordsarkivet — Jämförelse", layout="wide")
st.title("Vittnesjämförelse")
st.caption("Ställ källorna mot varandra och se var de säger emot varandra.")
_casebook_ui.render_pdf_opener(ROOT)

conn = _casebook_ui.state_conn()


@st.cache_resource(show_spinner="Laddar embedding-modell…")
def _load():
    db = lancedb.connect(str(ROOT / "generated" / "lancedb"))
    return db.open_table(TABLE), SentenceTransformer(EMBED_MODEL)


@st.cache_resource(show_spinner=False)
def _nr_to_pdf():
    return _citations.build_nr_to_pdf(ROOT)


def _resolve_backend() -> dict:
    """Plocka aktivt backend ur sparad llm-config (sätts i Utredning)."""
    saved = _llm_config.load()
    name = saved.get("backend_name", "Claude")
    base = _backends.BACKENDS.get(name, _backends.BACKENDS["Claude"])
    return {
        "name": name,
        "kind": base["kind"],
        "model": CLAUDE_MODEL if base["kind"] == "claude" else (saved.get("model") or base["model"]),
        "base_url": saved.get("base_url") or base.get("base_url", ""),
        "env": base.get("env"),
    }


async def _stream_claude(user_msg: str, placeholder, parts: list[str]) -> None:
    options = ClaudeAgentOptions(
        system_prompt=_compare.COMPARE_SYSTEM_PROMPT,
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


async def _stream_openai(user_msg: str, placeholder, parts: list[str], cfg: dict) -> None:
    from openai import AsyncOpenAI, NotFoundError, OpenAIError  # noqa: PLC0415

    api_key = (os.environ.get(cfg["env"]) if cfg.get("env") else None) or "ollama"
    base_url, model = cfg["base_url"], cfg["model"]
    try:
        async with AsyncOpenAI(api_key=api_key, base_url=base_url) as client:
            stream = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _compare.COMPARE_SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                stream=True,
            )
            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta.content
                if delta:
                    parts.append(delta)
                    placeholder.markdown("".join(parts))
    except NotFoundError as exc:
        hint = (f"\n\n**Tips:** Kontrollera att endpoint-URL:en (`{base_url}`) och "
                f"modellnamnet (`{model}`) stämmer. Ollamas standard är "
                "`http://localhost:11434/v1`.")
        st.error(f"404 från {base_url}: {exc}{hint}")
    except OpenAIError as exc:
        st.error(f"Fel vid anrop till {model}: {exc}")


async def _run_compare(topic: str, hits: list[dict], cfg: dict, placeholder) -> str:
    user_msg = _compare.build_compare_prompt(topic, hits)
    parts: list[str] = []
    if cfg["kind"] == "claude":
        await _stream_claude(user_msg, placeholder, parts)
    else:
        await _stream_openai(user_msg, placeholder, parts, cfg)
    known = {h["source"] for h in hits}
    final = _citations.linkify_citations("".join(parts), _nr_to_pdf(), known_sources=known)
    placeholder.markdown(final, unsafe_allow_html=True)
    return final


backend = _resolve_backend()
with st.sidebar:
    st.header("Inställningar")
    st.caption(f"AI-modell: **{backend['name']}** ({backend['model']})")
    st.caption("Byt modell i Utredning-sidans sidofält.")
    top_k = st.slider("Hämta top-K kandidater", 5, 50, 25)
    top_n = st.slider("Jämför top-N källor", 2, 15, 8)
    do_rerank = st.toggle("Använd cross-encoder reranker", value=True)

if backend["kind"] == "claude":
    if not (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")):
        st.error("Sätt `CLAUDE_CODE_OAUTH_TOKEN` eller `ANTHROPIC_API_KEY` i miljön.")
        st.stop()
elif backend.get("env") and not os.environ.get(backend["env"]):
    st.error(f"Sätt `{backend['env']}` i miljön för att använda {backend['name']}.")
    st.stop()

table, embed_model = _load()

with st.form("compare"):
    topic = st.text_input(
        "Ämne att jämföra källorna kring",
        placeholder="Vart sprang gärningsmannen efter skotten?",
    )
    submitted = st.form_submit_button("Jämför källor", type="primary")

if submitted and topic.strip():
    with st.status("Söker i indexet…", expanded=False) as status:
        hits = search(table, embed_model, topic, top_k)
        if not hits:
            status.update(label="Inga träffar", state="error")
            st.stop()
        if do_rerank:
            status.update(label="Omrankar med cross-encoder…")
            hits = rerank(topic, hits, top_n)
        else:
            hits = hits[:top_n]
        status.update(label=f"Jämför {len(hits)} utdrag", state="complete")

    groups = _compare.group_hits_by_source(hits)
    st.caption(f"Jämför {len(hits)} utdrag ur {len(groups)} källor.")

    st.subheader(f"Jämförelse ({backend['name']})")
    placeholder = st.empty()
    answer = asyncio.run(_run_compare(topic, hits, backend, placeholder))

    with st.expander(f"Källor ({len(hits)})", expanded=False):
        _casebook_ui.render_source_cards(ROOT, hits, conn, key_prefix="compare_source")

    _casebook_ui.render_casebook_save(
        conn,
        question=f"Jämförelse: {topic}",
        answer=answer,
        mode="jämförelse",
        backend_name=backend["name"],
        model=backend["model"],
        sources=hits,
        centers=[],
        key="compare_save",
    )
