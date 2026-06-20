"""Maskeringsutforskaren — hitta och visa svärtade (maskerade) partier.

OCR-pipelinen infogar ``[MASKAD]`` där redaktionsdetekteringen hittat svarta
maskeringsblock (se ocr_pages.py). Den här modulen aggregerar dessa markörer
ur ``pdf_pages`` i state.db så användaren kan se *vad som dolts* och var det är
som tätast. Ren logik utan Streamlit — sidan ``pages/5_Maskeringar.py`` ritar.
"""

from __future__ import annotations

import re
import sqlite3

MASK_TOKEN = "[MASKAD]"
_MASK_RE = re.compile(re.escape(MASK_TOKEN))
_WS_RE = re.compile(r"\s+")


def count_redactions(text: str | None) -> int:
    """Antal ``[MASKAD]``-markörer i en text."""
    if not text:
        return 0
    return len(_MASK_RE.findall(text))


def redaction_snippets(text: str | None, *, window: int = 80) -> list[str]:
    """Utdrag med ``window`` tecken kontext på var sida om varje maskering."""
    if not text:
        return []
    snippets: list[str] = []
    for m in _MASK_RE.finditer(text):
        start = max(0, m.start() - window)
        end = min(len(text), m.end() + window)
        snippet = _WS_RE.sub(" ", text[start:end]).strip()
        if start > 0:
            snippet = "…" + snippet
        if end < len(text):
            snippet = snippet + "…"
        snippets.append(snippet)
    return snippets


def documents_by_redaction(conn: sqlite3.Connection) -> list[dict]:
    """Dokument med minst en maskering, mest maskerade först.

    Returnerar dictar med ``pdf_stem``, ``redactions`` (totalt antal) och
    ``pages_with_redactions``."""
    rows = conn.execute(
        "SELECT pdf_stem, page_num, text FROM pdf_pages WHERE text LIKE ?",
        (f"%{MASK_TOKEN}%",),
    )
    agg: dict[str, dict] = {}
    for row in rows:
        n = count_redactions(row["text"])
        if n == 0:
            continue
        entry = agg.setdefault(
            row["pdf_stem"],
            {"pdf_stem": row["pdf_stem"], "redactions": 0, "pages_with_redactions": 0},
        )
        entry["redactions"] += n
        entry["pages_with_redactions"] += 1
    return sorted(
        agg.values(),
        key=lambda d: (-d["redactions"], -d["pages_with_redactions"], d["pdf_stem"]),
    )


def page_redactions(
    conn: sqlite3.Connection, pdf_stem: str, *, window: int = 80
) -> list[dict]:
    """Per-sida maskeringar för ett dokument: ``page_num``, ``redactions``,
    ``snippets`` — bara sidor som faktiskt har minst en maskering."""
    rows = conn.execute(
        "SELECT page_num, text FROM pdf_pages WHERE pdf_stem=? ORDER BY page_num",
        (pdf_stem,),
    )
    out: list[dict] = []
    for row in rows:
        n = count_redactions(row["text"])
        if n == 0:
            continue
        out.append(
            {
                "page_num": row["page_num"],
                "redactions": n,
                "snippets": redaction_snippets(row["text"], window=window),
            }
        )
    return out
