"""Tester för OCR-operationens Surya-redo (mode pages)."""

from __future__ import annotations

import sys
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
