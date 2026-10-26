"""Sidgranskning — en PDF-sida bredvid texten som OCR:en gav.

Visar den gällande texten ur ``generated/text/<stem>.txt`` (den som indexeras
och citeras) och, när sidan OCR:ats per sida, motor, poäng, Markdown, layoutblock
och blankettfält. En enskild sida kan skannas om med vald motor och en tidigare
läsning kan återställas — båda som bakgrundsjobbet ``rescan-page``, aldrig i
Streamlit-processen.

Query-parametrarna ``doc=<pdf_stem>&page=<n>`` öppnar en sida direkt.
"""

from __future__ import annotations

import io
import json
import logging
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import casebook_ui as _casebook_ui  # noqa: E402
import db  # noqa: E402
import page_review as _page_review  # noqa: E402
from operations import job_service as _job_service  # noqa: E402
from operations.exceptions import OperationFailed  # noqa: E402
from operations.registry import get_registry  # noqa: E402
from surya_layout import LayoutBlock  # noqa: E402

log = logging.getLogger(__name__)

OPERATION = "rescan-page"
VIEW_DPI = 110
EXCERPT = 80
TERMINAL_JOB_STATUSES = {"succeeded", "failed", "cancelled", "interrupted"}
STALE_TEXT = "Texten har ändrats efter OCR — Markdown används inte i sökindexet"

st.set_page_config(page_title="Palmemordsarkivet — Sidgranskning", layout="wide")
_casebook_ui.render_page_header(
    "Sidgranskning",
    "jämför PDF-sidan med texten OCR:en gav, skanna om en sida eller "
    "återställ en tidigare läsning",
)

root: Path = _page_review.PROJECT_ROOT
conn = _casebook_ui.state_conn()
definition = get_registry().get(OPERATION)
param_defs = {p.name: p for p in definition.parameters}
engine_choices = list(param_defs["engine"].choices)
default_engine = str(param_defs["engine"].default)
# operationens standard-dpi för omskanning; ramarna skalas från layoutens egen dpi
ocr_dpi = int(str(param_defs["dpi"].default))
default_reindex = bool(param_defs["reindex"].default)


@st.cache_data(show_spinner=False, max_entries=64)
def _page_png(pdf_path: str, page: int, dpi: int, mtime: float) -> bytes:
    """Sidbilden som PNG; ``mtime`` ingår i nyckeln så en ny PDF ritas om."""
    image = _page_review.render_page_image(Path(pdf_path), page, dpi)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _rescan_target(job: dict | None) -> tuple[str, int] | None:
    """(stem, sida) för ett ``rescan-page``-jobb, annars ``None``."""
    if not job or job.get("operation") != OPERATION:
        return None
    try:
        params = json.loads(job.get("params_json") or "{}")
        return str(params["pdf_stem"]), int(params["page"])
    except (ValueError, KeyError, TypeError):
        return None


def _active(job: dict | None) -> bool:
    return job is not None and job.get("status") not in TERMINAL_JOB_STATUSES


def _fmt_score(score: float | None) -> str:
    return "–" if score is None else f"{score:.1f}"


# --- Dokument- och sidval -----------------------------------------------------
stems = [r["pdf_stem"] for r in db.list_documents_for_review(conn)]
qp_doc = st.query_params.get("doc", "")
if any(part in qp_doc for part in ("/", "\\", "..")):
    # Stammen blir en sökväg under downloaded/ och generated/text — inga katalogsteg.
    log.debug("ogiltigt doc-värde i länken avvisat: %r", qp_doc)
    st.warning("Länkens dokumentnamn är ogiltigt (innehåller / , \\ eller ..) "
               "och används inte.")
    qp_doc = ""
if qp_doc and qp_doc not in stems:
    # Ett dokument utanför pdf_files (t.ex. bara textfil) går ändå att öppna via länk.
    stems.insert(0, qp_doc)
if not stems:
    st.info("Inga dokument finns ännu. Kör pipelinen först.")
    st.stop()

if "sg_doc" not in st.session_state:
    st.session_state["sg_doc"] = qp_doc if qp_doc in stems else stems[0]
if "sg_page" not in st.session_state:
    try:
        st.session_state["sg_page"] = max(1, int(st.query_params.get("page", "1")))
    except ValueError:
        st.session_state["sg_page"] = 1


def _reset_page() -> None:
    st.session_state["sg_page"] = 1


def _step_page(delta: int, total: int) -> None:
    st.session_state["sg_page"] = min(max(1, int(st.session_state["sg_page"]) + delta), total)


# Navigeringsknapparna får egna, breda kolumner så etiketten aldrig bryts mitt i ordet.
val_doc, val_page, val_prev, val_next = st.columns([5, 1.5, 1.5, 1.5],
                                                   vertical_alignment="bottom")
with val_doc:
    stem: str = st.selectbox(
        "Dokument", stems, key="sg_doc", on_change=_reset_page,
        help="Skriv för att söka bland filstammarna.",
    )

view = _page_review.load_page_view(conn, root, stem, int(st.session_state["sg_page"]))
total = max(view.page_count, 1)
if int(st.session_state["sg_page"]) > total:
    log.debug("sida %s finns inte i %s (%s sidor), visar sista sidan",
              st.session_state["sg_page"], stem, total)
    st.session_state["sg_page"] = total
    view = _page_review.load_page_view(conn, root, stem, total)

with val_page:
    page = int(st.number_input("Sida", min_value=1, max_value=total, step=1, key="sg_page",
                               help=f"Dokumentet har {total} sidor."))
with val_prev:
    st.button("← Föregående", key="sg_prev", disabled=page <= 1,
              on_click=_step_page, args=(-1, total), width="stretch")
with val_next:
    st.button("Nästa →", key="sg_next", disabled=page >= total,
              on_click=_step_page, args=(1, total), width="stretch")

st.query_params["doc"] = stem
st.query_params["page"] = str(page)

pdf_path = _page_review.find_pdf(root, stem)
missing_doc = pdf_path is None and not (root / "generated" / "text" / f"{stem}.txt").exists()
MISSING_TEXT = (f"Dokumentet {stem} finns inte i arkivet — varken PDF eller textfil "
                "hittades, så sidan kan inte visas eller skannas om.")
if missing_doc:
    log.debug("dokumentet %s saknar både PDF och textfil under %s", stem, root)
    st.warning(_page_review.escape_markdown_dollars(MISSING_TEXT))

# --- Sidbild och text ----------------------------------------------------------
vanster, hoger = st.columns(2)
with vanster:
    if not view.pdf_available or pdf_path is None:
        st.info("PDF:en saknas — bara texten visas")
    else:
        show_layout = st.checkbox("Visa layoutblock", key="sg_overlay",
                                  disabled=not view.blocks,
                                  help="Ritar en ram per layoutblock, färgad efter typ.")
        try:
            png = _page_png(str(pdf_path), page, VIEW_DPI, pdf_path.stat().st_mtime)
        except (ValueError, OSError) as exc:
            log.debug("sidbild för %s s.%s gick inte att rendera: %s", stem, page, exc)
            st.warning(f"Sidbilden kunde inte ritas: {exc}")
        else:
            if show_layout and view.blocks:
                from PIL import Image

                image = _page_review.draw_layout_overlay(
                    Image.open(io.BytesIO(png)), view.blocks, view.ocr_dpi, VIEW_DPI)
                st.image(image, width="stretch")
            else:
                st.image(png, width="stretch")


def _layout_rows(blocks: list[LayoutBlock]) -> list[dict[str, str]]:
    rows = []
    for b in blocks:
        text = " ".join(b.text.split())
        rows.append({
            "Ordning": str(b.order),
            "Typ": b.kind.value,
            "Nivå": "" if b.level is None else str(b.level),
            "Bbox": "" if b.bbox is None else ", ".join(str(v) for v in b.bbox),
            "Text": text if len(text) <= EXCERPT else text[: EXCERPT - 1] + "…",
        })
    return rows


def _field_rows(blocks: list[LayoutBlock]) -> list[dict[str, str]]:
    """Blankettfält och förhörsmetadata (block med ``fields``)."""
    rows = []
    for b in blocks:
        for f in b.fields or ():
            rows.append({
                "Fält": f.number or "",
                "Etikett": f.label,
                "Värde": f.value,
                "Kryssruta": "" if f.checked is None else ("ikryssad" if f.checked else "tom"),
            })
    return rows


with hoger:
    field_rows = _field_rows(view.blocks)
    labels = ["Text"]
    if view.markdown:
        labels += ["Markdown", "Markdown-källa"]
    if view.blocks:
        labels.append("Layout")
    if field_rows:
        labels.append("Formulärfält")
    tabs = dict(zip(labels, st.tabs(labels), strict=True))
    with tabs["Text"]:
        if view.current_text is None:
            st.info("Sidan finns inte i textfilen.")
        else:
            st.code(view.current_text, language=None)
    if view.markdown:
        with tabs["Markdown"]:
            st.markdown(_page_review.escape_markdown_dollars(view.markdown))
        with tabs["Markdown-källa"]:
            st.code(view.markdown, language="markdown")
    if view.blocks:
        with tabs["Layout"]:
            st.dataframe(_layout_rows(view.blocks), hide_index=True, width="stretch")
    if field_rows:
        with tabs["Formulärfält"]:
            st.dataframe(field_rows, hide_index=True, width="stretch")

    meta = [f"Motor: {view.engine}", f"Poäng: {_fmt_score(view.score)}"]
    if view.processed_at:
        meta.append(f"Tidpunkt: {view.processed_at}")
    if view.engine_identity:
        meta.append(f"Motoridentitet: {view.engine_identity}")
    st.caption(_page_review.escape_markdown_dollars(" · ".join(meta)))
    if view.markdown and view.stale:
        st.warning(STALE_TEXT)

# --- Omskanning ---------------------------------------------------------------
job = _job_service.reconcile_active_job()
busy = _active(job)
this_page_running = busy and _rescan_target(job) == (stem, page)


def _start(params: dict[str, object], what: str) -> None:
    """Starta ``rescan-page`` med registrets parameternamn."""
    full = {"root": str(root), "pdf_stem": stem, "page": page, "dpi": ocr_dpi, **params}
    if "restore_reading" in full:
        log.debug("startar %s för %s s.%s (%s): reindex=%s restore=%s",
                  OPERATION, stem, page, what, full.get("reindex"), full["restore_reading"])
    else:
        log.debug("startar %s för %s s.%s (%s): motor=%s reindex=%s",
                  OPERATION, stem, page, what, full.get("engine"), full.get("reindex"))
    try:
        started = _job_service.start_job(OPERATION, full)
    except db.ActiveAdminJobError as exc:
        log.debug("rescan-page startades inte, annat jobb aktivt: %s", exc)
        st.error("Ett annat jobb körs redan — vänta tills det är klart och försök igen.")
        return
    except (OperationFailed, ValueError, RuntimeError, OSError) as exc:
        log.debug("rescan-page kunde inte startas för %s s.%s: %s", stem, page, exc)
        st.error(f"Jobbet kunde inte startas: {exc}")
        return
    log.debug("rescan-page startat som jobb %s", started.get("id"))
    st.rerun()


done = st.session_state.pop("sg_job_done", None)
if done:
    if done["status"] == "succeeded":
        st.success(f"Jobbet för sidan {done['page']} är klart — den nya läsningen visas.")
    else:
        st.error(f"Jobbet för sidan {done['page']} slutade med status {done['status']}.")

with st.container(border=True):
    st.markdown("**Skanna om sidan**")
    c_engine, c_reindex, c_button = st.columns([2, 2, 2], vertical_alignment="bottom")
    with c_engine:
        engine = st.selectbox("Motor", engine_choices,
                              index=engine_choices.index(default_engine), key="sg_engine")
    with c_reindex:
        reindex = st.checkbox("Uppdatera sökindexet", value=default_reindex, key="sg_reindex")
    with c_button:
        if st.button("Skanna om sidan", key="sg_rescan", type="primary",
                     disabled=busy or pdf_path is None,
                     width="stretch"):
            _start({"engine": engine, "reindex": reindex}, "omskanning")
    if missing_doc:
        st.caption("Omskanning kräver att dokumentets PDF finns i arkivet.")
    elif pdf_path is None:
        st.caption("Omskanning kräver dokumentets PDF, som saknas i arkivet. "
                   "Återställ i historiken fungerar ändå — sidan kontrolleras mot textfilen.")
    elif busy and not this_page_running and job is not None:
        st.caption(f"Ett annat jobb körs ({job.get('operation')}, {job.get('status')}) — "
                   "omskanning och återställning går att starta när det är klart.")

    if this_page_running and job is not None:
        job_id = str(job["id"])
        log_path = Path(job.get("log_path") or "")

        @st.fragment(run_every=2)
        def _job_status() -> None:
            current = _job_service.reconcile_active_job()
            if not _active(current) or (current or {}).get("id") != job_id:
                row = db.get_admin_job(conn, job_id)
                status = row["status"] if row is not None else "okänd"
                log.debug("rescan-jobb %s för %s s.%s avslutat: %s", job_id, stem, page, status)
                if pdf_path is not None:
                    try:
                        _page_png.clear(str(pdf_path), page, VIEW_DPI, pdf_path.stat().st_mtime)
                    except OSError as exc:
                        log.debug("sidbildens cache rensades inte för %s: %s", pdf_path, exc)
                st.session_state["sg_job_done"] = {"status": status, "page": page}
                st.rerun(scope="app")
            st.caption(f"Jobbet för den här sidan körs ({(current or {}).get('status')}).")
            tail = _job_service.read_log_tail(log_path, lines=20) if log_path.name else ""
            st.code(tail or "(ingen loggrad ännu)", language=None)

        _job_status()

# --- Historik -----------------------------------------------------------------
with st.expander(f"Historik ({len(view.readings)} tidigare läsningar)"):
    if not view.readings:
        st.caption("Sidan har inga sparade tidigare läsningar.")
    shown = st.session_state.get("sg_show_reading")
    for reading in view.readings:
        rid = int(reading["id"])
        reason = "omskanning" if reading["reason"] == "rescan" else "återställning"
        r_text, r_show, r_restore = st.columns([6, 1, 1], vertical_alignment="center")
        with r_text:
            st.caption(_page_review.escape_markdown_dollars(
                f"{reading['recorded_at']} · motor {reading['engine']} · "
                f"poäng {_fmt_score(reading['score'])} · sparad vid {reason}"))
        with r_show:
            if st.button("Dölj" if shown == rid else "Visa", key=f"sg_show_{rid}"):
                shown = None if shown == rid else rid
                st.session_state["sg_show_reading"] = shown
        with r_restore:
            if st.button("Återställ", key=f"sg_restore_{rid}", disabled=busy):
                # Ingen motor skickas: återställningen OCR:ar inte, registrets
                # standardvärde fylls i av jobbtjänsten.
                _start({"reindex": reindex, "restore_reading": rid},
                       f"återställ läsning {rid}")
        if shown == rid:
            st.code(reading["text"] or "", language=None)
