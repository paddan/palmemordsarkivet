"""Streamlit-sida: manuell sökverkstad för retrieval-granskning."""

from __future__ import annotations

import sys
from pathlib import Path

import lancedb
import streamlit as st
from sentence_transformers import SentenceTransformer

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "rag"))

import casebook_ui as _casebook_ui  # noqa: E402
from ask import EMBED_MODEL, TABLE, rerank, search, search_hybrid  # type: ignore  # noqa: E402
from search_workbench import hit_excerpt, hit_key, hit_title  # noqa: E402

st.set_page_config(page_title="Palmemordsarkivet — Sökverkstad", layout="wide")
st.title("Sökverkstad")
st.caption("Granska träffar innan du låter AI formulera ett svar.")


@st.cache_resource(show_spinner="Laddar sökindex och embedding-modell...")
def load_index():
    """Ladda LanceDB-tabellen och embedding-modellen en gång per session."""
    db = lancedb.connect(str(ROOT / "generated" / "lancedb"))
    table = db.open_table(TABLE)
    embed = SentenceTransformer(EMBED_MODEL)
    return table, embed


casebook_conn = _casebook_ui.state_conn()

with st.sidebar:
    st.header("Sök")
    query = st.text_input("Sökfråga")
    do_hybrid = st.toggle("Hybrid/BM25", value=True)
    do_rerank = st.toggle("Reranka", value=True)
    top_k = st.slider("Top-K", 5, 50, 20)
    top_n = st.slider("Top-N", 1, 15, 8)
    run_search = st.button("Sök", type="primary", use_container_width=True)

try:
    table, embed_model = load_index()
except Exception as exc:  # noqa: BLE001
    st.error(
        "Kan inte öppna sökindexet. Kör `./ingest.sh` först och försök igen.\n\n"
        f"```\n{exc}\n```"
    )
    st.stop()

st.caption(f"Index: {table.count_rows():,} chunks")

if run_search:
    clean_query = query.strip()
    if not clean_query:
        st.warning("Skriv en sökfråga först.")
    else:
        with st.spinner("Söker i arkivet..."):
            if do_hybrid:
                hits = search_hybrid(table, embed_model, clean_query, top_k)
            else:
                hits = search(table, embed_model, clean_query, top_k)

            if do_rerank:
                hits = rerank(clean_query, hits, top_n)
            else:
                hits = hits[:top_n]

        st.session_state["search_workbench_hits"] = hits
        st.session_state["search_workbench_query"] = clean_query

hits = st.session_state.get("search_workbench_hits", [])
saved_query = st.session_state.get("search_workbench_query", "")

if not hits:
    st.info("Kör en sökning i sidofältet för att granska träffarna.")
    st.stop()

st.subheader(f"Träffar för: {saved_query}")
for i, hit in enumerate(hits):
    with st.container(border=True):
        st.markdown(f"#### {hit_title(hit)}")
        st.write(hit_excerpt(hit))
        _casebook_ui.render_source_cards(
            ROOT,
            [hit],
            casebook_conn,
            key_prefix=f"workbench_{i}_{hit_key(hit)}",
        )
