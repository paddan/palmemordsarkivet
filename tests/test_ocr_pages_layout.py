"""Tester för motorn ``surya-layout`` i ``ocr_pages`` (falsk Surya-worker)."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

import db as state_db  # noqa: E402
import ocr_pages  # noqa: E402
from surya_client import SuryaPage, SuryaPageError, SuryaUnavailable  # noqa: E402

TABLE_HTML = "<table><tr><th>Namn</th><th>Tid</th></tr><tr><td>Anna</td><td>21.20</td></tr></table>"


def _raw(html: str, label: str, order: int, bbox: list[int]) -> dict:
    return {"html": html, "label": label, "reading_order": order, "bbox": bbox, "confidence": 0.9}


class _FakeWorker:
    """Ersätter SuryaWorker: svaren styrs per sida, anropen sparas."""

    def __init__(self, pages: dict[int, object]) -> None:
        self.pages = pages
        self.calls: list[tuple[str, int, Path]] = []
        self.identity_calls = 0

    def identity(self) -> str | None:
        self.identity_calls += 1
        return "surya-ocr==0.22.1 surya-ocr-2-gguf"

    def read_page(self, pdf_stem: str, page: int, image_path: Path) -> SuryaPage:
        self.calls.append((pdf_stem, page, image_path))
        answer = self.pages[page]
        if isinstance(answer, BaseException):
            raise answer
        assert isinstance(answer, SuryaPage)
        return answer


class _RecordingSink:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def write_log(self, message: str, level: str = "info") -> None:
        self.lines.append((level, message))

    def write_progress(self, update: object) -> None:
        return None

    def write_traceback(self, exc: BaseException) -> None:
        return None


def _context():
    from operations.context import OperationContext

    sink = _RecordingSink()
    return OperationContext(sink=sink, cancel_requested=lambda: False, debug=True), sink


def _white(height: int = 400, width: int = 200) -> Image.Image:
    return Image.new("RGB", (width, height), "white")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """En PDF vars sidor ersätts av färdiga bilder; state.db i tmp."""
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    errors: list[tuple] = []
    monkeypatch.setattr(ocr_pages, "log_error", lambda *args: errors.append(args))
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.7")
    images: dict[int, Image.Image] = {}
    monkeypatch.setattr(
        ocr_pages, "render_pages", lambda path, dpi: iter(sorted(images.items()))
    )

    def run(worker, **kwargs):
        ctx, sink = _context()
        params = {
            "inp": pdf, "out_dir": str(tmp_path / "pages"), "engine": "surya-layout",
            "langs": "swe", "dpi": 300, "pages": None, "ocr_dir": None,
            "no_update_pdf": False, "no_detect_redactions": False, "txt_dir": None,
        }
        params.update(kwargs)
        rc = ocr_pages.run_ocr_pages(**params, context=ctx, surya_worker=worker)
        conn = state_db.connect()
        rows = {row["page_num"]: row for row in state_db.get_pages_for_stem(conn, "doc")}
        conn.close()
        return rc, rows, sink

    return images, run, errors


def test_layout_page_records_text_and_markdown(setup) -> None:
    images, run, _ = setup
    images[1] = _white()
    worker = _FakeWorker({1: SuryaPage("read", [_raw(TABLE_HTML, "Table", 0, [10, 20, 190, 80])], "m")})

    rc, rows, _ = run(worker)

    assert rc == 0
    row = rows[1]
    assert row["engine"] == "surya-layout"
    assert "| Namn | Tid |" in row["markdown"]
    assert "Namn\tTid" in row["text"]
    assert row["engine_identity"] == "surya-ocr==0.22.1 surya-ocr-2-gguf"
    assert '"TABLE"' in row["layout_json"]
    # sidbilden skickas som absolut sökväg till en tillfällig PNG
    _stem, page, image_path = worker.calls[0]
    assert page == 1 and image_path.is_absolute() and image_path.suffix == ".png"


def test_empty_page_recorded_without_markdown(setup) -> None:
    images, run, _ = setup
    images[1] = _white()
    worker = _FakeWorker({1: SuryaPage("empty", [], "m")})

    rc, rows, _ = run(worker)

    assert rc == 0
    assert rows[1]["text"] == ""
    assert rows[1]["markdown"] is None
    assert rows[1]["engine"] == "surya-layout"


def test_redaction_inserted_between_blocks(setup) -> None:
    images, run, _ = setup
    image = _white()
    for y in range(190, 211):
        for x in range(20, 181):
            image.putpixel((x, y), (0, 0, 0))
    images[1] = image
    worker = _FakeWorker({1: SuryaPage("read", [
        _raw("<p>Före maskeringen</p>", "Text", 0, [10, 20, 190, 60]),
        _raw("<p>Efter maskeringen</p>", "Text", 1, [10, 300, 190, 340]),
    ], "m")})

    rc, rows, _ = run(worker)

    assert rc == 0
    assert rows[1]["text"] == "Före maskeringen\n[MASKAD]\nEfter maskeringen"
    assert "[MASKAD]" in rows[1]["markdown"]


def test_page_error_skips_page_and_continues(setup) -> None:
    images, run, errors = setup
    images[1] = _white()
    images[2] = _white()
    worker = _FakeWorker({
        1: SuryaPageError("IMAGE_UNREADABLE", "bilden gick inte att läsa"),
        2: SuryaPage("read", [_raw("<p>Sida två</p>", "Text", 0, [10, 20, 190, 60])], "m"),
    })

    rc, rows, sink = run(worker)

    assert rc == 0
    assert 1 not in rows
    assert rows[2]["text"] == "Sida två"
    assert ("ocr_pages", "doc.pdf#p1", "IMAGE_UNREADABLE") in errors
    assert any(level == "error" and "IMAGE_UNREADABLE" in msg for level, msg in sink.lines)


def test_runtime_unavailable_aborts_file_with_rc3(setup) -> None:
    images, run, errors = setup
    for n in (1, 2, 3):
        images[n] = _white()
    worker = _FakeWorker({
        1: SuryaPage("read", [_raw("<p>Sida ett</p>", "Text", 0, [10, 20, 190, 60])], "m"),
        2: SuryaUnavailable("NEEDS_LLAMA_CPP", "llama-server saknas"),
        3: SuryaPage("read", [_raw("<p>Sida tre</p>", "Text", 0, [10, 20, 190, 60])], "m"),
    })

    rc, rows, sink = run(worker, pages="1,2,3")

    assert rc == ocr_pages.EXIT_SURYA_UNAVAILABLE == 3
    assert set(rows) == {1}
    assert [call[1] for call in worker.calls] == [1, 2]
    assert ("ocr_pages", "doc.pdf#p2", "NEEDS_LLAMA_CPP") in errors
    assert any(level == "error" and "NEEDS_LLAMA_CPP" in msg for level, msg in sink.lines)


def _text_page(text: str) -> SuryaPage:
    return SuryaPage("read", [_raw(f"<p>{text}</p>", "Text", 0, [10, 20, 190, 60])], "m")


def test_one_worker_death_skips_page_and_continues(setup) -> None:
    images, run, errors = setup
    images[1] = _white()
    images[2] = _white()
    worker = _FakeWorker({
        1: SuryaUnavailable("WORKER_DIED", "workern avslutades utan svar"),
        2: _text_page("Sida två"),
    })

    rc, rows, sink = run(worker)

    assert rc == 0
    assert set(rows) == {2}
    assert [call[1] for call in worker.calls] == [1, 2]
    assert ("ocr_pages", "doc.pdf#p1", "WORKER_DIED") in errors
    assert any(level == "error" and "WORKER_DIED" in msg for level, msg in sink.lines)


def test_three_consecutive_worker_deaths_abort_with_rc3(setup) -> None:
    images, run, _ = setup
    for n in (1, 2, 3, 4):
        images[n] = _white()
    worker = _FakeWorker({
        1: SuryaUnavailable("WORKER_DIED", "x"),
        2: SuryaUnavailable("PROTOCOL_MISMATCH", "x"),
        3: SuryaUnavailable("WORKER_DIED", "x"),
        4: _text_page("Sida fyra"),
    })

    rc, rows, _ = run(worker)

    assert rc == 3
    assert rows == {}
    assert [call[1] for call in worker.calls] == [1, 2, 3]


def test_worker_deaths_must_be_consecutive(setup) -> None:
    """En lyckad sida nollställer räknaren: tre dödsfall med läsningar emellan avbryter inte."""
    images, run, _ = setup
    for n in range(1, 6):
        images[n] = _white()
    death = SuryaUnavailable("WORKER_DIED", "x")
    worker = _FakeWorker({1: death, 2: death, 3: _text_page("tre"), 4: death, 5: death})

    rc, rows, _ = run(worker)

    assert rc == 0
    assert set(rows) == {3}
    assert [call[1] for call in worker.calls] == [1, 2, 3, 4, 5]


def test_layout_png_is_temporary(setup, tmp_path) -> None:
    """Sidbilden finns bara medan workern läser den — även när sidan misslyckas."""
    images, run, _ = setup
    images[1] = _white()
    images[2] = _white()
    seen: list[bool] = []

    class _Checking(_FakeWorker):
        def read_page(self, pdf_stem, page, image_path):  # noqa: ANN001
            seen.append(image_path.exists())
            return super().read_page(pdf_stem, page, image_path)

    worker = _Checking({1: SuryaPageError("INFERENCE_FAILED", "x"), 2: _text_page("två")})

    rc, rows, _ = run(worker)

    assert rc == 0 and set(rows) == {2}
    assert seen == [True, True]
    assert list((tmp_path / "pages").rglob("*.png")) == []


def test_layout_never_reuses_existing_png(setup, tmp_path) -> None:
    """En gammal page-NNN.png (annan dpi) skickas aldrig till workern."""
    images, run, _ = setup
    images[1] = _white()
    stale = tmp_path / "pages" / "doc" / "page-001.png"
    stale.parent.mkdir(parents=True)
    _white(10, 10).save(stale)
    worker = _FakeWorker({1: _text_page("ett")})

    rc, _, _ = run(worker)

    assert rc == 0
    sent = worker.calls[0][2]
    assert sent != stale.resolve()
    assert not sent.exists()


HARNESS = """
import os, sys
from pathlib import Path
from PIL import Image
sys.path.insert(0, {src!r})
sys.path.insert(0, {root!r})
import ocr_pages, surya_client
surya_client.WORKER_PATH = Path({worker!r})
ocr_pages.render_pages = lambda path, dpi: iter([(1, Image.new("RGB", (50, 50), "white"))])
sys.argv = ["ocr_pages.py", "--in", {pdf!r}, "--out-dir", {out!r},
            "--engine", "surya-layout", "--no-detect-redactions"]
sys.exit(ocr_pages.main())
"""

BLOCKING_WORKER = """
import json, subprocess, sys, time, os
if '--identity' in sys.argv:
    print(json.dumps({{"protocol": 1, "type": "identity", "status": "ok", "identity": "fake"}}), flush=True)
    sys.exit(0)
for line in sys.stdin:
    llama = subprocess.Popen(["sleep", "120"])  # står för llama-server
    open({marker!r}, "w").write(f"{{os.getpid()}} {{llama.pid}}")
    time.sleep(120)  # sidan är halvvägs i modellen
"""


def _alive(pid: int) -> bool:
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_sigterm_stops_worker_group(tmp_path) -> None:
    """Ett avbrutet jobb (SIGTERM mot ocr_pages grupp) får inte lämna workern kvar."""
    import contextlib
    import os
    import signal
    import subprocess
    import time

    marker = tmp_path / "pids"
    worker_script = tmp_path / "worker.py"
    worker_script.write_text(BLOCKING_WORKER.format(marker=str(marker)))
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.7")
    harness = tmp_path / "harness.py"
    harness.write_text(HARNESS.format(
        src=str(ROOT / "src"), root=str(ROOT), worker=str(worker_script),
        pdf=str(pdf), out=str(tmp_path / "pages"),
    ))
    env = {**os.environ, "STATE_DB": str(tmp_path / "state.db"),
           "PALME_SURYA_PYTHON": sys.executable}
    process = subprocess.Popen(
        [sys.executable, str(harness)], env=env, start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    worker_pid = llama_pid = 0
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not marker.exists():
            assert process.poll() is None, "harnessen avslutades innan workern läste sidan"
            time.sleep(0.05)
        time.sleep(0.2)
        worker_pid, llama_pid = map(int, marker.read_text().split())
        os.killpg(process.pid, signal.SIGTERM)  # som OperationContext vid cancel
        process.wait(timeout=10)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (_alive(worker_pid) or _alive(llama_pid)):
            time.sleep(0.05)
        assert not _alive(worker_pid)
        assert not _alive(llama_pid)
    finally:
        for pid in (process.pid, worker_pid):
            if pid:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(pid, signal.SIGKILL)
        process.wait(timeout=10)


def test_worker_not_used_when_all_pages_done(setup) -> None:
    """Sidor som redan finns i pdf_pages startar ingen Surya-worker."""
    images, run, _ = setup
    images[1] = _white()
    conn = state_db.connect()
    state_db.init_schema(conn)
    state_db.record_page(conn, pdf_stem="doc", page_num=1, engine="tesseract", text="x", score=80.0)
    conn.close()
    worker = _FakeWorker({})

    rc, rows, sink = run(worker)

    assert rc == 0
    assert worker.calls == [] and worker.identity_calls == 0
    assert rows[1]["engine"] == "tesseract"
    assert any(level == "debug" and "redan i pdf_pages" in msg for level, msg in sink.lines)


def test_identity_fetched_once_per_run(setup) -> None:
    images, run, _ = setup
    images[1] = _white()
    images[2] = _white()
    page = SuryaPage("read", [_raw("<p>Text</p>", "Text", 0, [10, 20, 190, 60])], "m")
    worker = _FakeWorker({1: page, 2: page})

    rc, rows, _ = run(worker)

    assert rc == 0
    assert worker.identity_calls == 1
    assert {rows[1]["engine_identity"], rows[2]["engine_identity"]} == {"surya-ocr==0.22.1 surya-ocr-2-gguf"}


def test_degraded_structure_logged_as_warning(setup) -> None:
    images, run, _ = setup
    images[1] = _white()
    # en tabell utan tabellmarkup tolkas som stycke och markeras degraded
    worker = _FakeWorker({1: SuryaPage("read", [_raw("<p>bara text</p>", "Table", 0, [10, 20, 190, 60])], "m")})

    rc, rows, sink = run(worker)

    assert rc == 0
    assert rows[1]["text"] == "bara text"
    assert any(level == "warning" and "degraderad" in msg for level, msg in sink.lines)
    # tracet loggas på debugnivå, utan sidtext
    trace_lines = [msg for level, msg in sink.lines if level == "debug" and "layout-spår" in msg]
    assert trace_lines and all("bara text" not in msg for msg in trace_lines)


def test_ocr_surya_layout_returns_pdf_lines(tmp_path) -> None:
    image = _white()
    worker = _FakeWorker({3: SuryaPage("read", [
        _raw("<p>Rad ett<br/>rad två</p>", "Text", 0, [10, 20, 190, 60]),
        _raw("<p>Utan ruta</p>", "Text", 1, None),  # type: ignore[arg-type]
    ], "m")})

    text, markdown, layout_json, pdf_lines = ocr_pages.ocr_surya_layout(
        worker, image, tmp_path, "doc", 3, [],
    )

    assert list(tmp_path.glob("*.png")) == []
    assert "Rad ett" in text and "Utan ruta" in text
    assert markdown
    assert json.loads(layout_json)["dpi"] == 300  # standard när ingen dpi ges
    assert pdf_lines == [{"text": "Rad ett\nrad två", "bbox": [10, 20, 190, 60]}]


# --- Renderingstak (beslut BB) ----------------------------------------------------


class _BoundedWorker(_FakeWorker):
    """Som workern: läser PNG:en och vägrar (som PAGE_TOO_LARGE) en bild över gränsen."""

    def __init__(self, pages: dict[int, object], limit: int) -> None:
        super().__init__(pages)
        self.limit = limit
        self.sizes: list[tuple[int, int]] = []

    def read_page(self, pdf_stem: str, page: int, image_path: Path) -> SuryaPage:
        with Image.open(image_path) as im:
            self.sizes.append(im.size)
            assert im.size[0] * im.size[1] <= self.limit, im.size
        return super().read_page(pdf_stem, page, image_path)


@pytest.fixture
def oversize(setup, monkeypatch):
    """Sidan är 200×400 px i 300 dpi men gränsen 40 000 px: den måste renderas om."""
    images, run, errors = setup
    monkeypatch.setattr(ocr_pages, "LAYOUT_MAX_PIXELS", 40_000)
    images[1] = _white(400, 200)
    renders: list[int] = []
    grow: dict[int, int] = {}  # dpi -> extra bildpunkter i varje led (prövar avrundningen)

    def render_page(path, page, dpi):
        renders.append(dpi)
        extra = grow.get(dpi, 0)
        side = round(200 * dpi / 300) + extra, round(400 * dpi / 300) + extra
        return Image.new("RGB", side, "white")

    monkeypatch.setattr(ocr_pages, "render_page", render_page)
    return images, run, renders, grow


def test_oversize_page_rendered_at_highest_dpi_that_fits(oversize) -> None:
    images, run, renders, _ = oversize
    worker = _BoundedWorker({1: _text_page("Hej")}, 40_000)

    rc, rows, sink = run(worker)

    assert rc == 0
    expected = int(300 * (40_000 / 80_000) ** 0.5)  # floor(dpi·sqrt(gräns/bildpunkter)) = 212
    assert renders == [expected] == [212]
    assert worker.sizes == [(141, 283)]
    assert json.loads(rows[1]["layout_json"])["dpi"] == 212
    assert rows[1]["text"] == "Hej"
    debug = [m for level, m in sink.lines if level == "debug" and "dpi" in m and "doc p1" in m]
    assert any("300" in m and "212" in m for m in debug), debug
    assert not [m for level, m in sink.lines if level == "error"]


def test_capped_render_rounding_is_rechecked(oversize) -> None:
    _images, run, renders, grow = oversize
    grow[212] = 2  # i 212 dpi blir bilden 143×285 = 40 755 px, fortfarande för stor
    worker = _BoundedWorker({1: _text_page("Hej")}, 40_000)

    rc, rows, _ = run(worker)

    assert rc == 0
    assert renders == [212, 211]
    assert json.loads(rows[1]["layout_json"])["dpi"] == 211


def test_capped_image_used_for_redactions_and_text_layer(oversize, monkeypatch, tmp_path) -> None:
    _images, run, _renders, _ = oversize
    seen: list[tuple[int, int]] = []
    monkeypatch.setattr(ocr_pages, "detect_redactions_image", lambda image: seen.append(image.size) or [])
    patched: dict = {}

    def fake_update(src, dst, page_lines, dpi, on_failure=None, page_dpi=None):
        patched.update(page_lines=page_lines, dpi=dpi, page_dpi=page_dpi)
        return len(page_lines)

    monkeypatch.setattr(ocr_pages, "update_pdf_text_layer", fake_update)
    page = SuryaPage("read", [_raw("<p>Hej</p>", "Text", 0, [10, 20, 100, 60])], "m")
    worker = _BoundedWorker({1: page}, 40_000)

    rc, _rows, _ = run(worker, ocr_dir=str(tmp_path / "ocr"))

    assert rc == 0
    assert seen == [(141, 283)]  # maskeringarna söks i samma (nedskalade) bild
    assert patched["page_dpi"] == {1: 212} and patched["dpi"] == 300
    assert patched["page_lines"][1][0]["bbox"] == [10, 20, 100, 60]


def test_page_within_bound_is_not_rerendered(setup, monkeypatch) -> None:
    images, run, _ = setup
    monkeypatch.setattr(ocr_pages, "LAYOUT_MAX_PIXELS", 80_000)
    monkeypatch.setattr(ocr_pages, "render_page", lambda *a: pytest.fail("ingen omrendering"))
    images[1] = _white(400, 200)

    rc, rows, _ = run(_BoundedWorker({1: _text_page("Hej")}, 80_000))

    assert rc == 0 and json.loads(rows[1]["layout_json"])["dpi"] == 300


def test_other_engines_ignore_the_render_cap(setup, monkeypatch) -> None:
    images, run, _ = setup
    monkeypatch.setattr(ocr_pages, "LAYOUT_MAX_PIXELS", 10)
    monkeypatch.setattr(ocr_pages, "render_page", lambda *a: pytest.fail("ingen omrendering"))
    monkeypatch.setattr(ocr_pages, "ocr_tesseract", lambda png, langs="swe": "Hej")
    images[1] = _white(400, 200)

    rc, rows, _ = run(None, engine="tesseract")

    assert rc == 0 and rows[1]["layout_json"] is None


def test_capped_dpi_with_real_render(tmp_path) -> None:
    """Riktig rendering (pypdfium2) av en sida som är för stor i 300 dpi."""
    pymupdf = pytest.importorskip("pymupdf")
    pdf = tmp_path / "stor.pdf"
    doc = pymupdf.open()
    doc.new_page(width=1100, height=1100)  # 4 583² ≈ 21 Mpx i 300 dpi
    doc.save(str(pdf))
    doc.close()
    limit = ocr_pages.LAYOUT_MAX_PIXELS
    assert limit == 16_777_216
    first = ocr_pages.render_page(pdf, 1, 300)
    assert first.width * first.height > limit

    image, used = ocr_pages.cap_layout_image(pdf, 1, first, 300, limit)

    assert used == int(300 * (limit / (first.width * first.height)) ** 0.5)
    assert image.width * image.height <= limit
    assert ocr_pages.cap_layout_image(pdf, 1, image, used, limit) == (image, used)


def test_pdf_patch_scales_each_page_by_its_own_dpi(tmp_path, fake_pymupdf) -> None:
    src = tmp_path / "in.pdf"
    src.write_bytes(b"%PDF")
    line = {"text": "Hej", "bbox": [0, 0, 100, 150]}

    ocr_pages.update_pdf_text_layer(src, tmp_path / "out.pdf", {1: [line]}, 300, page_dpi={1: 150})

    # skala 72/150 (sidans dpi), inte 72/300
    assert fake_pymupdf.inserted == [("Hej", pytest.approx(150 * 72 / 150 * 0.85))]


# --- PDF-textlagret med en falsk pymupdf ------------------------------------------


class _FakePage:
    def __init__(self) -> None:
        self.inserted: list[tuple[str, float]] = []

    def get_text(self, kind: str) -> dict:
        return {"blocks": []}

    def add_redact_annot(self, bbox) -> None:  # noqa: ANN001
        return None

    def apply_redactions(self, **kwargs) -> None:  # noqa: ANN003
        return None

    def insert_textbox(self, rect, text, *, fontsize, **kwargs) -> float:  # noqa: ANN001, ANN003
        if text == "trasig":
            return -1.0
        self.inserted.append((text, fontsize))
        return 1.0


class _FakeDoc:
    def __init__(self, page: _FakePage) -> None:
        self.page = page

    def __len__(self) -> int:
        return 1

    def __getitem__(self, index: int) -> _FakePage:
        return self.page

    def save(self, path: str, **kwargs) -> None:  # noqa: ANN003
        Path(path).write_bytes(b"%PDF-patch")

    def close(self) -> None:
        return None


@pytest.fixture
def fake_pymupdf(monkeypatch):
    page = _FakePage()
    module = types.SimpleNamespace(open=lambda path: _FakeDoc(page), Rect=lambda *a: a)
    monkeypatch.setitem(sys.modules, "pymupdf", module)
    return page


def test_pdf_patch_continues_past_uninsertable_block(tmp_path, fake_pymupdf) -> None:
    src = tmp_path / "in.pdf"
    src.write_bytes(b"%PDF")
    failures: list[tuple[int, int]] = []
    lines = [
        {"text": "trasig", "bbox": [0, 0, 100, 20]},
        {"text": "Rad ett\nrad två", "bbox": [0, 30, 100, 70]},
    ]

    patched = ocr_pages.update_pdf_text_layer(
        src, tmp_path / "out.pdf", {1: lines}, 72,
        on_failure=lambda page, index: failures.append((page, index)),
    )

    assert patched == 1
    assert failures == [(1, 0)]
    # flerradigt block: teckenstorleken räknas per rad, inte på hela blockets höjd
    assert fake_pymupdf.inserted == [("Rad ett\nrad två", pytest.approx(40 * 0.85 / 2))]


def test_pdf_patch_without_callback_still_raises(tmp_path, fake_pymupdf) -> None:
    """Den radbaserade Surya-vägen är oförändrad: en rad som inte ryms avbryter."""
    src = tmp_path / "in.pdf"
    src.write_bytes(b"%PDF")
    with pytest.raises(RuntimeError):
        ocr_pages.update_pdf_text_layer(
            src, tmp_path / "out.pdf", {1: [{"text": "trasig", "bbox": [0, 0, 100, 20]}]}, 72,
        )
