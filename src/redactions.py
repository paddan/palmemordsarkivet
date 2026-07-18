"""Maskeringsutforskaren — hitta och visa svärtade (maskerade) partier.

OCR-pipelinen infogar ``[MASKAD]`` där redaktionsdetekteringen hittat svarta
maskeringsblock (se ocr_pages.py). Den här modulen aggregerar dessa markörer
ur ``pdf_pages`` i state.db så användaren kan se *vad som dolts* och var det är
som tätast. Ren logik utan Streamlit — sidan ``pages/5_Maskeringar.py`` ritar.
"""

from __future__ import annotations

import re
import sqlite3
from html import escape
from urllib.parse import quote

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


def document_label(doc: dict) -> str:
    """Etikett för dokumentval i UI:t."""
    pages = int(doc.get("pages_with_redactions") or 0)
    page_word = "sida" if pages == 1 else "sidor"
    return (
        f"{doc.get('pdf_stem', '')} — {int(doc.get('redactions') or 0)} "
        f"maskeringar på {pages} {page_word}"
    )


def documents_table_html(
    docs: list[dict], *, selected_stem: str | None = None, page_path: str = "/Maskeringar"
) -> str:
    """HTML-tabell där varje dokumentrad är klickbar utan separat valkontroll."""
    rows = []
    for doc in docs:
        stem = str(doc.get("pdf_stem") or "")
        selected = " redaction-selected" if stem == selected_stem else ""
        href = f"{page_path}?redaction_doc={quote(stem, safe='')}#maskeringar-detalj"
        doc_link = (
            f'<a class="redaction-cell-link" href="{href}" target="_self">'
            f"{escape(stem)}</a>"
        )
        redactions_link = (
            f'<a class="redaction-cell-link redaction-number" href="{href}" '
            f'target="_self">{int(doc.get("redactions") or 0):,}</a>'
        )
        pages_link = (
            f'<a class="redaction-cell-link redaction-number" href="{href}" '
            f'target="_self">{int(doc.get("pages_with_redactions") or 0):,}</a>'
        )
        rows.append(
            f'<tr class="redaction-row{selected}">'
            f"<td>{doc_link}</td>"
            f"<td>{redactions_link}</td>"
            f"<td>{pages_link}</td>"
            "</tr>"
        )
    return (
        "<style>"
        ".redaction-table-wrap{border:1px solid rgba(128,128,128,.32);border-radius:6px;"
        "overflow:auto;max-height:430px;margin:.25rem 0 1rem 0}"
        ".redaction-table{width:100%;border-collapse:collapse;table-layout:fixed}"
        ".redaction-table th{padding:.45rem .65rem}"
        ".redaction-table td{padding:0}"
        ".redaction-table th,.redaction-table td{"
        "border-bottom:1px solid rgba(128,128,128,.18);vertical-align:middle}"
        ".redaction-table th{position:sticky;top:0;z-index:1;font-weight:600;"
        "text-align:left;background:rgba(128,128,128,.18);"
        "border-bottom:1px solid rgba(128,128,128,.32)}"
        ".redaction-table th:nth-child(1),.redaction-table td:nth-child(1){width:auto}"
        ".redaction-table th:nth-child(2),.redaction-table td:nth-child(2){width:7rem;text-align:right}"
        ".redaction-table th:nth-child(3),.redaction-table td:nth-child(3){width:5rem;text-align:right}"
        ".redaction-row:hover{background:rgba(128,128,128,.12)}"
        ".redaction-selected{background:rgba(255,193,7,.18)}"
        ".redaction-cell-link{display:block;padding:.45rem .65rem;color:inherit;text-decoration:none;"
        "overflow:hidden;text-overflow:ellipsis;white-space:nowrap}"
        ".redaction-cell-link:hover{text-decoration:none}"
        ".redaction-number{text-align:right;font-variant-numeric:tabular-nums}"
        "</style>"
        '<div class="redaction-table-wrap">'
        '<table class="redaction-table" aria-label="Maskerade dokument">'
        "<thead><tr>"
        '<th scope="col">Dokument</th>'
        '<th scope="col">Maskeringar</th>'
        '<th scope="col">Sidor</th>'
        "</tr></thead>"
        "<tbody>"
        + "".join(rows)
        + "</tbody></table>"
        + "</div>"
    )


def filter_documents(docs: list[dict], query: str) -> list[dict]:
    """Filtrera maskeringsdokument på pdf_stem, case-insensitivt."""
    needle = query.strip().casefold()
    if not needle:
        return docs
    return [doc for doc in docs if needle in str(doc.get("pdf_stem") or "").casefold()]


def selected_document(docs: list[dict], pdf_stem: str | None) -> dict | None:
    """Returnera valt dokument eller None om inget matchar."""
    if not pdf_stem:
        return None
    return next((doc for doc in docs if doc.get("pdf_stem") == pdf_stem), None)


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
