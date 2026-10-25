"""Tester för OCR-operationens Surya-redo (mode pages)."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from operations import ocr as ocr_mod  # noqa: E402
from operations.ocr import OcrOptions  # noqa: E402


class _Context:
    """Minimal ctx-stub: redo-vägen behöver bara dessa anrop."""

    def check_cancelled(self) -> None:
        return None

    def progress(self, *args: object, **kwargs: object) -> None:
        return None

    def log(self, *args: object, **kwargs: object) -> None:
        return None

    def run_process(self, argv, cwd=None):  # noqa: ANN001
        return 0


@pytest.fixture
def redo_setup(tmp_path, monkeypatch):
    """Ett dokument med en dålig sida och ingen befintlig textfil."""
    inp = tmp_path / "downloaded" / "files"
    txt = tmp_path / "generated" / "text"
    inp.mkdir(parents=True)
    txt.mkdir(parents=True)
    (inp / "doc.pdf").write_bytes(b"%PDF-1.7")

    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setattr(
        ocr_mod.state_db, "list_redo_pages",
        lambda conn, threshold: [{"pdf_stem": "doc", "page_num": 4}],
    )
    monkeypatch.setattr(ocr_mod.state_db, "page_exists", lambda conn, stem, page: False)

    calls: list[tuple[str, Path, dict]] = []
    monkeypatch.setattr(
        ocr_mod.merge_pages, "merge_one",
        lambda stem, txt_dir, **kwargs: calls.append((stem, txt_dir, kwargs)),
    )
    options = OcrOptions(root=tmp_path, inp=inp, txt=txt, mode="pages")
    return options, calls


def test_redo_pages_creates_missing_text_file(redo_setup) -> None:
    """Regression: utan create_missing stannade Surya-texten i pdf_pages."""
    options, calls = redo_setup

    assert ocr_mod._run_redo(options, _Context()) == 0
    assert calls == [("doc", options.txt, {"create_missing": True})]


class _RecordingSink:
    """Samlar loggrader så att debug-grinden kan verifieras."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def write_log(self, message: str, level: str = "info") -> None:
        self.lines.append((level, message))

    def write_progress(self, update: object) -> None:
        return None

    def write_traceback(self, exc: BaseException) -> None:
        return None


def _context(*, debug: bool) -> tuple[object, _RecordingSink]:
    from operations.context import OperationContext

    sink = _RecordingSink()
    return (
        OperationContext(sink=sink, cancel_requested=lambda: False, debug=debug),
        sink,
    )


def test_redo_pages_writes_debug_lines_only_when_enabled(redo_setup) -> None:
    options, _ = redo_setup

    ctx_on, sink_on = _context(debug=True)
    assert ocr_mod._run_redo(options, ctx_on) == 0
    assert any(
        level == "debug" and "sidor under tröskel" in message
        for level, message in sink_on.lines
    )

    ctx_off, sink_off = _context(debug=False)
    assert ocr_mod._run_redo(options, ctx_off) == 0
    assert sink_off.lines  # vanliga rader skrivs som förut
    assert not any(level == "debug" for level, _ in sink_off.lines)


def test_redo_pages_debug_explains_missing_pdf(redo_setup, monkeypatch) -> None:
    """En stam utan PDF hoppas över tyst — debug-raden ska säga varför."""
    options, _ = redo_setup
    (options.inp / "doc.pdf").unlink()

    ctx, sink = _context(debug=True)
    assert ocr_mod._run_redo(options, ctx) == 0
    assert any(
        "PDF saknas" in message for level, message in sink.lines if level == "debug"
    )


class _ArgvContext(_Context):
    """Som _Context men sparar argv till varje subprocess."""

    def __init__(self) -> None:
        self.argv: list[list[str]] = []
        self.lines: list[tuple[str, str]] = []

    def log(self, message: str = "", *args: object, level: str = "info", **kwargs: object) -> None:
        self.lines.append((level, message))

    def run_process(self, argv, cwd=None):  # noqa: ANN001
        self.argv.append([str(a) for a in argv])
        return 0


def _engine_of(argv: list[str]) -> str:
    return argv[argv.index("--engine") + 1]


def test_redo_uses_layout_engine_when_selected(redo_setup) -> None:
    options, _ = redo_setup
    ctx = _ArgvContext()

    assert ocr_mod._run_redo(replace(options, surya_engine="layout"), ctx) == 0
    assert [_engine_of(a) for a in ctx.argv] == ["surya-layout"]


def test_redo_keeps_line_engine_by_default(redo_setup) -> None:
    options, _ = redo_setup
    ctx = _ArgvContext()

    assert options.surya_engine == "lines"
    assert ocr_mod._run_redo(options, ctx) == 0
    assert [_engine_of(a) for a in ctx.argv] == ["surya"]


def test_fallback_uses_layout_engine_when_selected(tmp_path, monkeypatch) -> None:
    inp = tmp_path / "files"
    txt = tmp_path / "text"
    inp.mkdir()
    txt.mkdir()
    (inp / "doc.pdf").write_bytes(b"%PDF-1.7")
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setattr(ocr_mod.state_db, "list_surya_fallback_candidates", lambda conn: ["doc"])
    monkeypatch.setattr(ocr_mod.merge_pages, "merge_one", lambda *a, **k: None)
    # ingen text skrivs av den falska processen — markeringarna är inte testets sak
    monkeypatch.setattr(ocr_mod.state_db, "mark_surya_failed", lambda conn, stem: None)
    monkeypatch.setattr(ocr_mod.state_db, "mark_tesseract_blacklisted", lambda conn, stem: None)
    ctx = _ArgvContext()

    options = OcrOptions(root=tmp_path, inp=inp, txt=txt, surya_engine="layout")
    assert ocr_mod.run_surya_fallback("test", inp, options, ctx) == 0
    assert [_engine_of(a) for a in ctx.argv] == ["surya-layout"]


def test_missing_layout_runtime_is_logged_with_reason(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ocr_mod, "surya_layout_available", lambda: False)
    monkeypatch.setattr(ocr_mod, "_surya_available", lambda: True)
    ctx = _ArgvContext()

    options = OcrOptions(root=tmp_path, fallback_only=True, surya_engine="layout")
    assert ocr_mod.run_ocr(options, ctx) == 1
    assert ctx.argv == []
    assert any(
        level == "error" and "PALME_SURYA_PYTHON" in message for level, message in ctx.lines
    )



# --- exitkod 3 och delvis lästa filer (Ruling Y/Z/AB) -------------------------------


def _record_pages(stem: str, *pages: int) -> None:
    """Som om ocr_pages hade sparat sidorna i pdf_pages."""
    conn = ocr_mod.state_db.connect()
    ocr_mod.state_db.init_schema(conn)
    # som ocr_pages: pdf_files-raden finns innan sidorna sparas
    ocr_mod.state_db.upsert_pdf_file(conn, pdf_stem=stem, source="palme", pdf_path=f"{stem}.pdf")
    for page in pages:
        ocr_mod.state_db.record_page(
            conn, pdf_stem=stem, page_num=page, engine="surya-layout", text="ny text", score=70.0,
        )
    conn.close()


class _ScriptedContext(_ArgvContext):
    """Kör ett skript per subprocess: (exitkod, sidor som "sparas")."""

    def __init__(self, script: list[tuple[int, tuple[int, ...]]]) -> None:
        super().__init__()
        self.script = list(script)

    def run_process(self, argv, cwd=None):  # noqa: ANN001
        self.argv.append([str(a) for a in argv])
        rc, pages = self.script.pop(0)
        stem = Path(argv[argv.index("--in") + 1]).stem
        _record_pages(stem, *pages)
        return rc


@pytest.fixture
def fallback_setup(tmp_path, monkeypatch):
    inp = tmp_path / "files"
    txt = tmp_path / "text"
    inp.mkdir()
    txt.mkdir()
    for stem in ("a", "b"):
        (inp / f"{stem}.pdf").write_bytes(b"%PDF-1.7")
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setattr(ocr_mod.state_db, "list_surya_fallback_candidates", lambda conn: ["a", "b"])
    marks: list[tuple[str, str]] = []
    monkeypatch.setattr(ocr_mod.state_db, "mark_surya_failed",
                        lambda conn, stem: marks.append(("surya_failed", stem)))
    monkeypatch.setattr(ocr_mod.state_db, "mark_tesseract_blacklisted",
                        lambda conn, stem: marks.append(("blacklisted", stem)))
    merged: list[str] = []

    def merge(stem, txt_dir, **kwargs):  # noqa: ANN001, ANN003
        merged.append(stem)
        (txt_dir / f"{stem}.txt").write_text("ny text", encoding="utf-8")

    monkeypatch.setattr(ocr_mod.merge_pages, "merge_one", merge)
    normalized: list[str] = []
    monkeypatch.setattr(ocr_mod.normalize_text, "process_file",
                        lambda path: normalized.append(Path(path).stem))
    options = OcrOptions(root=tmp_path, inp=inp, txt=txt, surya_engine="layout")
    return inp, options, marks, merged, normalized


def test_fallback_rc3_marks_nothing_and_stops(fallback_setup) -> None:
    inp, options, marks, merged, normalized = fallback_setup
    ctx = _ScriptedContext([(3, ()), (0, (1,))])

    assert ocr_mod.run_surya_fallback("test", inp, options, ctx) != 0
    assert len(ctx.argv) == 1
    assert marks == []
    assert merged == [] and normalized == []
    errors = [m for level, m in ctx.lines if level == "error"]
    assert len(errors) == 1 and "3" in errors[0]


def test_fallback_rc3_keeps_pages_read_before_abort(fallback_setup) -> None:
    inp, options, marks, merged, normalized = fallback_setup
    ctx = _ScriptedContext([(3, (1, 2))])

    assert ocr_mod.run_surya_fallback("test", inp, options, ctx) != 0
    assert merged == ["a"] and normalized == ["a"]
    assert marks == []


def test_fallback_genuine_failure_still_marks_file(fallback_setup) -> None:
    inp, options, marks, _merged, _normalized = fallback_setup
    ctx = _ScriptedContext([(1, ()), (1, ())])

    assert ocr_mod.run_surya_fallback("test", inp, options, ctx) == 0
    assert ("surya_failed", "a") in marks and ("blacklisted", "b") in marks


def test_redo_rc3_stops_step_without_merge(redo_setup, monkeypatch) -> None:
    options, calls = redo_setup
    monkeypatch.setattr(
        ocr_mod.state_db, "list_redo_pages",
        lambda conn, threshold: [{"pdf_stem": "doc", "page_num": 4},
                                 {"pdf_stem": "doc2", "page_num": 1}],
    )
    (options.inp / "doc2.pdf").write_bytes(b"%PDF-1.7")
    ctx = _ScriptedContext([(3, ()), (0, (1,))])

    assert ocr_mod._run_redo(replace(options, surya_engine="layout"), ctx) != 0
    assert len(ctx.argv) == 1
    assert calls == []
    errors = [m for level, m in ctx.lines if level == "error"]
    assert len(errors) == 1


def test_redo_merges_new_pages_even_when_rc_nonzero(redo_setup, monkeypatch) -> None:
    options, calls = redo_setup
    normalized: list[str] = []
    monkeypatch.setattr(ocr_mod.normalize_text, "process_file",
                        lambda path: normalized.append(Path(path).stem))
    (options.txt / "doc.txt").write_text("gammal", encoding="utf-8")
    ctx = _ScriptedContext([(1, (4,))])

    ocr_mod._run_redo(options, ctx)

    assert [c[0] for c in calls] == ["doc"]
    assert normalized == ["doc"]
    # filen räknas ändå som misslyckad
    assert any(level == "error" and "misslyckades" in m for level, m in ctx.lines)


def test_redo_rc3_merges_pages_read_before_abort(redo_setup) -> None:
    options, calls = redo_setup
    ctx = _ScriptedContext([(3, (4,))])

    assert ocr_mod._run_redo(options, ctx) != 0
    assert [c[0] for c in calls] == ["doc"]


def test_redo_without_new_pages_and_failure_does_not_merge(redo_setup) -> None:
    options, calls = redo_setup
    ctx = _ScriptedContext([(1, ())])

    ocr_mod._run_redo(options, ctx)

    assert calls == []


def test_redo_only_checks_layout_availability_once(tmp_path, monkeypatch) -> None:
    checks: list[int] = []

    def unavailable() -> bool:
        checks.append(1)
        return False

    monkeypatch.setattr(ocr_mod, "surya_layout_available", unavailable)
    ctx = _ArgvContext()

    options = OcrOptions(root=tmp_path, redo_only=True, surya_engine="layout")
    assert ocr_mod.run_ocr(options, ctx) == 1
    assert checks == [1]
    assert ctx.argv == []
    assert any(level == "error" and "PALME_SURYA_PYTHON" in m for level, m in ctx.lines)


# --- ofullständiga filer och en stoppad Surya-runtime per körning (Ruling AC/AD) ----


def _write_pdf(path: Path, pages: int) -> None:
    """En riktig PDF med ``pages`` tomma sidor (sidantalet läses via pypdfium2)."""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument.new()
    try:
        for _ in range(pages):
            doc.new_page(200, 200)
        doc.save(str(path))
    finally:
        doc.close()


def _mark_tesseract_failed(stem: str, pdf: Path) -> None:
    conn = ocr_mod.state_db.connect()
    ocr_mod.state_db.init_schema(conn)
    ocr_mod.state_db.mark_tesseract_failed(conn, stem, pdf_path=str(pdf), source="palme")
    conn.close()


@pytest.fixture
def incomplete_setup(tmp_path, monkeypatch):
    """Riktig state.db: kandidaturvalet går genom list_surya_fallback_candidates."""
    inp = tmp_path / "files"
    txt = tmp_path / "text"
    inp.mkdir()
    txt.mkdir()
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    marks: list[tuple[str, str]] = []
    monkeypatch.setattr(ocr_mod.state_db, "mark_surya_failed",
                        lambda conn, stem: marks.append(("surya_failed", stem)))
    monkeypatch.setattr(ocr_mod.state_db, "mark_tesseract_blacklisted",
                        lambda conn, stem: marks.append(("blacklisted", stem)))

    def merge(stem, txt_dir, **kwargs):  # noqa: ANN001, ANN003
        (txt_dir / f"{stem}.txt").write_text("ny text", encoding="utf-8")

    monkeypatch.setattr(ocr_mod.merge_pages, "merge_one", merge)
    monkeypatch.setattr(ocr_mod.normalize_text, "process_file", lambda path: None)
    options = OcrOptions(root=tmp_path, inp=inp, txt=txt, surya_engine="layout")
    return inp, options, marks


def _stems_in(argv: list[list[str]]) -> list[str]:
    return [Path(a[a.index("--in") + 1]).stem for a in argv]


def test_fallback_picks_incomplete_stem_with_text(incomplete_setup) -> None:
    inp, options, _marks = incomplete_setup
    # ofullständig: 3 sidor i PDF:en, 1 i pdf_pages, text finns
    _write_pdf(inp / "half.pdf", 3)
    _mark_tesseract_failed("half", inp / "half.pdf")
    _record_pages("half", 1)
    (options.txt / "half.txt").write_text("sida 1", encoding="utf-8")
    # komplett: alla sidor lästa
    _write_pdf(inp / "full.pdf", 2)
    _mark_tesseract_failed("full", inp / "full.pdf")
    _record_pages("full", 1, 2)
    (options.txt / "full.txt").write_text("sida 1 och 2", encoding="utf-8")
    # oläsbar PDF med text: ingen krasch, ingen kandidat
    (inp / "broken.pdf").write_bytes(b"%PDF-1.7")
    _mark_tesseract_failed("broken", inp / "broken.pdf")
    (options.txt / "broken.txt").write_text("gammal text", encoding="utf-8")
    # Surya-misslyckad (och blacklistad): rörs inte trots att den är ofullständig
    _write_pdf(inp / "dead.pdf", 3)
    _mark_tesseract_failed("dead", inp / "dead.pdf")
    _record_pages("dead", 1)
    (options.txt / "dead.txt").write_text("sida 1", encoding="utf-8")
    import db.pipeline as db_pipeline  # fixturen har bytt ut markeringarna i db

    conn = ocr_mod.state_db.connect()
    db_pipeline.mark_surya_failed(conn, "dead")
    db_pipeline.mark_tesseract_blacklisted(conn, "dead")
    conn.close()

    ctx, sink = _context(debug=True)
    argv: list[list[str]] = []
    ctx.run_process = lambda a, cwd=None: argv.append([str(x) for x in a]) or 0  # type: ignore[method-assign]

    assert ocr_mod.run_surya_fallback("test", inp, options, ctx) == 0
    assert _stems_in(argv) == ["half"]
    assert any(
        level == "debug" and "half" in m and "1 av 3" in m for level, m in sink.lines
    )


def test_fallback_retries_file_after_rc3_with_pages_recorded(incomplete_setup) -> None:
    inp, options, marks = incomplete_setup
    _write_pdf(inp / "a.pdf", 3)
    _mark_tesseract_failed("a", inp / "a.pdf")

    first = _ScriptedContext([(3, (1, 2))])
    assert ocr_mod.run_surya_fallback("test", inp, options, first) == ocr_mod.EXIT_SURYA_UNAVAILABLE
    assert (options.txt / "a.txt").read_text(encoding="utf-8").strip()

    second = _ScriptedContext([(0, (3,))])
    assert ocr_mod.run_surya_fallback("test", inp, options, second) == 0
    assert _stems_in(second.argv) == ["a"]
    assert "--pages" not in second.argv[0]  # ocr_pages hoppar själv redan sparade sidor
    assert marks == []


def test_redo_lists_unread_pages_again_after_rc3(redo_setup, monkeypatch) -> None:
    """Ingen kodändring: sidor som inte hann läsas står kvar i list_redo_pages."""
    options, _calls = redo_setup
    import db.pipeline as db_pipeline

    monkeypatch.setattr(ocr_mod.state_db, "list_redo_pages", db_pipeline.list_redo_pages)
    monkeypatch.setattr(ocr_mod.state_db, "page_exists", db_pipeline.page_exists)
    conn = ocr_mod.state_db.connect()
    ocr_mod.state_db.init_schema(conn)
    for page in (4, 5):
        db_pipeline.record_quality_page(conn, pdf_stem="doc", page_num=page, score=10.0)
    conn.close()

    first = _ScriptedContext([(3, (4,))])
    assert ocr_mod._run_redo(options, first) == ocr_mod.EXIT_SURYA_UNAVAILABLE
    assert first.argv[0][first.argv[0].index("--pages") + 1] == "4,5"

    second = _ScriptedContext([(0, (5,))])
    assert ocr_mod._run_redo(options, second) == 0
    assert second.argv[0][second.argv[0].index("--pages") + 1] == "5"


@pytest.fixture
def run_ocr_setup(tmp_path, monkeypatch):
    """run_ocr med alla steg utbytta mot inspelare; Surya-stegen styrs per test."""
    (tmp_path / "downloaded" / "wpu_files").mkdir(parents=True)
    steps: list[str] = []
    monkeypatch.setattr(ocr_mod, "_surya_ready", lambda options, ctx: True)
    monkeypatch.setattr(ocr_mod, "run_tesseract",
                        lambda opts, ctx: steps.append(f"tesseract:{opts.inp.name}") or 0)
    monkeypatch.setattr(ocr_mod, "run_detect_redactions",
                        lambda opts, ctx: steps.append("redactions") or 0)
    monkeypatch.setattr(ocr_mod.normalize_text, "run_normalize",
                        lambda **kw: steps.append("normalize") or 0)
    import merge_wpu
    import quality

    monkeypatch.setattr(merge_wpu, "run_merge_wpu", lambda **kw: steps.append("merge_wpu") or 0)
    monkeypatch.setattr(quality, "run_quality", lambda **kw: steps.append("quality") or 0)
    options = OcrOptions(root=tmp_path, inp=tmp_path / "downloaded" / "files")
    return options, steps


def _scripted_surya(monkeypatch, steps: list[str], fallback_rc: dict[str, int]) -> None:
    def fallback(label, pdf_dir, options, context):  # noqa: ANN001
        steps.append(f"fallback:{label}")
        return fallback_rc.get(label, 0)

    def redo(options, ctx):  # noqa: ANN001
        steps.append("redo")
        return 0

    monkeypatch.setattr(ocr_mod, "run_surya_fallback", fallback)
    monkeypatch.setattr(ocr_mod, "_run_redo", redo)


def _skip_lines(ctx: _ArgvContext) -> list[str]:
    return [m for _level, m in ctx.lines if "hoppas" in m and "Surya" in m]


def test_run_ocr_palme_rc3_skips_remaining_surya_steps(run_ocr_setup, monkeypatch) -> None:
    options, steps = run_ocr_setup
    _scripted_surya(monkeypatch, steps, {"palmemordsarkivet": 3})
    ctx = _ArgvContext()

    assert ocr_mod.run_ocr(options, ctx) != 0
    assert "fallback:wpu.nu" not in steps and "redo" not in steps
    assert steps == [
        "tesseract:files", "fallback:palmemordsarkivet", "tesseract:wpu_files",
        "merge_wpu", "redactions", "normalize", "quality",
    ]
    lines = _skip_lines(ctx)
    assert len(lines) == 1
    assert "palmemordsarkivet" in lines[0] and "3" in lines[0]


def test_run_ocr_wpu_rc3_skips_redo(run_ocr_setup, monkeypatch) -> None:
    options, steps = run_ocr_setup
    _scripted_surya(monkeypatch, steps, {"wpu.nu": 3})
    ctx = _ArgvContext()

    assert ocr_mod.run_ocr(options, ctx) != 0
    assert "fallback:wpu.nu" in steps and "redo" not in steps
    assert steps[-3:] == ["redactions", "normalize", "quality"]
    lines = _skip_lines(ctx)
    assert len(lines) == 1 and "wpu.nu" in lines[0]


def test_run_ocr_without_rc3_runs_every_surya_step(run_ocr_setup, monkeypatch) -> None:
    options, steps = run_ocr_setup
    _scripted_surya(monkeypatch, steps, {})
    ctx = _ArgvContext()

    assert ocr_mod.run_ocr(options, ctx) == 0
    assert "fallback:wpu.nu" in steps and "redo" in steps
    assert _skip_lines(ctx) == []
