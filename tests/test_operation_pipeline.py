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


@pytest.fixture(autouse=True)
def _no_real_neo4j(monkeypatch) -> None:
    """Pipelinens dokumentnodssteg får aldrig nå en riktig Neo4j i testerna."""
    from graph import load_documents

    monkeypatch.setattr(load_documents, "run_load_documents", lambda **kw: 0)


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


def test_pipeline_passes_selected_llm_profile_to_correction(tmp_path, monkeypatch) -> None:
    import config

    config_file = tmp_path / "llm_config.json"
    config_file.write_text(
        '{"profiles": {"Deepseek v4 flash": {}}, "default": "Deepseek v4 flash"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_FILE", config_file)
    received: dict[str, object] = {}

    def ok(**_kw) -> int:
        return 0

    def llm_correct(**kwargs) -> int:
        received.update(kwargs)
        return 0

    options = PipelineOptions(
        root=Path("/root"), inp=Path("/in"), txt=Path("/txt"),
        skip_wpu=True, with_llm=True, profile="Deepseek v4 flash",
    )
    rc = run_pipeline(
        options,
        _context(),
        deps=PipelineDependencies(
            download=ok, ocr=ok, llm_correct=llm_correct, quality=ok, ingest=ok,
        ),
    )

    assert rc == 0
    assert received["profile"] == "Deepseek v4 flash"


def test_pipeline_rejects_unknown_llm_profile_before_starting_steps(
    tmp_path, monkeypatch
) -> None:
    """Ett felskrivet profilnamn får inte starta en kostsam pipeline."""
    import config

    config_file = tmp_path / "llm_config.json"
    config_file.write_text(
        '{"profiles": {"Standard": {}}, "default": "Standard"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_FILE", config_file)
    calls: list[str] = []

    def record(name: str):
        def run(**_kwargs) -> int:
            calls.append(name)
            return 0

        return run

    options = PipelineOptions(
        root=tmp_path,
        inp=tmp_path / "files",
        txt=tmp_path / "text",
        skip_wpu=True,
        with_llm=True,
        profile="Saknas",
    )

    with pytest.raises(OperationFailed, match="Okänd LLM-konfiguration 'Saknas'.*"):
        run_pipeline(
            options,
            _context(),
            deps=PipelineDependencies(
                download=record("download"),
                ocr=record("ocr"),
                llm_correct=record("llm-correct"),
                quality=record("quality"),
                ingest=record("ingest"),
            ),
        )

    assert calls == []


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
    monkeypatch.setattr("operations.ocr.state_db.count_pages_for_stem", lambda conn, stem: 0)
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


def _fake_steps(calls: list[str], load_documents) -> PipelineDependencies:
    def record(step: str):
        def run(**kwargs) -> int:
            calls.append(step)
            return 0

        return run

    return PipelineDependencies(
        download=record("download"), download_wpu=record("download-wpu"),
        ocr=record("ocr"), ingest=record("ingest"), load_documents=load_documents,
    )


def test_pipeline_runs_load_documents_after_ingest() -> None:
    calls: list[str] = []

    def load(**kwargs) -> int:
        calls.append("load-documents")
        assert kwargs["text_dir"] == Path("/txt")
        assert kwargs["uri"] == "bolt://localhost:7687"
        return 0

    assert run_pipeline(_options(), _context(), deps=_fake_steps(calls, load)) == 0
    assert calls[-2:] == ["ingest", "load-documents"]


@pytest.mark.parametrize("error", [
    OSError("nätverk nere"),
    OperationFailed("Sätt NEO4J_PASSWORD"),
])
def test_pipeline_continues_when_documents_step_cannot_run(error, capsys) -> None:
    def load(**kwargs) -> int:
        raise error

    ctx = _context()
    assert run_pipeline(_options(), ctx, deps=_fake_steps([], load)) == 0
    cap = capsys.readouterr()
    out = cap.out + cap.err
    assert "Hoppar dokumentnoder:" in out
    assert "Pipeline klar." in out


def test_pipeline_continues_when_neo4j_is_down(capsys) -> None:
    ServiceUnavailable = pytest.importorskip("neo4j.exceptions").ServiceUnavailable

    def load(**kwargs) -> int:
        raise ServiceUnavailable("ingen anslutning")

    assert run_pipeline(_options(), _context(), deps=_fake_steps([], load)) == 0
    cap = capsys.readouterr()
    assert "Hoppar dokumentnoder:" in cap.out + cap.err


def test_pipeline_fails_when_documents_step_returns_nonzero() -> None:
    with pytest.raises(OperationFailed, match="Steget load-documents misslyckades"):
        run_pipeline(_options(), _context(), deps=_fake_steps([], lambda **kw: 1))


def test_pipeline_continues_when_neo4j_package_is_missing(capsys) -> None:
    """Saknat neo4j-paket (ModuleNotFoundError med name=neo4j) hoppar över steget."""
    def load(**kwargs) -> int:
        raise ModuleNotFoundError("No module named 'neo4j'", name="neo4j")

    assert run_pipeline(_options(), _context(), deps=_fake_steps([], load)) == 0
    cap = capsys.readouterr()
    assert "Hoppar dokumentnoder:" in cap.out + cap.err


def test_pipeline_fails_on_other_import_error() -> None:
    """Ett ImportError för någon annan modul är ett riktigt fel och fäller pipelinen."""
    def load(**kwargs) -> int:
        raise ImportError("cannot import name 'x' from 'graph.y'")

    with pytest.raises(ImportError):
        run_pipeline(_options(), _context(), deps=_fake_steps([], load))

    def load_other_module(**kwargs) -> int:
        raise ModuleNotFoundError("No module named 'lancedb'", name="lancedb")

    with pytest.raises(ModuleNotFoundError):
        run_pipeline(_options(), _context(), deps=_fake_steps([], load_other_module))


def test_pipeline_works_when_exceptions_module_cannot_be_imported(
        monkeypatch, capsys) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("neo4j"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    def load(**kwargs) -> int:
        raise OperationFailed("Sätt NEO4J_PASSWORD")

    assert run_pipeline(_options(), _context(), deps=_fake_steps([], load)) == 0
    cap = capsys.readouterr()
    assert "Hoppar dokumentnoder:" in cap.out + cap.err


def test_pipeline_continues_on_neo4j_auth_error(capsys) -> None:
    AuthError = pytest.importorskip("neo4j.exceptions").AuthError

    def load(**kwargs) -> int:
        raise AuthError("fel lösenord")

    assert run_pipeline(_options(), _context(), deps=_fake_steps([], load)) == 0
    cap = capsys.readouterr()
    assert "Hoppar dokumentnoder:" in cap.out + cap.err
