"""Tester för Task 5-adapters: run-funktioner och registry-mappning."""

from __future__ import annotations

from pathlib import Path

import pytest

import db
import merge_pages
import normalize_text
from operations.adapters import (
    detect_redactions_adapter,
    download_adapter,
    extract_entities_adapter,
    extract_map_observations_adapter,
    ingest_adapter,
    llm_correct_adapter,
    normalize_adapter,
)
from operations.context import OperationContext, ProgressSink, TerminalSink
from operations.exceptions import OperationFailed
from operations.models import ProgressUpdate


class RecordingSink(ProgressSink):
    def __init__(self) -> None:
        self.progress: list[ProgressUpdate] = []
        self.logs: list[str] = []

    def write_log(self, message: str, level: str = "info") -> None:
        self.logs.append(message)

    def write_progress(self, update: ProgressUpdate) -> None:
        self.progress.append(update)

    def write_traceback(self, exc: BaseException) -> None:
        pass


def _recording_context() -> tuple[OperationContext, RecordingSink]:
    sink = RecordingSink()
    return OperationContext(sink=sink, cancel_requested=lambda: False), sink


def test_run_normalize_reports_each_changed_file(tmp_path: Path) -> None:
    context, sink = _recording_context()
    text_dir = tmp_path / "text"
    text_dir.mkdir()
    (text_dir / "a.txt").write_text("A\u00ad  B", encoding="utf-8")

    changed = normalize_text.run_normalize(
        root=tmp_path,
        txt_dir=text_dir,
        dry_run=False,
        stats=False,
        rebuild=True,
        files_from=None,
        context=context,
    )

    assert changed == 1
    assert sink.progress[-1].completed == 1


def test_run_merge_pages_all(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    conn = db.connect(db_path)
    db.init_schema(conn)
    db.record_page(conn, pdf_stem="doc", page_num=1, engine="surya", text="sida ett", score=90.0)
    conn.close()

    text_dir = tmp_path / "text"
    text_dir.mkdir()
    (text_dir / "doc.txt").write_text("gammal text", encoding="utf-8")

    import os
    os.environ["STATE_DB"] = str(db_path)
    try:
        context, _ = _recording_context()
        rc = merge_pages.run_merge_pages(
            stem=None, merge_all=True, txt_dir=text_dir, context=context
        )
        assert rc == 0
        assert (text_dir / "doc.txt").read_text(encoding="utf-8") == "sida ett"
    finally:
        os.environ.pop("STATE_DB", None)


def test_normalize_adapter_maps_params(monkeypatch) -> None:
    calls: dict = {}

    def fake_run_normalize(**kwargs):
        calls.update(kwargs)
        return 0

    monkeypatch.setattr(normalize_text, "run_normalize", fake_run_normalize)
    context = OperationContext(sink=TerminalSink(), cancel_requested=lambda: False)

    normalize_adapter(
        context,
        {
            "root": Path("/r"),
            "txt": Path("/r/text"),
            "dry_run": False,
            "stats": False,
            "rebuild": True,
            "files_from": None,
        },
    )

    assert calls["root"] == Path("/r")
    assert calls["txt_dir"] == Path("/r/text")
    assert calls["rebuild"] is True


def test_download_adapter_raises_on_failure(monkeypatch) -> None:
    import download

    monkeypatch.setattr(download, "run_download", lambda **kwargs: 2)
    context = OperationContext(sink=TerminalSink(), cancel_requested=lambda: False)

    with pytest.raises(OperationFailed, match="download"):
        download_adapter(
            context,
            {"out": Path("/out"), "sheet_id": "abc", "limit": 0},
        )


@pytest.mark.parametrize(
    "adapter,module_name,function_name,extra_params",
    (
        (
            llm_correct_adapter,
            "llm_correct",
            "run_llm_correct",
            {
                "threshold": 50.0, "txt": Path("/text"), "root": Path("/root"),
                "test": None,
            },
        ),
        (
            extract_entities_adapter,
            "graph.extract_entities",
            "run_extract_entities",
            {"text_dir": Path("/text"), "limit": None, "timeout": 120.0},
        ),
        (
            extract_map_observations_adapter,
            "extract_map_observations",
            "run_extract_map_observations",
            {"text_dir": Path("/text"), "limit": None, "timeout": 120.0},
        ),
    ),
)
def test_llm_adapters_forward_legacy_provider_overrides(
    monkeypatch, adapter, module_name: str, function_name: str, extra_params: dict
) -> None:
    import importlib

    module = importlib.import_module(module_name)
    received: dict = {}
    monkeypatch.setattr(
        module,
        function_name,
        lambda **kwargs: received.update(kwargs) or 0,
    )
    context = OperationContext(sink=TerminalSink(), cancel_requested=lambda: False)
    params = {
        "profile": "",
        "provider": "openai",
        "model": "gpt-test",
        "base_url": "https://example.invalid/v1",
        "api_key": "secret",
        "jobs": 2,
        "dry_run": False,
        **extra_params,
    }

    adapter(context, params)

    assert received["provider"] == "openai"
    assert received["model"] == "gpt-test"
    assert received["base_url"] == "https://example.invalid/v1"
    assert received["api_key"] == "secret"


def test_detect_redactions_rebuild_text_also_resets_detection_state(monkeypatch) -> None:
    from operations import detect_redactions

    received: dict = {}
    monkeypatch.setattr(
        detect_redactions,
        "run_detect_redactions",
        lambda options, context: received.update(options.__dict__) or 0,
    )
    context = OperationContext(sink=TerminalSink(), cancel_requested=lambda: False)

    detect_redactions_adapter(
        context,
        {
            "root": Path("/root"), "inp": Path("/in"), "txt": Path("/text"),
            "jobs": 4, "dpi": 72, "rebuild": False, "rebuild_text": True,
            "files_from": None,
        },
    )

    assert received["rebuild"] is True


def test_detect_redactions_default_paths_follow_explicit_legacy_root(monkeypatch) -> None:
    from operations import detect_redactions
    from operations.cli import parse_operation_args
    from operations.registry import get_registry

    received: dict = {}
    monkeypatch.setattr(
        detect_redactions,
        "run_detect_redactions",
        lambda options, context: received.update(options.__dict__) or 0,
    )
    params = parse_operation_args(
        get_registry().get("detect-redactions"), ["--root", "/alternate"]
    )

    detect_redactions_adapter(
        OperationContext(sink=TerminalSink(), cancel_requested=lambda: False), params
    )

    assert received["inp"] == Path("/alternate/downloaded/files")
    assert received["txt"] == Path("/alternate/generated/text")


def test_ocr_option_defaults_follow_explicit_legacy_root() -> None:
    from operations.cli import parse_operation_args
    from operations.ocr import OcrOptions
    from operations.registry import get_registry
    from operations.tesseract import TesseractOptions

    ocr_params = parse_operation_args(
        get_registry().get("ocr"), ["--root", "/alternate"]
    )
    tesseract_params = parse_operation_args(
        get_registry().get("ocr-tesseract"), ["--root", "/alternate"]
    )

    assert OcrOptions(**ocr_params).inp == Path("/alternate/downloaded/files")
    assert TesseractOptions(**tesseract_params).tessdata == Path("/alternate/tessdata")


# ---------------------------------------------------------------------------
# ingest_adapter — vidarebefordran av --reindex-since via parse_reindex_since
# ---------------------------------------------------------------------------

def _ingest_params(**overrides) -> dict:
    params = {
        "rebuild": False,
        "limit": None,
        "text_dir": Path("/text"),
        "db_dir": Path("/db"),
        "chunk_chars": 800,
        "chunk_overlap": 150,
        "model": "intfloat/multilingual-e5-large",
        "unusable_list": Path("/unusable.txt"),
        "reindex_since": None,
    }
    params.update(overrides)
    return params


def test_ingest_adapter_parses_and_forwards_reindex_since(monkeypatch) -> None:
    from rag import ingest

    calls: dict = {}

    def fake_run_ingest(**kwargs):
        calls.update(kwargs)
        return 0

    monkeypatch.setattr(ingest, "run_ingest", fake_run_ingest)
    context = OperationContext(sink=TerminalSink(), cancel_requested=lambda: False)

    ingest_adapter(context, _ingest_params(reindex_since="2026-05-01"))

    assert calls["reindex_since"] == ingest.parse_reindex_since("2026-05-01")
    assert calls["model_name"] == "intfloat/multilingual-e5-large"


def test_ingest_adapter_passes_none_when_reindex_since_missing(monkeypatch) -> None:
    from rag import ingest

    calls: dict = {}

    def fake_run_ingest(**kwargs):
        calls.update(kwargs)
        return 0

    monkeypatch.setattr(ingest, "run_ingest", fake_run_ingest)
    context = OperationContext(sink=TerminalSink(), cancel_requested=lambda: False)

    ingest_adapter(context, _ingest_params(reindex_since=None))
    ingest_adapter(context, _ingest_params(reindex_since=""))

    assert calls["reindex_since"] is None


# ---------------------------------------------------------------------------
# ocr-operationen: registry-parametrar mappas via OcrOptions(**params)
# ---------------------------------------------------------------------------

def test_ocr_operation_new_params_map_to_ocr_options() -> None:
    from operations.cli import parse_operation_args
    from operations.ocr import OcrOptions
    from operations.registry import get_registry

    definition = get_registry().get("ocr")
    params = parse_operation_args(definition, [
        "--redo", "--mode", "files", "--source", "ocr", "--no-update-pdf",
        "--in", "/in", "--ocr", "/ocr", "--txt", "/txt",
        "--pages-out", "/pages", "--from-list", "/list.txt", "--retry-failed",
    ])

    options = OcrOptions(**params)

    assert options.redo_only is True
    assert options.mode == "files"
    assert options.source == "ocr"
    assert options.no_update_pdf is True
    assert options.inp == Path("/in")
    assert options.ocr == Path("/ocr")
    assert options.txt == Path("/txt")
    assert options.pages_out == Path("/pages")
    assert options.files_from == Path("/list.txt")
    assert options.retry_failed is True


def test_ocr_tesseract_operation_new_params_map_to_tesseract_options() -> None:
    from operations.cli import parse_operation_args
    from operations.registry import get_registry
    from operations.tesseract import TesseractOptions

    definition = get_registry().get("ocr-tesseract")
    params = parse_operation_args(definition, [
        "--in", "/in", "--ocr", "/ocr", "--txt", "/txt",
        "--tessdata", "/tessdata", "--user-words", "/uw",
        "--user-words-auto", "/uwa", "--tess-config", "/tc",
        "--psm", "3", "--langs", "swe+eng", "--min-text-chars", "100",
        "--image-dpi", "150", "--errors-log", "/err.log",
    ])

    options = TesseractOptions(**params)

    assert options.inp == Path("/in")
    assert options.ocr == Path("/ocr")
    assert options.txt == Path("/txt")
    assert options.tessdata == Path("/tessdata")
    assert options.user_words == Path("/uw")
    assert options.user_words_auto == Path("/uwa")
    assert options.tess_config == Path("/tc")
    assert options.psm == 3
    assert options.langs == "swe+eng"
    assert options.min_text_chars == 100
    assert options.image_dpi == 150
    assert options.errors_log == Path("/err.log")


def test_run_detect_redactions_tolerates_per_file_errors(tmp_path: Path, monkeypatch) -> None:
    """Ett misslyckat dokument får inte avbryta hela redaktionsbatchen."""
    import db

    db_path = tmp_path / "state.db"
    conn = db.connect(db_path)
    db.init_schema(conn)
    pdf_dir = tmp_path / "pdf"
    pdf_dir.mkdir()
    for stem in ("a", "b"):
        (pdf_dir / f"{stem}.pdf").write_bytes(b"%PDF-1.4")
        db.upsert_pdf_file(
            conn, pdf_stem=stem, source="files", pdf_path=str(pdf_dir / f"{stem}.pdf")
        )
    conn.close()
    monkeypatch.setenv("STATE_DB", str(db_path))

    txt_dir = tmp_path / "text"
    txt_dir.mkdir()
    (txt_dir / "a.txt").write_text("text a", encoding="utf-8")
    (txt_dir / "b.txt").write_text("text b", encoding="utf-8")

    from operations.detect_redactions import RedactionsOptions, run_detect_redactions

    called: list[str] = []

    def fake_detect_one(stem, pdf, txt_dir, dpi, wpu_dir, root):
        called.append(stem)
        if stem == "a":
            raise RuntimeError("korrupt pdf")
        return stem

    monkeypatch.setattr("operations.detect_redactions._detect_one", fake_detect_one)

    context, sink = _recording_context()
    options = RedactionsOptions(root=tmp_path, inp=pdf_dir, txt=txt_dir, jobs=2)

    rc = run_detect_redactions(options, context)

    assert rc == 0
    assert set(called) == {"a", "b"}
    assert any("korrupt pdf" in msg for msg in sink.logs)
