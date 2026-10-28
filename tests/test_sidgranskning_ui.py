"""Beteendetester för Sidgranskning-sidan (headless AppTest).

Sidan körs mot en temporär projektrot (``page_review.PROJECT_ROOT``) med en
2-sidig test-PDF och en textfil, och en temporär state.db (autouse-fixturen i
``conftest.py``). ``start_job`` och ``reconcile_active_job`` byts ut så att inget
bakgrundsjobb startas och inget OCR körs.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from pathlib import Path

import pytest
import streamlit
from streamlit.testing.v1 import AppTest

import casebook_ui
import db
from surya_layout import BlockKind, FormField, LayoutBlock, blocks_to_json, layout_to_json

SIDA = Path(__file__).resolve().parents[1] / "src" / "pages" / "9_Sidgranskning.py"
STEM = "Nr 2 — Testdokument"
ANNAN = "Nr 1 — Annat dokument"


@pytest.fixture(autouse=True)
def _tysta_bare_mode_varningar() -> None:
    logging.getLogger(
        "streamlit.runtime.scriptrunner_utils.script_run_context"
    ).setLevel(logging.ERROR)


def _pdf(path: Path, pages: int) -> None:
    pymupdf = pytest.importorskip("pymupdf")
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    for i in range(pages):
        doc.new_page(width=200, height=200).insert_text((20, 40), f"Sida {i + 1}")
    doc.save(str(path))
    doc.close()


def _text(root: Path, stem: str, pages: list[str]) -> None:
    d = root / "generated" / "text"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{stem}.txt").write_text("\f".join(pages), encoding="utf-8")


@pytest.fixture
def miljo(tmp_path, monkeypatch):
    """Projektrot med två dokument, temporär state.db och utbytta jobbfunktioner."""
    monkeypatch.setattr("page_review.PROJECT_ROOT", tmp_path)
    for stem in (ANNAN, STEM):
        _pdf(tmp_path / "downloaded" / "files" / f"{stem}.pdf", 2)
    _text(tmp_path, ANNAN, ["Annan sida ett", "Annan sida två"])
    _text(tmp_path, STEM, ["Sida ett\tmed tabb", "Sida två rad 1\nrad 2"])
    conn = db.connect()
    db.init_schema(conn)
    for stem in (ANNAN, STEM):
        db.upsert_pdf_file(conn, pdf_stem=stem, source="palme",
                           pdf_path=str(tmp_path / "downloaded" / "files" / f"{stem}.pdf"))
    calls: list[tuple[str, dict]] = []

    def fake_start(operation_id, params, **kwargs):
        calls.append((operation_id, dict(params)))
        return {"id": "job-1", "operation": operation_id, "log_path": str(tmp_path / "j.log")}

    state = {"active": None}
    monkeypatch.setattr("operations.job_service.start_job", fake_start)
    monkeypatch.setattr("operations.job_service.reconcile_active_job",
                        lambda **kw: state["active"])
    yield {"root": tmp_path, "conn": conn, "calls": calls, "state": state}
    conn.close()


def _kor(doc: str | None = STEM, page: int | None = None) -> AppTest:
    at = AppTest.from_file(str(SIDA), default_timeout=60)
    if doc is not None:
        at.query_params["doc"] = doc
    if page is not None:
        at.query_params["page"] = str(page)
    at.run()
    assert not at.exception, at.exception
    return at


def _flikar(at: AppTest) -> list[str]:
    return [t.label for t in at.tabs]


def test_shows_page_text_from_file(miljo):
    at = _kor(STEM)
    assert at.code[0].value == "Sida ett\tmed tabb"
    assert any("hel-fils-OCR" in c.value for c in at.caption)


def test_query_params_select_document_and_page(miljo):
    at = _kor(STEM, 2)
    assert at.selectbox(key="sg_doc").value == STEM
    assert at.number_input(key="sg_page").value == 2
    assert at.code[0].value == "Sida två rad 1\nrad 2"


def test_markdown_tab_only_with_layout_reading(miljo):
    at = _kor(STEM, 1)
    assert _flikar(at) == ["Text"]
    block = LayoutBlock(kind=BlockKind.HEADING, text="Rubrik", order=0,
                        bbox=(10, 10, 100, 40), level=1)
    db.record_page(miljo["conn"], pdf_stem=STEM, page_num=1, engine="surya-layout",
                   text="Sida ett\tmed tabb", score=88.0, markdown="# Rubrik",
                   layout_json=blocks_to_json([block]), engine_identity="surya-2")
    at = _kor(STEM, 1)
    assert _flikar(at) == ["Text", "Markdown", "Markdown-källa", "Layout"]
    assert any(c.value == "# Rubrik" for c in at.code)
    assert any("surya-layout" in c.value and "surya-2" in c.value for c in at.caption)


@pytest.mark.parametrize("layout,expected", [("new", 212), ("old", 300)])
def test_layout_overlay_uses_stored_dpi(miljo, monkeypatch, layout, expected):
    # beslut BB: ramarna skalas från layoutens dpi, inte från operationens standard
    import page_review

    seen: list[int] = []
    real = page_review.draw_layout_overlay

    def spy(image, blocks, ocr_dpi, view_dpi):
        seen.append(ocr_dpi)
        return real(image, blocks, ocr_dpi, view_dpi)

    monkeypatch.setattr(page_review, "draw_layout_overlay", spy)
    block = LayoutBlock(kind=BlockKind.HEADING, text="Rubrik", order=0,
                        bbox=(10, 10, 100, 40), level=1)
    data = layout_to_json([block], 212) if layout == "new" else blocks_to_json([block])
    db.record_page(miljo["conn"], pdf_stem=STEM, page_num=1, engine="surya-layout",
                   text="Sida ett\tmed tabb", score=88.0, markdown="# Rubrik",
                   layout_json=data, engine_identity="surya-2")
    at = _kor(STEM, 1)
    at.checkbox(key="sg_overlay").check().run()
    assert not at.exception, at.exception
    assert seen == [expected]


def test_form_fields_tab_lists_fields(miljo):
    form = LayoutBlock(
        kind=BlockKind.FORM, text="01 Polisdistrikt Stockholm", order=0, bbox=None,
        fields=(FormField("01", "Polisdistrikt", "Stockholm", None),
                FormField("07", "Tips", "", True)),
    )
    header = LayoutBlock(
        kind=BlockKind.FORM, text="Förhörsledare: Ek", order=1, bbox=None,
        fields=(FormField(None, "Förhörsledare", "Ek", None),),
    )
    db.record_page(miljo["conn"], pdf_stem=STEM, page_num=1, engine="surya-layout",
                   text="Sida ett\tmed tabb", score=80.0, markdown="**01 Polisdistrikt**",
                   layout_json=blocks_to_json([form, header]))
    at = _kor(STEM, 1)
    assert "Formulärfält" in _flikar(at)
    frames = [d.value for d in at.dataframe]
    falt = next(f for f in frames if "Etikett" in f.columns)
    assert list(falt["Etikett"]) == ["Polisdistrikt", "Tips", "Förhörsledare"]
    assert list(falt["Värde"]) == ["Stockholm", "", "Ek"]
    assert list(falt["Fält"]) == ["01", "07", ""]


def test_markdown_dollars_escaped(miljo):
    db.record_page(miljo["conn"], pdf_stem=STEM, page_num=1, engine="surya-layout",
                   text="Sida ett\tmed tabb", score=80.0,
                   markdown="| Belopp | 5 $ |\n|---|---|\n| a | 6 $ |")
    at = _kor(STEM, 1)
    assert any("5 \\$" in m.value and "6 \\$" in m.value for m in at.markdown)


def test_stale_warning_shown(miljo):
    db.record_page(miljo["conn"], pdf_stem=STEM, page_num=1, engine="surya-layout",
                   text="Original", score=80.0, markdown="Original")
    at = _kor(STEM, 1)
    assert any("Texten har ändrats efter OCR" in w.value for w in at.warning)


def test_no_stale_warning_without_markdown(miljo):
    db.record_page(miljo["conn"], pdf_stem=STEM, page_num=1, engine="tesseract",
                   text="Original", score=80.0)
    at = _kor(STEM, 1)
    assert not any("Texten har ändrats efter OCR" in w.value for w in at.warning)


def test_missing_pdf_shows_info_and_text(miljo):
    (miljo["root"] / "downloaded" / "files" / f"{STEM}.pdf").unlink()
    at = _kor(STEM, 2)
    assert any("PDF:en saknas" in i.value for i in at.info)
    assert at.code[0].value == "Sida två rad 1\nrad 2"


def test_rescan_button_starts_job_with_params(miljo):
    at = _kor(STEM, 2)
    assert at.selectbox(key="sg_engine").value == "surya-layout"
    assert at.checkbox(key="sg_reindex").value is True
    at.button(key="sg_rescan").click().run()
    assert not at.exception, at.exception
    assert len(miljo["calls"]) == 1
    op, params = miljo["calls"][0]
    assert op == "rescan-page"
    assert params["pdf_stem"] == STEM and params["page"] == 2
    assert params["engine"] == "surya-layout" and params["reindex"] is True
    assert "restore_reading" not in params


def test_rescan_button_disabled_while_job_active(miljo):
    miljo["state"]["active"] = {
        "id": "annat", "operation": "ocr", "status": "running",
        "params_json": json.dumps({}), "log_path": "",
    }
    at = _kor(STEM, 1)
    assert at.button(key="sg_rescan").disabled is True
    assert any("Ett annat jobb körs" in c.value for c in at.caption)


def test_running_rescan_of_this_page_shows_log(miljo):
    log = miljo["root"] / "j.log"
    log.write_text("rad ett\nOCR klar\n", encoding="utf-8")
    miljo["state"]["active"] = {
        "id": "job-1", "operation": "rescan-page", "status": "running",
        "params_json": json.dumps({"pdf_stem": STEM, "page": 1}),
        "log_path": str(log),
    }
    at = _kor(STEM, 1)
    assert at.button(key="sg_rescan").disabled is True
    assert any("OCR klar" in c.value for c in at.code)


def test_restore_starts_job_with_reading_id(miljo):
    rid = db.archive_page_reading(
        miljo["conn"], pdf_stem=STEM, page_num=1, engine="tesseract",
        text="Gammal läsning", markdown=None, layout_json=None,
        engine_identity=None, score=42.0, reason="rescan",
    )
    at = _kor(STEM, 1)
    at.button(key=f"sg_show_{rid}").click().run()
    assert any(c.value == "Gammal läsning" for c in at.code)
    at.button(key=f"sg_restore_{rid}").click().run()
    assert not at.exception, at.exception
    op, params = miljo["calls"][0]
    assert op == "rescan-page"
    assert params["restore_reading"] == rid
    assert params["pdf_stem"] == STEM and params["page"] == 1
    # Motorn spelar ingen roll vid återställning; registrets standardvärde gäller.
    assert "engine" not in params


def test_start_job_conflict_shows_error(miljo, monkeypatch):
    def upptagen(operation_id, params, **kwargs):
        raise db.ActiveAdminJobError("annat-jobb")

    monkeypatch.setattr("operations.job_service.start_job", upptagen)
    at = _kor(STEM, 1)
    at.button(key="sg_rescan").click().run()
    assert not at.exception, at.exception
    assert any("Ett annat jobb körs redan" in e.value for e in at.error)


def test_unknown_document_warns_and_disables_rescan(miljo):
    at = _kor("Nr 99 — Finns inte", 1)
    assert any("finns inte i arkivet" in w.value for w in at.warning)
    assert at.button(key="sg_rescan").disabled is True


def test_missing_pdf_disables_rescan_but_not_restore(miljo):
    rid = db.archive_page_reading(
        miljo["conn"], pdf_stem=STEM, page_num=1, engine="tesseract",
        text="Gammal läsning", markdown=None, layout_json=None,
        engine_identity=None, score=42.0, reason="rescan",
    )
    (miljo["root"] / "downloaded" / "files" / f"{STEM}.pdf").unlink()
    at = _kor(STEM, 1)
    assert at.button(key="sg_rescan").disabled is True
    assert any("PDF" in c.value and "Återställ" in c.value for c in at.caption)
    assert at.button(key=f"sg_restore_{rid}").disabled is False
    at.button(key=f"sg_restore_{rid}").click().run()
    assert not at.exception, at.exception
    op, params = miljo["calls"][0]
    assert op == "rescan-page" and params["restore_reading"] == rid


@pytest.mark.parametrize("doc", ["../hemligt", "a/b", "a\\b", ".."])
def test_doc_query_with_path_characters_is_rejected(miljo, doc):
    at = _kor(doc, 1)
    assert any("ogiltigt" in w.value.lower() for w in at.warning)
    assert at.selectbox(key="sg_doc").value == ANNAN  # som utan val
    assert doc not in at.selectbox(key="sg_doc").options


def test_job_done_with_vanished_pdf_does_not_crash(miljo, monkeypatch):
    """Fragmentet rensar sidbildens cache med PDF:ens mtime; en borttagen PDF får inte fälla sidan."""
    pdf = miljo["root"] / "downloaded" / "files" / f"{STEM}.pdf"
    active = {
        "id": "job-1", "operation": "rescan-page", "status": "running",
        "params_json": json.dumps({"pdf_stem": STEM, "page": 1}), "log_path": "",
    }
    calls = {"n": 0}

    def reconcile(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return active
        if pdf.exists():
            pdf.unlink()
        return None

    monkeypatch.setattr("operations.job_service.reconcile_active_job", reconcile)
    at = AppTest.from_file(str(SIDA), default_timeout=60)
    at.query_params["doc"] = STEM
    at.query_params["page"] = "1"
    at.run()
    assert not at.exception, at.exception


@pytest.mark.parametrize("status", ["succeeded", "failed"])
def test_terminal_status_read_is_thread_safe(miljo, monkeypatch, status):
    """Slutstatusen läses i fragmentets egen tråd med en egen, stängd anslutning.

    AppTest ritar hela sidan och kör fragmentet i sin scripttråd. Här behålls den
    faktiska fragmentfunktionen (dekorationen fångas) och körs sedan på en annan
    levande tråd, medan jobbet redan är terminalt i den riktiga temporära
    state.db. Sidans långlivade anslutning (rad 49) får därför aldrig användas för
    statusläsningen: SQLite:s ``check_same_thread`` ska bestå och fragmentet ska
    öppna och stänga en egen anslutning i den tråd där det körs.
    """
    job_params = json.dumps({"pdf_stem": STEM, "page": 1})
    log = miljo["root"] / "j.log"
    log.write_text("rad ett\nOCR klar\n", encoding="utf-8")
    miljo["state"]["active"] = {
        "id": "job-1", "operation": "rescan-page", "status": "running",
        "params_json": job_params, "log_path": str(log),
    }
    # En riktig rad i temp-databasen, redan terminal när fragmentet körs.
    db.create_admin_job(miljo["conn"], job_id="job-1", operation="rescan-page",
                        params_json=job_params, log_path=str(log))
    db.claim_admin_job(miljo["conn"], "job-1", pid=os.getpid())
    db.finish_admin_job(miljo["conn"], "job-1", status=status,
                        exit_code=0 if status == "succeeded" else 1)

    fragments: list = []

    def capture_fragment(*args, **kwargs):
        def decorate(func):
            fragments.append(func)
            return func
        return decorate

    # Behåll den faktiska funktionen i stället för att låta fragmentet köras
    # bara som en del av AppTests hela sidkörning.
    monkeypatch.setattr(streamlit, "fragment", capture_fragment)

    page_conns: list[tuple[int, sqlite3.Connection]] = []
    real_state_conn = casebook_ui.state_conn

    def spy_state_conn():
        conn = real_state_conn()
        page_conns.append((threading.get_ident(), conn))
        return conn

    monkeypatch.setattr(casebook_ui, "state_conn", spy_state_conn)

    fake_state: dict[str, object] = {}
    reruns: list[str] = []
    opened: list[sqlite3.Connection] = []
    result: dict[str, object] = {}
    release = threading.Event()

    class _Rerun(Exception):
        """Markerar att st.rerun avbröt fragmentet."""

    def fake_rerun(*, scope="app"):
        reruns.append(scope)
        raise _Rerun

    real_connect = db.connect

    def spy_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    def run_fragment() -> None:
        result["thread"] = threading.get_ident()
        if not release.wait(10):
            result["error"] = TimeoutError("fragmentet släpptes aldrig")
            return
        if not fragments:
            return
        try:
            fragments[0]()
        except _Rerun:
            result["rerun"] = True
        except BaseException as exc:  # noqa: BLE001 - felet ska rapporteras
            result["error"] = exc
        # Stängningskontrollen körs i samma tråd som skapade anslutningen; testet
        # stänger alltså aldrig någon annan tråds anslutning.
        closed: list[bool] = []
        for conn in opened:
            try:
                conn.execute("SELECT 1")
            except sqlite3.ProgrammingError as exc:
                closed.append("closed" in str(exc))
            else:
                closed.append(False)
        result["closed"] = closed

    # Tråden finns och väntar redan medan sidan ritas, så sidans anslutning
    # skapas medan tråden lever och får garanterat ett annat tråd-id.
    thread = threading.Thread(target=run_fragment, daemon=True)
    thread.start()

    try:
        at = AppTest.from_file(str(SIDA), default_timeout=60)
        at.query_params["doc"] = STEM
        at.query_params["page"] = "1"
        at.run()
        assert not at.exception, at.exception
        assert len(fragments) == 1
        assert len(page_conns) == 1
        page_thread, _page_conn = page_conns[0]

        # Jobbet är nu terminalt: reconcile returnerar inget aktivt jobb.
        miljo["state"]["active"] = None

        monkeypatch.setattr(streamlit, "session_state", fake_state)
        monkeypatch.setattr(streamlit, "rerun", fake_rerun)
        monkeypatch.setattr(db, "connect", spy_connect)
        release.set()
        thread.join(10)
    finally:
        release.set()
        thread.join(10)

    assert not thread.is_alive()

    assert result["thread"] != page_thread
    assert "error" not in result, result.get("error")
    assert fake_state["sg_job_done"] == {"status": status, "page": 1}
    assert reruns == ["app"]
    assert len(opened) == 1
    assert result["closed"] == [True]
