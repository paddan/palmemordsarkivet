"""Maskeringsutforskaren — bläddra bland svärtade (maskerade) partier.

Listar dokument efter hur många ``[MASKAD]``-markörer OCR-pipelinens
redaktionsdetektering hittat, och visar kontexten först för klickad tabellrad.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import casebook_ui as _casebook_ui  # noqa: E402
import redactions as _redactions  # noqa: E402

st.set_page_config(page_title="Palmemordsarkivet — Maskeringar", layout="wide")
st.title("Maskeringar")
st.caption("Var har arkivet svärtat över text? Det som dolts är ofta lika "
           "intressant som innehållet.")

conn = _casebook_ui.state_conn()


@st.cache_data(ttl=300, show_spinner="Letar maskeringar…")
def _documents() -> list[dict]:
    # Cachas på innehåll via en enkel signatur; ttl räcker för en arbetssession.
    return _redactions.documents_by_redaction(conn)


docs = _documents()
if not docs:
    st.info("Inga maskeringar hittade. Kör pipelinen med redaktionsdetektering "
            "påslagen (standard) först.")
    st.stop()

total = sum(d["redactions"] for d in docs)
st.caption(f"{total:,} maskeringar i {len(docs):,} dokument.")

needle = st.text_input("Filtrera dokument", placeholder="del av filnamn/nr…")
shown = _redactions.filter_documents(docs, needle)
st.caption(f"Visar {len(shown):,} av {len(docs):,} dokument, mest maskerade först.")

if not shown:
    st.info("Inga dokument matchar filtret.")
    st.stop()

selected_stem = st.query_params.get("redaction_doc", "")
st.caption("Klicka på en dokumentrad för att visa källknappar och kontextutdrag.")
st.markdown(
    _redactions.documents_table_html(shown, selected_stem=selected_stem),
    unsafe_allow_html=True,
)

selected = _redactions.selected_document(shown, selected_stem)
if selected is None:
    st.info("Klicka på en dokumentrad i tabellen för att visa källknappar och kontextutdrag.")
    st.stop()

stem = selected["pdf_stem"]
st.markdown('<div id="maskeringar-detalj"></div>', unsafe_allow_html=True)
st.subheader(_redactions.document_label(selected))
source = {"source": f"{stem}.txt", "title": stem,
          "nr": stem.split(" — ")[0].strip()}
_casebook_ui.render_source_cards(
    ROOT, [source], conn, key_prefix=f"redact_card_{stem}"
)
for page in _redactions.page_redactions(conn, stem):
    st.markdown(f"**Sida {page['page_num']}** "
                f"({page['redactions']} maskeringar)")
    for snippet in page["snippets"]:
        st.markdown(f"> {snippet}")
