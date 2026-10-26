"""Tester för operationen ``rescan-page`` (omskanning och återställning av en sida).

OCR:en körs i operationen som underprocess (``ctx.run_process``); här ersätts
den av en falsk process som kontrollerar argv och spelar upp vad
``ocr_pages.py`` gör: skriver sidans nya läsning i ``pdf_pages``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import db as state_db  # noqa: E402
import ocr_pages  # noqa: E402
from operations.context import OperationContext  # noqa: E402
from operations.exceptions import OperationCancelled, OperationFailed  # noqa: E402

STEM = "doc"
OLD_TEXT = "gammal sida två"
NEW_TEXT = "ny text sida två"


class _Sink:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def write_log(self, message: str, level: str = "info") -> None:
        self.lines.append((level, message))

    def write_progress(self, update: object) -> None:
        return None

    def write_traceback(self, exc: BaseException) -> None:
        return None


class _FakeOcr:
    """Ersätter ``ctx.run_process``: sparar argv och skriver sidan som ocr_pages gör."""

    def __init__(self, rc: int = 0) -> None:
        self.rc = rc
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, cwd, env=None) -> int:
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        if self.rc != 0:
            return self.rc
        page = int(argv[argv.index("--pages") + 1])
        engine = argv[argv.index("--engine") + 1]
        conn = state_db.connect()
        state_db.init_schema(conn)
        state_db.upsert_pdf_file(conn, pdf_stem=STEM, source="palme", pdf_path=argv[argv.index("--in") + 1])
        state_db.record_page(
            conn, pdf_stem=STEM, page_num=page, engine=engine, text=NEW_TEXT,
            score=91.0, markdown="# ny", layout_json="[]", engine_identity="surya-test",
        )
        conn.close()
        return 0


@pytest.fixture
def archive(tmp_path, monkeypatch):
    """Tmp-rot med en tresidig PDF, en textfil och en egen state.db."""
    import pypdfium2 as pdfium

    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    files = tmp_path / "downloaded" / "files"
    files.mkdir(parents=True)
    doc = pdfium.PdfDocument.new()
    for _ in range(3):
        doc.new_page(200, 300)
    doc.save(str(files / f"{STEM}.pdf"))
    text_dir = tmp_path / "generated" / "text"
    text_dir.mkdir(parents=True)
    (text_dir / f"{STEM}.txt").write_text(f"sida ett\f{OLD_TEXT}\fsida tre", encoding="utf-8")
    conn = state_db.connect()
    state_db.init_schema(conn)
    state_db.upsert_pdf_file(conn, pdf_stem=STEM, source="palme", pdf_path=str(files / f"{STEM}.pdf"))
    conn.close()
    return tmp_path


def _context(fake: _FakeOcr, *, cancelled: bool = False):
    sink = _Sink()
    ctx = OperationContext(sink=sink, cancel_requested=lambda: cancelled, debug=True)
    ctx.run_process = fake  # type: ignore[method-assign]
    return ctx, sink


def _run(root: Path, fake: _FakeOcr, **kwargs):
    from operations.rescan import run_rescan_page

    params = {
        "root": root, "pdf_stem": STEM, "page": 2, "engine": "surya-layout",
        "dpi": 300, "reindex": False, "restore_reading": None,
    }
    params.update(kwargs)
    ctx, sink = _context(fake, cancelled=params.pop("cancelled", False))
    rc = run_rescan_page(**params, context=ctx)
    return rc, sink


def _record(page: int, engine: str, text: str | None, score: float | None = 40.0, **extra) -> None:
    conn = state_db.connect()
    state_db.init_schema(conn)
    state_db.record_page(conn, pdf_stem=STEM, page_num=page, engine=engine, text=text,
                         score=score, **extra)
    conn.close()


def _state(page: int = 2):
    conn = state_db.connect()
    state_db.init_schema(conn)
    row = state_db.get_page_row(conn, STEM, page)
    readings = [dict(r) for r in state_db.list_page_readings(conn, STEM, page)]
    conn.close()
    return (dict(row) if row else None), readings


def _file_page(root: Path, page: int = 2) -> str:
    text = (root / "generated" / "text" / f"{STEM}.txt").read_text(encoding="utf-8")
    return text.split("\f")[page - 1]


def test_rescan_archives_then_records(archive) -> None:
    _record(2, "tesseract", OLD_TEXT)
    fake = _FakeOcr()

    rc, sink = _run(archive, fake)

    assert rc == 0
    row, readings = _state()
    assert [(r["reason"], r["engine"], r["text"], r["score"]) for r in readings] == [
        ("rescan", "tesseract", OLD_TEXT, 40.0)
    ]
    assert row["engine"] == "surya-layout" and row["text"] == NEW_TEXT
    assert _file_page(archive) == NEW_TEXT
    argv = fake.calls[0]
    assert argv[1].endswith("src/ocr_pages.py")
    assert argv[argv.index("--pages") + 1] == "2"
    assert argv[argv.index("--engine") + 1] == "surya-layout"
    assert argv[argv.index("--dpi") + 1] == "300"
    assert argv[argv.index("--in") + 1] == str(archive / "downloaded" / "files" / f"{STEM}.pdf")
    assert argv[argv.index("--out-dir") + 1] == str(archive / "generated" / "text_pages")
    assert "--force" in argv
    debug = [message for level, message in sink.lines if level == "debug"]
    assert any("arkiverade läsning" in m and "tesseract" in m for m in debug)
    # Sidtexten får aldrig hamna i loggen.
    assert not any(OLD_TEXT in m or NEW_TEXT in m for _, m in sink.lines)


def test_rescan_archives_file_text_when_no_row(archive) -> None:
    rc, _ = _run(archive, _FakeOcr())

    assert rc == 0
    _row, readings = _state()
    assert [(r["engine"], r["text"], r["score"]) for r in readings] == [
        ("hel-fils-OCR", OLD_TEXT, None)
    ]


def test_rescan_without_any_text_archives_nothing(archive) -> None:
    (archive / "generated" / "text" / f"{STEM}.txt").unlink()

    rc, _ = _run(archive, _FakeOcr())

    assert rc == 0
    row, readings = _state()
    assert readings == []
    assert row["engine"] == "surya-layout"
    assert _file_page(archive) == NEW_TEXT


@pytest.mark.parametrize("page", [0, 99])
def test_rescan_rejects_page_out_of_range(archive, page) -> None:
    fake = _FakeOcr()

    with pytest.raises(OperationFailed, match="1–3"):
        _run(archive, fake, page=page)

    assert fake.calls == []
    assert _state(page)[1] == []


def test_rescan_rejects_missing_pdf(archive) -> None:
    fake = _FakeOcr()

    with pytest.raises(OperationFailed, match="saknas"):
        _run(archive, fake, pdf_stem="finns-inte")

    assert fake.calls == []


@pytest.mark.parametrize("rc", [1, 3])
def test_rescan_failure_keeps_current_reading(archive, rc) -> None:
    _record(2, "tesseract", OLD_TEXT)
    before = _file_page(archive)

    with pytest.raises(OperationFailed) as info:
        _run(archive, _FakeOcr(rc=rc))

    row, readings = _state()
    assert row["engine"] == "tesseract" and row["text"] == OLD_TEXT
    assert _file_page(archive) == before
    # Historikraden som skrevs före försöket tas bort: misslyckade försök
    # får inte lämna kopior "sparad vid omskanning" efter sig.
    assert readings == []
    if rc == 3:
        assert "Surya" in str(info.value) and "Admin" in str(info.value)


def test_rescan_cancel_before_archiving_writes_nothing(archive) -> None:
    _record(2, "tesseract", OLD_TEXT)
    fake = _FakeOcr()

    with pytest.raises(OperationCancelled):
        _run(archive, fake, cancelled=True)

    assert fake.calls == []
    assert _state()[1] == []


def _archived_reading(engine: str, text: str, page: int = 2) -> int:
    conn = state_db.connect()
    state_db.init_schema(conn)
    reading_id = state_db.archive_page_reading(
        conn, pdf_stem=STEM, page_num=page, engine=engine, text=text, markdown=None,
        layout_json=None, engine_identity=None, score=40.0, reason="rescan",
    )
    conn.close()
    return reading_id


def test_restore_reading_becomes_current(archive) -> None:
    reading_id = _archived_reading("tesseract", OLD_TEXT)
    _record(2, "surya-layout", NEW_TEXT, score=91.0, markdown="# ny")
    (archive / "generated" / "text" / f"{STEM}.txt").write_text(
        f"sida ett\f{NEW_TEXT}\fsida tre", encoding="utf-8")
    fake = _FakeOcr()

    rc, _ = _run(archive, fake, restore_reading=reading_id)

    assert rc == 0
    assert fake.calls == []  # ingen OCR vid återställning
    row, readings = _state()
    assert row["engine"] == "tesseract" and row["text"] == OLD_TEXT
    assert row["markdown"] is None  # den återställda läsningen hade ingen Markdown
    assert readings[0]["reason"] == "restore"
    assert (readings[0]["engine"], readings[0]["text"], readings[0]["markdown"]) == (
        "surya-layout", NEW_TEXT, "# ny")
    assert _file_page(archive) == OLD_TEXT


def test_restore_rejects_reading_of_another_page(archive) -> None:
    reading_id = _archived_reading("tesseract", "annan sida", page=1)
    _record(2, "surya-layout", NEW_TEXT)

    with pytest.raises(OperationFailed, match=str(reading_id)):
        _run(archive, _FakeOcr(), restore_reading=reading_id)

    row, readings = _state()
    assert row["text"] == NEW_TEXT and readings == []


def test_restore_rejects_unknown_reading(archive) -> None:
    with pytest.raises(OperationFailed, match="12345"):
        _run(archive, _FakeOcr(), restore_reading=12345)


def test_reindex_flag_runs_incremental_ingest(archive, monkeypatch) -> None:
    from rag import ingest

    calls: list[dict] = []

    def fake_run_ingest(**kwargs):
        conn = state_db.connect()
        state_db.init_schema(conn)
        kwargs["ingest_row"] = state_db.get_ingested_mtime(conn, STEM)
        conn.close()
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(ingest, "run_ingest", fake_run_ingest)
    conn = state_db.connect()
    state_db.init_schema(conn)
    state_db.record_ingest(conn, pdf_stem=STEM, text_mtime=1.0, chunks=3)
    conn.close()

    rc, _ = _run(archive, _FakeOcr(), reindex=True)

    assert rc == 0
    assert len(calls) == 1
    call = calls[0]
    # Dokumentets ingest-rad tas bort så att samma text med ny Markdown indexeras om.
    assert call["ingest_row"] is None
    assert call["rebuild"] is False
    assert call["text_dir"] == archive / "generated" / "text"
    assert call["db_dir"] == archive / "generated" / "lancedb"
    assert call["chunk_chars"] == 800 and call["chunk_overlap"] == 150
    assert call["limit"] is None and call["reindex_since"] is None


def test_reindex_failure_fails_the_job(archive, monkeypatch) -> None:
    from rag import ingest

    monkeypatch.setattr(ingest, "run_ingest", lambda **kwargs: 1)

    with pytest.raises(OperationFailed):
        _run(archive, _FakeOcr(), reindex=True)


def test_no_reindex_skips_ingest(archive, monkeypatch) -> None:
    from rag import ingest

    calls: list[dict] = []
    monkeypatch.setattr(ingest, "run_ingest", lambda **kwargs: calls.append(kwargs) or 0)

    rc, _ = _run(archive, _FakeOcr(), reindex=False)

    assert rc == 0
    assert calls == []


# --- Ruling AV: gällande läsning = textfilens sida -------------------------

def _file_bytes(root: Path) -> bytes:
    return (root / "generated" / "text" / f"{STEM}.txt").read_bytes()


def test_rescan_null_text_row_archives_file_page_and_restore_is_exact(archive) -> None:
    """Äldre rad med text NULL: läsningen finns bara i textfilen och får inte gå förlorad."""
    _record(2, "surya", None, score=None)
    original = _file_bytes(archive)

    _run(archive, _FakeOcr())

    _row, readings = _state()
    assert [(r["engine"], r["text"], r["markdown"], r["layout_json"],
             r["engine_identity"], r["score"]) for r in readings] == [
        ("hel-fils-OCR", OLD_TEXT, None, None, None, None)
    ]
    assert _file_page(archive) == NEW_TEXT

    _run(archive, _FakeOcr(), restore_reading=readings[0]["id"])

    assert _file_page(archive) == OLD_TEXT
    assert _file_bytes(archive) == original


def test_rescan_stale_row_archives_file_page_with_row_engine(archive) -> None:
    """Raden avviker från textfilen (t.ex. LLM-korrigerad): textfilens sida arkiveras."""
    _record(2, "surya-layout", "rå OCR före korrigering", score=55.0,
            markdown="# rå", layout_json="[]", engine_identity="id-gammal")
    original = _file_bytes(archive)

    _run(archive, _FakeOcr())

    _row, readings = _state()
    assert [(r["engine"], r["text"], r["markdown"], r["layout_json"],
             r["engine_identity"], r["score"]) for r in readings] == [
        ("surya-layout", OLD_TEXT, None, None, None, None)
    ]

    _run(archive, _FakeOcr(), restore_reading=readings[0]["id"])

    assert _file_page(archive) == OLD_TEXT
    assert _file_bytes(archive) == original


def test_rescan_null_text_row_without_file_page_archives_nothing(archive) -> None:
    (archive / "generated" / "text" / f"{STEM}.txt").unlink()
    _record(2, "surya", None, score=None)

    _rc, sink = _run(archive, _FakeOcr())

    assert _state()[1] == []
    assert any("ingen gällande läsning" in m for level, m in sink.lines if level == "debug")


def test_restore_refuses_reading_without_text(archive) -> None:
    conn = state_db.connect()
    state_db.init_schema(conn)
    reading_id = state_db.archive_page_reading(
        conn, pdf_stem=STEM, page_num=2, engine="surya", text=None, markdown=None,
        layout_json=None, engine_identity=None, score=None, reason="rescan",
    )
    conn.close()
    _record(2, "surya-layout", OLD_TEXT, score=91.0)

    with pytest.raises(OperationFailed, match="text"):
        _run(archive, _FakeOcr(), restore_reading=reading_id)

    row, readings = _state()
    assert row["engine"] == "surya-layout" and row["text"] == OLD_TEXT
    assert [r["id"] for r in readings] == [reading_id]  # ingen ny historikrad


def test_restore_works_without_pdf_but_rescan_requires_it(archive) -> None:
    reading_id = _archived_reading("tesseract", "äldre läsning")
    (archive / "downloaded" / "files" / f"{STEM}.pdf").unlink()

    rc, _ = _run(archive, _FakeOcr(), restore_reading=reading_id)

    assert rc == 0
    assert _file_page(archive) == "äldre läsning"
    with pytest.raises(OperationFailed, match="saknas"):
        _run(archive, _FakeOcr())


def test_restore_validates_page_against_text_file(archive) -> None:
    reading_id = _archived_reading("tesseract", "sida fyra", page=4)
    (archive / "downloaded" / "files" / f"{STEM}.pdf").unlink()

    with pytest.raises(OperationFailed, match="1–3"):
        _run(archive, _FakeOcr(), restore_reading=reading_id, page=4)


def test_rescan_cancel_during_ocr_removes_its_history_row(archive) -> None:
    _record(2, "tesseract", OLD_TEXT)

    def cancelled(argv, *, cwd, env=None) -> int:
        raise OperationCancelled("Operationen avbröts")

    with pytest.raises(OperationCancelled):
        _run(archive, cancelled)  # type: ignore[arg-type]

    row, readings = _state()
    assert row["text"] == OLD_TEXT
    assert readings == []


def test_restored_whole_file_reading_leaves_page_open_for_surya(archive) -> None:
    """Återställd hel-fils-OCR-läsning: sidan ska kunna nås av Surya-redo igen.

    Före omskanningen saknade sidan ``pdf_pages``-rad, så Surya-redo
    (``page_exists`` falskt) och fallbacken (0 rader = Tesseract-text) behandlade
    den som en vanlig hel-fils-sida. En kvarlämnad rad med motorn ``hel-fils-OCR``
    hade stängt sidan för all framtida sidoredo; därför tas raden bort efter
    sammanslagningen och sidan står som före omskanningen.
    """
    from operations import ocr as ocr_mod

    _run(archive, _FakeOcr())
    reading_id = _state()[1][0]["id"]

    _run(archive, _FakeOcr(), restore_reading=reading_id)

    conn = state_db.connect()
    state_db.init_schema(conn)
    try:
        assert state_db.page_exists(conn, STEM, 2) is False
        assert state_db.count_pages_for_stem(conn, STEM) == 0
        ctx, _ = _context(_FakeOcr())
        pdf = archive / "downloaded" / "files" / f"{STEM}.pdf"
        txt = archive / "generated" / "text" / f"{STEM}.txt"
        assert ocr_mod._fallback_reason(conn, STEM, pdf, txt, ctx) is None
    finally:
        conn.close()
    assert _file_page(archive) == OLD_TEXT


def test_restored_ocr_reading_keeps_its_row(archive) -> None:
    """En återställd riktig OCR-läsning är en gällande per-sida-rad som förut."""
    reading_id = _archived_reading("surya", "surya-läsning")

    _run(archive, _FakeOcr(), restore_reading=reading_id)

    row, _ = _state()
    assert row["engine"] == "surya" and row["text"] == "surya-läsning"


# --- ocr_pages --force ------------------------------------------------------

@pytest.fixture
def pages_run(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setattr(ocr_pages, "log_error", lambda *args: None)
    pdf = tmp_path / f"{STEM}.pdf"
    pdf.write_bytes(b"%PDF-1.7")
    monkeypatch.setattr(
        ocr_pages, "render_pages",
        lambda path, dpi: iter([(1, Image.new("RGB", (50, 50), "white")),
                                (2, Image.new("RGB", (50, 50), "white"))]),
    )
    answers: dict[str, object] = {"text": "ny"}

    def fake_tesseract(png_path, langs="swe"):
        if isinstance(answers["text"], Exception):
            raise answers["text"]
        return answers["text"]

    monkeypatch.setattr(ocr_pages, "ocr_tesseract", fake_tesseract)
    _record(1, "tesseract", "gammal ett")
    _record(2, "tesseract", "gammal två")

    def run(**kwargs) -> int:
        params = {
            "inp": pdf, "out_dir": str(tmp_path / "pages"), "engine": "tesseract",
            "langs": "swe", "dpi": 300, "pages": "2", "ocr_dir": None,
            "no_update_pdf": False, "no_detect_redactions": True, "txt_dir": None,
        }
        params.update(kwargs)
        ctx = OperationContext(sink=_Sink(), cancel_requested=lambda: False)
        return ocr_pages.run_ocr_pages(**params, context=ctx)

    return run, answers


def test_ocr_pages_force_rescans_oversize_layout_page(pages_run, monkeypatch) -> None:
    """Omskanning (``--force``) av en sida som är för stor i 300 dpi (beslut BB):
    sidan läses i lägre dpi i stället för att falla på PAGE_TOO_LARGE."""
    import json

    from surya_client import SuryaPage

    run, _ = pages_run
    monkeypatch.setattr(ocr_pages, "LAYOUT_MAX_PIXELS", 1_000)  # sidan är 50×50 = 2 500 px
    monkeypatch.setattr(ocr_pages, "render_page", lambda path, page, dpi: Image.new(
        "RGB", (round(50 * dpi / 300), round(50 * dpi / 300)), "white"))

    class Worker:
        def identity(self) -> str:
            return "surya-test"

        def read_page(self, pdf_stem, page, image_path):
            with Image.open(image_path) as im:
                assert im.size[0] * im.size[1] <= 1_000, im.size
            return SuryaPage("read", [{"html": "<p>omskannad</p>", "label": "Text",
                                       "reading_order": 0, "bbox": [1, 1, 20, 20]}], "m")

    rc = run(engine="surya-layout", force=True, surya_worker=Worker())

    assert rc == 0
    conn = state_db.connect()
    row = {r["page_num"]: r for r in state_db.get_pages_for_stem(conn, STEM)}[2]
    conn.close()
    assert row["text"] == "omskannad" and row["engine"] == "surya-layout"
    # floor(300·sqrt(1000/2500)) = 189 ger 32×32 = 1 024 px efter avrundningen,
    # så sidan renderas om ett steg lägre
    assert json.loads(row["layout_json"])["dpi"] == 188


def _texts() -> dict[int, str]:
    conn = state_db.connect()
    rows = {r["page_num"]: r["text"] for r in state_db.get_pages_for_stem(conn, STEM)}
    conn.close()
    return rows


def test_ocr_pages_force_reocrs_existing_page(pages_run) -> None:
    run, _ = pages_run

    assert run() == 0
    assert _texts() == {1: "gammal ett", 2: "gammal två"}  # utan --force hoppas sidan

    assert run(force=True) == 0
    assert _texts() == {1: "gammal ett", 2: "ny"}  # bara sidan i --pages


def test_ocr_pages_force_reports_page_that_could_not_be_read(pages_run) -> None:
    run, answers = pages_run
    answers["text"] = RuntimeError("tesseract dog")

    assert run(force=True) == 1
    assert _texts()[2] == "gammal två"


def test_ocr_pages_cli_passes_force(monkeypatch, tmp_path) -> None:
    seen: dict = {}
    monkeypatch.setattr(ocr_pages, "run_ocr_pages", lambda **kwargs: seen.update(kwargs) or 0)
    monkeypatch.setattr(ocr_pages, "_install_sigterm_handler", lambda: None)
    monkeypatch.setattr(sys, "argv", ["ocr_pages.py", "--in", str(tmp_path / "a.pdf"),
                                      "--out-dir", str(tmp_path), "--pages", "2", "--force"])

    assert ocr_pages.main() == 0
    assert seen["force"] is True


def test_ocr_pages_force_ignores_leftover_png(pages_run, tmp_path, monkeypatch) -> None:
    """En kvarlämnad sid-PNG (t.ex. från en annan DPI) får inte läsas under --force."""
    run, _ = pages_run
    stale = tmp_path / "pages" / STEM / "page-002.png"
    stale.parent.mkdir(parents=True)
    Image.new("RGB", (10, 10), "black").save(stale)
    sizes: list[tuple[int, int]] = []

    def fake_tesseract(png_path, langs="swe"):
        with Image.open(png_path) as image:
            sizes.append(image.size)
        return "ny"

    monkeypatch.setattr(ocr_pages, "ocr_tesseract", fake_tesseract)

    assert run(force=True) == 0
    assert sizes == [(50, 50)]  # den färska renderingen, inte den gamla bilden


def test_file_page_reads_the_current_page_text(archive) -> None:
    from page_review import file_page

    assert file_page(archive, STEM, 2) == OLD_TEXT
    assert file_page(archive, STEM, 9) is None
    assert file_page(archive, "saknas", 1) is None


def test_rescan_leaves_stale_neighbour_page_in_file(archive) -> None:
    """En inaktuell grannrad får inte skriva över filens grannsida vid omskanning."""
    _record(1, "tesseract", "Ratad sida ett OCR")  # filen har "sida ett" (t.ex. rättad)

    _run(archive, _FakeOcr())

    assert _file_page(archive, 1) == "sida ett"
    assert _file_page(archive, 2) == NEW_TEXT
    assert _file_page(archive, 3) == "sida tre"


def test_restore_leaves_stale_neighbour_page_in_file(archive) -> None:
    _run(archive, _FakeOcr())
    _row, readings = _state()
    _record(3, "tesseract", "inaktuell sida tre")

    _run(archive, _FakeOcr(), restore_reading=readings[0]["id"])

    assert _file_page(archive, 2) == OLD_TEXT
    assert _file_page(archive, 3) == "sida tre"
