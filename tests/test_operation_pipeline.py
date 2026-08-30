"""Tester för run_pipeline med PipelineDependencies-injection.

Verifierar stegordningen och att en misslyckad delstegs-exitkod får
run_pipeline att kasta OperationFailed med stegnamnet i meddelandet.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from operations.context import OperationContext, TerminalSink
from operations.exceptions import OperationFailed
from operations.pipeline import PipelineDependencies, PipelineOptions, run_pipeline


def _context() -> OperationContext:
    return OperationContext(sink=TerminalSink(), cancel_requested=lambda: False)


def _options() -> PipelineOptions:
    return PipelineOptions(root=Path("/root"), inp=Path("/in"), txt=Path("/txt"))


def test_pipeline_options_rebase_defaults_under_custom_root() -> None:
    options = PipelineOptions(root=Path("/custom/root"))

    assert options.inp == Path("/custom/root/downloaded/files")
    assert options.txt == Path("/custom/root/generated/text")


def test_pipeline_runs_steps_in_order() -> None:
    calls: list[str] = []

    def download(**kw) -> int:
        calls.append("download")
        return 0

    def download_wpu(**kw) -> int:
        calls.append("download-wpu")
        return 0

    def ocr(**kw) -> int:
        calls.append("ocr")
        return 0

    def ingest(**kw) -> int:
        calls.append("ingest")
        return 0

    rc = run_pipeline(
        _options(),
        _context(),
        deps=PipelineDependencies(
            download=download,
            download_wpu=download_wpu,
            ocr=ocr,
            ingest=ingest,
        ),
    )

    assert rc == 0
    assert calls == ["download", "download-wpu", "ocr", "ingest"]


def test_pipeline_skips_wpu_and_llm_steps_when_disabled() -> None:
    calls: list[str] = []

    def download(**kw) -> int:
        calls.append("download")
        return 0

    def ocr(**kw) -> int:
        calls.append("ocr")
        return 0

    def ingest(**kw) -> int:
        calls.append("ingest")
        return 0

    def llm_correct(**kw) -> int:
        calls.append("llm-correct")
        return 0

    def quality(**kw) -> int:
        calls.append("quality")
        return 0

    options = _options()
    options.skip_wpu = True
    rc = run_pipeline(
        options,
        _context(),
        deps=PipelineDependencies(
            download=download,
            download_wpu=lambda **kw: (_ for _ in ()).throw(AssertionError("ska inte anropas")),
            ocr=ocr,
            llm_correct=llm_correct,
            quality=quality,
            ingest=ingest,
        ),
    )

    assert rc == 0
    assert calls == ["download", "ocr", "ingest"]


def test_pipeline_raises_operation_failed_when_download_fails() -> None:
    with pytest.raises(OperationFailed, match="Steget download misslyckades"):
        run_pipeline(
            _options(),
            _context(),
            deps=PipelineDependencies(
                download=lambda **kw: 1,
                download_wpu=lambda **kw: 0,
                ocr=lambda **kw: 0,
                ingest=lambda **kw: 0,
            ),
        )


def test_pipeline_continues_after_partial_download_failure() -> None:
    """Exitkod 2 betyder partiell download och ska inte blockera resume-stegen."""
    calls: list[str] = []

    def record(step: str, rc: int = 0):
        def run(**kwargs) -> int:
            calls.append(step)
            return rc

        return run

    rc = run_pipeline(
        _options(),
        _context(),
        deps=PipelineDependencies(
            download=record("download", 2),
            download_wpu=record("download-wpu"),
            ocr=record("ocr"),
            ingest=record("ingest"),
        ),
    )

    assert rc == 0
    assert calls == ["download", "download-wpu", "ocr", "ingest"]


def test_surya_fallback_uses_current_python_interpreter(tmp_path, monkeypatch) -> None:
    """Surya ska ärva den venv-interpreter som startade OCR-operationen."""
    from operations.ocr import OcrOptions, run_surya_fallback

    class FakeConnection:
        def close(self) -> None:
            pass

    class RecordingContext:
        def __init__(self) -> None:
            self.argv: list[str] | None = None

        def check_cancelled(self) -> None:
            pass

        def progress(self, *args) -> None:
            pass

        def log(self, *args, **kwargs) -> None:
            pass

        def run_process(self, argv, **kwargs) -> int:
            self.argv = argv
            return 1

    pdf_dir = tmp_path / "pdf"
    pdf_dir.mkdir()
    (pdf_dir / "doc.pdf").write_bytes(b"%PDF-1.4")
    options = OcrOptions(
        root=tmp_path,
        inp=pdf_dir,
        txt=tmp_path / "text",
        ocr=tmp_path / "ocr",
        pages_out=tmp_path / "pages",
    )
    context = RecordingContext()

    monkeypatch.setattr("operations.ocr.state_db.connect", lambda: FakeConnection())
    monkeypatch.setattr("operations.ocr.state_db.init_schema", lambda conn: None)
    monkeypatch.setattr(
        "operations.ocr.state_db.list_surya_fallback_candidates",
        lambda conn: ["doc"],
    )
    blacklisted: list[str] = []
    monkeypatch.setattr(
        "operations.ocr.state_db.mark_surya_failed",
        lambda conn, stem: blacklisted.append(stem),
    )
    monkeypatch.setattr(
        "operations.ocr.state_db.mark_tesseract_blacklisted",
        lambda conn, stem: blacklisted.append(stem),
    )
    monkeypatch.setattr("operations.ocr.merge_pages.merge_one", lambda *args, **kwargs: None)

    # Ett misslyckat dokument ska blacklistas och loggas, men inte faila steget
    # (gammal shell-semantik: per-fil-fel sammanfattas, pipelinen fortsätter).
    assert run_surya_fallback("test", pdf_dir, options, context) == 0
    assert blacklisted == ["doc", "doc"]
    assert context.argv is not None
    assert context.argv[0] == sys.executable


def test_redo_files_from_list_resets_state_and_runs_chain(tmp_path: Path, monkeypatch) -> None:
    """--redo --mode files --from-list ska nollställa state för listade stems och
    köra hela kedjan (tesseract → redactions → normalize → quality)."""
    import db

    db_path = tmp_path / "state.db"
    conn = db.connect(db_path)
    db.init_schema(conn)
    pdf_dir = tmp_path / "pdf"
    pdf_dir.mkdir()
    (pdf_dir / "doc.pdf").write_bytes(b"%PDF-1.4")
    db.upsert_pdf_file(
        conn, pdf_stem="doc", source="files", pdf_path=str(pdf_dir / "doc.pdf")
    )
    db.mark_tesseract_done(
        conn, "doc", pdf_path=str(pdf_dir / "doc.pdf"), source="files"
    )
    conn.close()

    txt_dir = tmp_path / "text"
    txt_dir.mkdir()
    txt_file = txt_dir / "doc.txt"
    txt_file.write_text("gammal text", encoding="utf-8")

    lst = tmp_path / "lista.txt"
    lst.write_text("doc.txt\n", encoding="utf-8")

    monkeypatch.setenv("STATE_DB", str(db_path))

    from operations.ocr import OcrOptions, run_ocr

    calls: list[str] = []
    monkeypatch.setattr(
        "operations.ocr.run_tesseract",
        lambda opts, ctx: calls.append("tesseract") or 0,
    )
    monkeypatch.setattr(
        "operations.ocr.run_detect_redactions",
        lambda opts, ctx: calls.append("redactions") or 0,
    )
    monkeypatch.setattr(
        "operations.ocr.normalize_text.run_normalize",
        lambda **kw: calls.append("normalize") or 0,
    )
    monkeypatch.setattr(
        "quality.run_quality",
        lambda **kw: calls.append("quality") or 0,
    )
    monkeypatch.setattr("operations.ocr._surya_available", lambda: False)

    options = OcrOptions(
        root=tmp_path,
        inp=pdf_dir,
        txt=txt_dir,
        redo_only=True,
        mode="files",
        files_from=lst,
        skip_redo=True,
    )

    rc = run_ocr(options, _context())

    assert rc == 0
    assert calls == ["tesseract", "redactions", "normalize", "quality"]
    assert not txt_file.exists()

    conn = db.connect(db_path)
    row = db.get_pdf_file(conn, "doc")
    conn.close()
    assert row is not None
    assert row["tesseract_done_at"] is None


def test_pipeline_raises_operation_failed_when_ocr_step_fails() -> None:
    with pytest.raises(OperationFailed, match="Steget ocr misslyckades"):
        run_pipeline(
            _options(),
            _context(),
            deps=PipelineDependencies(
                download=lambda **kw: 0,
                download_wpu=lambda **kw: 0,
                ocr=lambda **kw: 2,
                ingest=lambda **kw: 0,
            ),
        )


def test_pipeline_raises_operation_failed_when_ingest_fails() -> None:
    with pytest.raises(OperationFailed, match="Steget ingest misslyckades"):
        run_pipeline(
            _options(),
            _context(),
            deps=PipelineDependencies(
                download=lambda **kw: 0,
                download_wpu=lambda **kw: 0,
                ocr=lambda **kw: 0,
                ingest=lambda **kw: 3,
            ),
        )
