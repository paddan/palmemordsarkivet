"""Delade Streamlit-komponenter för utredningspärm och källbokmärken."""

from __future__ import annotations

import base64
import binascii
import json
import re
import subprocess
import webbrowser
from pathlib import Path

import streamlit as st

import citations as _citations
import db as _state_db


def state_conn():
    """Öppna state-db för UI-persistens och säkerställ senaste schemat."""
    conn = _state_db.connect()
    _state_db.init_schema(conn)
    return conn


def find_pdf(root: Path, source_txt: str) -> Path | None:
    """Hitta original-PDF för en chunk. Föredrar ocr/ (sökbar)."""
    stem = source_txt[:-4] if source_txt.endswith(".txt") else source_txt
    for d in ("generated/ocr", "downloaded/files", "downloaded/wpu_files"):
        p = root / d / f"{stem}.pdf"
        if p.is_file():
            return p
    return None


def find_txt(root: Path, source_txt: str) -> Path | None:
    """Hitta extraherad textfil för en chunk."""
    stem = source_txt[:-4] if source_txt.endswith(".txt") else source_txt
    p = root / "generated" / "text" / f"{stem}.txt"
    return p if p.is_file() else None


def resolve_pdf_query_path(root: Path, token: str | list[str] | tuple[str, ...]) -> Path | None:
    """Avkoda och validera en ``?pdf=``-token mot projektroten."""
    if isinstance(token, (list, tuple)):
        token = token[0] if token else ""
    try:
        raw = str(token)
        raw += "=" * (-len(raw) % 4)
        path = Path(base64.urlsafe_b64decode(raw).decode()).resolve()
        root = root.resolve()
    except (binascii.Error, OSError, UnicodeDecodeError, ValueError):
        return None
    if path.is_file() and path.suffix.lower() == ".pdf" and path.is_relative_to(root):
        return path
    return None


def resolve_pdf_query_page(value: str | list[str] | tuple[str, ...] | None) -> int | None:
    """Validera sidnummer från ``?page=``. Sidnummer är 1-baserade."""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if value is None:
        return None
    try:
        page = int(value)
    except (TypeError, ValueError):
        return None
    return page if page > 0 else None


def pdf_open_target(path: Path, page: int | str | None = None) -> str:
    """Webbläsar-URL för lokal PDF, med sidfragment när sida är känd."""
    target = path.resolve().as_uri()
    page_num = resolve_pdf_query_page(str(page)) if page is not None else None
    if page_num is None:
        return target
    return f"{target}#page={page_num}"


def source_bookmark_payload(source: dict) -> dict:
    """Normalisera RAG/MCP-källa till source_bookmarks-format."""
    raw_source = str(source.get("source") or "")
    stem = raw_source[:-4] if raw_source.endswith(".txt") else raw_source
    page = source.get("page")
    return {
        "source": raw_source,
        "page": int(page) if page else None,
        "nr": source.get("nr") or (stem.split(" — ")[0].strip() if stem else None),
        "title": source.get("titel") or source.get("title") or stem,
    }


def source_display_title(source: dict) -> str:
    """Människoläsbar källrubrik med sidnummer när det finns."""
    raw_source = str(source.get("source") or "")
    stem = raw_source[:-4] if raw_source.endswith(".txt") else raw_source
    title = str(source.get("title") or source.get("titel") or stem or "Källa")
    page = source.get("page")
    return f"{title}, sida {page}" if page else title


def casebook_entry_title(entry: dict, *, max_chars: int = 72) -> str:
    """Kort rubrik för en sparad fråga/svar-post."""
    raw = str(entry.get("note") or entry.get("question") or "Sparat svar")
    text = re.sub(r"\s+", " ", raw).strip()
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars - 3].rsplit(" ", 1)[0].rstrip(".,;: ")
    return f"{truncated or text[:max_chars - 3]}..."


def _graph_center_payload(entities: list[dict]) -> list[dict]:
    centers = []
    for entity in entities:
        namn = str(entity.get("namn") or "").strip()
        label = str(entity.get("label") or "").strip()
        norm = str(entity.get("norm") or "").strip()
        if namn and label and norm:
            centers.append({"namn": namn, "label": label, "norm": norm})
    return centers


def graph_centers_query_params(entities: list[dict]) -> dict[str, str]:
    """Koda sparade grafentiteter som query params till Graf-sidan."""
    centers = _graph_center_payload(entities)
    payload = json.dumps(centers, ensure_ascii=False, separators=(",", ":")).encode()
    token = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return {"centers": token}


def decode_graph_centers_param(
    value: str | list[str] | tuple[str, ...] | None,
) -> list[dict]:
    """Läs tillbaka centers-parametern från Graf-sidans query params."""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if not value:
        return []
    token = str(value)
    try:
        token += "=" * (-len(token) % 4)
        data = json.loads(base64.urlsafe_b64decode(token).decode())
    except (binascii.Error, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return _graph_center_payload([e for e in data if isinstance(e, dict)])


def bookmark_source_button(conn, source: dict, key: str) -> None:
    """Rendera knapp som sparar en källa som bokmärke."""
    payload = source_bookmark_payload(source)
    if not payload["source"]:
        return
    if st.button("Bokmärk", key=key, use_container_width=True):
        _state_db.record_source_bookmark(conn, **payload)
        st.toast("Källan är bokmärkt")
        st.rerun()


def open_pdf_in_browser(path: Path, page: int | str | None = None) -> None:
    """Öppna en lokal PDF i ny webbläsarflik så ``#page=N`` bevaras."""
    try:
        opened = webbrowser.open(pdf_open_target(path, page), new=2)
    except webbrowser.Error as e:
        st.error(f"Kan inte öppna PDF i webbläsare: {e}")
        return
    if not opened:
        st.error("Kan inte öppna PDF i webbläsare.")


def _open_path(path: Path) -> None:
    try:
        subprocess.Popen(["open", str(path)])
    except OSError as e:
        st.error(f"Kan inte öppna fil: {e}")


def render_pdf_opener(root: Path) -> None:
    """Installera den dolda PDF-openern som inline-citatlänkar riktas till."""
    st.markdown(
        '<iframe name="pdf_opener" style="display:none" '
        'width="0" height="0" tabindex="-1" aria-hidden="true"></iframe>',
        unsafe_allow_html=True,
    )
    if "pdf" not in st.query_params:
        return
    path = resolve_pdf_query_path(root, st.query_params["pdf"])
    if path:
        open_pdf_in_browser(
            path,
            page=resolve_pdf_query_page(st.query_params.get("page")),
        )
    st.query_params.clear()


def render_annotation_widget(conn, source: dict, key: str) -> None:
    """Hopfällbar ruta för att läsa/lägga till fritextanteckningar på en källa."""
    payload = source_bookmark_payload(source)
    if not payload["source"]:
        return
    existing = _state_db.list_source_annotations(conn, source=payload["source"])
    label = "✏️ Anteckna" + (f" ({len(existing)})" if existing else "")
    with st.expander(label, expanded=False):
        for a in existing:
            # Ingen unsafe_allow_html: anteckningstexten är användarens egen och
            # ska renderas som vanlig markdown, inte tolkas som HTML.
            st.markdown(f"- {a['note']}")
            if a.get("page"):
                st.caption(f"sida {a['page']}")
        note = st.text_area(
            "Anteckning",
            key=f"{key}_note",
            label_visibility="collapsed",
            placeholder="Din anteckning om den här källan…",
        )
        if (
            st.button("Spara anteckning", key=f"{key}_save", use_container_width=True)
            and note.strip()
        ):
            _state_db.record_source_annotation(conn, note=note, **payload)
            st.toast("Anteckning sparad")
            st.rerun()


def render_source_cards(root: Path, sources: list[dict], conn, *, key_prefix: str) -> None:
    """Rendera källkort med samma PDF/text-knappar som Utredning-vyn."""
    for i, source in enumerate(sources):
        raw_source = str(source.get("source") or "")
        if not raw_source:
            continue
        pdf = find_pdf(root, raw_source)
        txt = find_txt(root, raw_source)
        with st.container(border=True):
            cols = st.columns([5, 2, 2, 2])
            with cols[0]:
                st.markdown(f"**{source_display_title(source)}**")
                st.caption(raw_source)
            with cols[1]:
                if pdf and st.button(
                    "Öppna PDF",
                    key=f"{key_prefix}_pdf_{i}",
                    use_container_width=True,
                ):
                    open_pdf_in_browser(pdf, page=source.get("page"))
            with cols[2]:
                if txt and st.button(
                    "Öppna text",
                    key=f"{key_prefix}_txt_{i}",
                    use_container_width=True,
                ):
                    _open_path(txt)
            with cols[3]:
                bookmark_source_button(conn, source, f"{key_prefix}_bookmark_{i}")
            render_annotation_widget(conn, source, f"{key_prefix}_annot_{i}")


def render_graph_restore_link(entities: list[dict]) -> None:
    """Länk till Graf-sidan med sparade entiteter som center-noder."""
    centers = _graph_center_payload(entities)
    if not centers:
        return
    names = ", ".join(c["namn"] for c in centers)
    label = casebook_entry_title({"question": f"Återskapa graf: {names}"}, max_chars=96)
    st.page_link(
        "pages/3_Graf.py",
        label=label,
        icon=":material/account_tree:",
        query_params=graph_centers_query_params(centers),
    )


def render_casebook_save(
    conn,
    *,
    question: str,
    answer: str,
    mode: str,
    backend_name: str,
    model: str,
    sources: list[dict],
    centers: list[dict] | None,
    key: str,
) -> None:
    """Spara aktuell fråga/svar-vy som ett spår i utredningspärmen."""
    if not question.strip() or not answer.strip():
        return
    cols = st.columns([4, 1])
    with cols[0]:
        note = st.text_input(
            "Anteckning",
            key=f"{key}_note",
            label_visibility="collapsed",
            placeholder="Anteckning till utredningspärmen (valfritt)",
        )
    with cols[1]:
        if st.button("Spara i pärm", key=f"{key}_save", use_container_width=True):
            saved_sources = [
                source_bookmark_payload(source)
                for source in sources
                if source.get("source")
            ]
            _state_db.record_casebook_entry(
                conn,
                question=question,
                answer=answer,
                mode=mode,
                backend=backend_name,
                model=model,
                sources=saved_sources,
                entities=centers or [],
                note=note,
            )
            st.toast("Sparat i utredningspärmen")
            st.rerun()


def render_casebook_page(root: Path, conn) -> None:
    """Rendera den fristående utredningspärmsidan."""
    st.set_page_config(page_title="Palmemordsarkivet — Utredningspärm", layout="wide")
    st.title("Utredningspärm")
    st.caption("Sparade svar och bokmärkta källor från utredningsarbetet.")
    render_pdf_opener(root)

    entries = _state_db.list_casebook_entries(conn, limit=100)
    bookmarks = _state_db.list_source_bookmarks(conn, limit=200)
    annotations = _state_db.list_source_annotations(conn, limit=300)
    st.caption(
        f"{len(entries)} sparade svar · {len(bookmarks)} bokmärken · "
        f"{len(annotations)} anteckningar"
    )

    tab_entries, tab_bookmarks, tab_annotations = st.tabs(
        ["Sparade svar", "Bokmärken", "Anteckningar"]
    )
    with tab_entries:
        _render_saved_answers(root, entries, conn)
    with tab_bookmarks:
        _render_bookmarks(root, bookmarks, conn)
    with tab_annotations:
        _render_annotations(root, annotations, conn)


def _render_saved_answers(root: Path, entries: list[dict], conn) -> None:
    if not entries:
        st.info("Inga sparade svar ännu.")
        return
    for entry in entries:
        title = casebook_entry_title(entry)
        with st.expander(title, expanded=False):
            cols = st.columns([5, 1])
            with cols[0]:
                st.markdown(f"**Fråga:** {entry['question']}")
                meta = " · ".join(
                    p for p in [entry["mode"], entry["backend"], entry["model"]]
                    if p
                )
                st.caption(meta)
                if entry.get("note"):
                    st.caption(f"Anteckning: {entry['note']}")
            with cols[1]:
                if st.button(
                    "Ta bort",
                    key=f"casebook_page_del_{entry['id']}",
                    use_container_width=True,
                ):
                    _state_db.delete_casebook_entry(conn, entry["id"])
                    st.rerun()
            st.markdown(entry["answer"], unsafe_allow_html=True)
            if entry["sources"]:
                st.markdown("**Källor**")
                render_source_cards(
                    root,
                    entry["sources"],
                    conn,
                    key_prefix=f"casebook_entry_{entry['id']}_source",
                )
            if entry["entities"]:
                st.markdown("**Kunskapsgraf**")
                render_graph_restore_link(entry["entities"])


def _render_bookmarks(root: Path, bookmarks: list[dict], conn) -> None:
    if not bookmarks:
        st.info("Inga bokmärkta källor ännu.")
        return
    for bm in bookmarks:
        with st.container(border=True):
            cols = st.columns([5, 1, 1])
            with cols[0]:
                title = bm.get("title") or bm["source"]
                st.markdown(f"### {title}")
                page = f", sida {bm['page']}" if bm.get("page") else ""
                st.caption(f"{bm['source']}{page}")
                if bm.get("note"):
                    st.caption(f"Anteckning: {bm['note']}")
            with cols[1]:
                pdf = find_pdf(root, bm["source"])
                if pdf and st.button(
                    "Öppna PDF",
                    key=f"bookmark_page_pdf_{bm['id']}",
                    use_container_width=True,
                ):
                    open_pdf_in_browser(pdf, page=bm.get("page"))
            with cols[2]:
                if st.button(
                    "Ta bort",
                    key=f"bookmark_page_del_{bm['id']}",
                    use_container_width=True,
                ):
                    _state_db.delete_source_bookmark(conn, bm["id"])
                    st.rerun()


def _render_annotations(root: Path, annotations: list[dict], conn) -> None:
    if not annotations:
        st.info("Inga anteckningar ännu. Skriv anteckningar via källkortens "
                "✏️-ruta i Utredning, Jämförelse eller Maskeringar.")
        return
    for a in annotations:
        with st.container(border=True):
            cols = st.columns([5, 1, 1])
            with cols[0]:
                title = a.get("title") or a["source"]
                page = f", sida {a['page']}" if a.get("page") else ""
                st.markdown(f"**{title}**{page}")
                st.caption(a["source"])
                if a.get("quote"):
                    st.caption(f"”{a['quote']}”")
                st.markdown(a["note"])
            with cols[1]:
                pdf = find_pdf(root, a["source"])
                if pdf and st.button(
                    "Öppna PDF",
                    key=f"annot_page_pdf_{a['id']}",
                    use_container_width=True,
                ):
                    open_pdf_in_browser(pdf, page=a.get("page"))
            with cols[2]:
                if st.button(
                    "Ta bort",
                    key=f"annot_page_del_{a['id']}",
                    use_container_width=True,
                ):
                    _state_db.delete_source_annotation(conn, a["id"])
                    st.rerun()


def pdf_anchor(
    root: Path, source_txt: str, label: str, title: str, page: int | None = None
) -> str:
    """Rendera PDF-länk för källor där filen finns lokalt."""
    pdf = find_pdf(root, source_txt)
    return _citations.pdf_anchor(pdf, label, title=title, page=page) if pdf else label
