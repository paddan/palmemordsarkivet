#!/usr/bin/env python3
"""MCP-server för Palmemordsarkivet.

Exponerar två verktyg som Claude kan anropa autonomt:
  - search_archive  — vektor- eller hybridsökning, valfri reranking
  - get_page        — hämta råtext från en specifik sida

Körs som subprocess av ask.py (--mcp) och Utredning.py (utredningsläge).
Startas normalt inte manuellt.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Annotated

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "palmemordsarkivet",
    instructions=(
        "Sök i Palmemordsarkivet med search_archive. "
        "Använd get_page för att läsa mer kontext kring en specifik sida. "
        "Citera alltid med [Nr X, sida Y]."
    ),
)

# ── Lazy-initierade globaler ──────────────────────────────────────────────────
_table = None
_model = None
_text_dir: Path = ROOT / "generated" / "text"


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


# ── Verktyg ───────────────────────────────────────────────────────────────────

@mcp.tool()
def search_archive(
    query: Annotated[str, "Sökfrågan på svenska"],
    top_k: Annotated[int, "Antal kandidater att hämta (5–50)"] = 20,
    top_n: Annotated[int, "Antal att behålla efter reranking (1–15)"] = 6,
    hybrid: Annotated[bool, "Kombinera vektor- och BM25-sökning"] = True,
    rerank: Annotated[bool, "Omranka med cross-encoder för bättre precision"] = True,
) -> str:
    """Sök i Palmemordsarkivet och returnera relevanta textutdrag med källhänvisningar.

    Använd detta verktyg för att hitta information i Palmemordsarkivet.
    Anropa flera gånger med olika söktermer för att täcka ett ämne från flera vinklar.
    """
    _init()
    from ask import format_context, search, search_hybrid

    hits = search_hybrid(_table, _model, query, top_k) if hybrid else search(_table, _model, query, top_k)
    if not hits:
        return "Inga träffar."

    if rerank:
        from ask import rerank as do_rerank
        hits = do_rerank(query, hits, top_n)
    else:
        hits = hits[:top_n]

    header = f"Sökning: {query!r} → {len(hits)} träffar\n\n"
    return header + format_context(hits)


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
    return f"[{source}, sida {page}]\n\n{text}"


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        mcp.run()
    except KeyboardInterrupt:
        # Stdio-server: avsluta tyst när föräldraprocessen stänger ned.
        pass
