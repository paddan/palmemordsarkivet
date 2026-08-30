"""Tester för migrerad ocr_tesseract.sh-logik (operations/tesseract.py)."""

from __future__ import annotations

from pathlib import Path

import db
from operations.context import OperationContext, TerminalSink
from operations.tesseract import (
    TesseractOptions,
    build_ocr_attempts,
    detect_language,
    process_pdf,
    text_quality_ok,
)


def test_text_quality_rejects_ocr_garbage() -> None:
    assert not text_quality_ok("1 2 3 x y z 7a 8b " * 50)


def test_text_quality_accepts_clean_swedish() -> None:
    text = "Detta är en ren svensk mening med normala ord och tydlig struktur. " * 5
    assert text_quality_ok(text)


def test_detect_language_returns_swe_plus_eng_for_empty() -> None:
    assert detect_language("") == "swe+eng"
    assert detect_language("   ") == "swe+eng"
    assert detect_language("Detta är svensk text") == "swe"


def test_build_attempts_drops_clean_then_deskew() -> None:
    attempts = build_ocr_attempts(mode="skip", common=["--rotate-pages"])
    assert len(attempts) == 3
    assert "--clean" in attempts[0]
    assert "--clean" not in attempts[1]
    assert "--deskew" not in attempts[2]


def test_build_attempts_redo_has_two_attempts() -> None:
    attempts = build_ocr_attempts(mode="redo", common=[])
    assert len(attempts) == 2
    assert all("--redo-ocr" in a for a in attempts)


def test_completed_pdf_is_not_scheduled(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "state.db"
    conn = db.connect(db_path)
    db.init_schema(conn)
    db.mark_tesseract_done(conn, "doc", pdf_path=str(tmp_path / "doc.pdf"), source="files")
    conn.close()
    monkeypatch.setenv("STATE_DB", str(db_path))

    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4")

    options = TesseractOptions(inp=tmp_path, ocr=tmp_path / "ocr", txt=tmp_path / "text")
    context = OperationContext(sink=TerminalSink(), cancel_requested=lambda: False)

    result = process_pdf(context, options, pdf)

    assert result.status == "hoppar"


def test_run_tesseract_tolerates_per_file_failures(tmp_path: Path, monkeypatch) -> None:
    """Per-dokumentfel ska loggas men inte faila hela Tesseract-steget (rc=0)."""
    db_path = tmp_path / "state.db"
    conn = db.connect(db_path)
    db.init_schema(conn)
    conn.close()
    monkeypatch.setenv("STATE_DB", str(db_path))

    pdf_dir = tmp_path / "pdf"
    pdf_dir.mkdir()
    (pdf_dir / "a.pdf").write_bytes(b"%PDF-1.4")
    (pdf_dir / "b.pdf").write_bytes(b"%PDF-1.4")

    from operations.tesseract import TesseractResult, run_tesseract

    def fake_process_pdf(ctx, opts, pdf, *, conn=None):
        return TesseractResult(pdf.stem, "fel" if pdf.stem == "a" else "ok")

    monkeypatch.setattr("operations.tesseract.process_pdf", fake_process_pdf)

    class Sink:
        def __init__(self) -> None:
            self.logs: list[tuple[str, str]] = []

        def write_log(self, message: str, level: str = "info") -> None:
            self.logs.append((message, level))

        def write_progress(self, update) -> None:
            pass

        def write_traceback(self, exc: BaseException) -> None:
            pass

    from operations.context import OperationContext

    sink = Sink()
    context = OperationContext(sink=sink, cancel_requested=lambda: False)
    options = TesseractOptions(
        inp=pdf_dir, ocr=tmp_path / "ocr", txt=tmp_path / "text", jobs=2
    )

    rc = run_tesseract(options, context)

    assert rc == 0
    assert any("misslyckades" in msg for msg, _ in sink.logs)
