"""Streamlit-sida för att bläddra, söka och bokmärka källdokument."""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import archive_browser as _archive_browser  # noqa: E402
import casebook_ui as _casebook_ui  # noqa: E402


@st.cache_data(ttl=30, show_spinner=False)
def _load_documents(root: Path) -> list[_archive_browser.DocumentRecord]:
    return _archive_browser.iter_documents(root)


st.set_page_config(page_title="Palmemordsarkivet — Källor", layout="wide")
st.title("Källor")
st.caption("Bläddra, sök och bokmärk dokument utan att fråga AI först.")

conn = _casebook_ui.state_conn()
documents = _load_documents(ROOT)

query = st.text_input("Sök dokument", placeholder="Nr, titel eller filnamn")
limit = st.slider("Antal träffar", 10, 200, 50, step=10)
filtered = _archive_browser.filter_documents(documents, query)
st.caption(f"Visar {min(len(filtered), limit)} av {len(filtered)} träffar ({len(documents)} dokument totalt)")

if not documents:
    st.info("Inga OCR-textdokument hittades i generated/text.")
elif not filtered:
    st.info("Inga dokument matchar sökningen.")
else:
    for i, record in enumerate(filtered[:limit]):
        with st.container(border=True):
            st.subheader(record.title)
            st.caption(record.source)
            preview = _archive_browser.read_preview(record.text_path)
            if preview:
                st.write(preview)
            else:
                st.caption("Ingen textförhandsvisning finns.")

            _casebook_ui.render_source_cards(
                ROOT,
                [{
                    "source": record.source,
                    "page": None,
                    "nr": record.nr,
                    "title": record.title,
                }],
                conn,
                key_prefix=f"kallor_{i}",
            )
