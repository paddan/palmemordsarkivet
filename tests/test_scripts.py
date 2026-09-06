"""Regressionstester för Python-entrypoints och borttagna shell-script."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

SHELL_TO_PYTHON = {
    "build_user_words.sh": "build_user_words.py",
    "detect_redactions.sh": "detect_redactions.py",
    "download.sh": "download.py",
    "download_wpu.sh": "download_wpu.py",
    "extract_entities.sh": "extract_entities.py",
    "extract_map_observations.sh": "extract_map_observations.py",
    "ingest.sh": "ingest.py",
    "install.sh": "install.py",
    "llm_config.sh": "llm_config.py",
    "llm_correct.sh": "llm_correct.py",
    "load_graph.sh": "load_graph.py",
    "merge_pages.sh": "merge_pages.py",
    "merge_wpu.sh": "merge_wpu.py",
    "neo4j.sh": "neo4j.py",
    "normalize.sh": "normalize.py",
    "ocr.sh": "ocr.py",
    "ocr_pages.sh": "ocr_pages.py",
    "ocr_tesseract.sh": "ocr_tesseract.py",
    "quality.sh": "quality.py",
    "run_pipeline.sh": "run_pipeline.py",
    "setup_tessdata.sh": "setup_tessdata.py",
    "test.sh": "test.py",
    "web.sh": "web.py",
}


def test_shell_scripts_are_removed() -> None:
    # web.sh och neo4j.sh är användarbegärda tunna Python-genvägar.
    for shell in SHELL_TO_PYTHON:
        if shell in ("web.sh", "neo4j.sh"):
            continue
        assert not (PROJECT_ROOT / shell).exists(), f"{shell} ska vara borttagen"


def test_python_replacements_exist() -> None:
    for py in SHELL_TO_PYTHON.values():
        path = PROJECT_ROOT / "scripts" / py
        assert path.exists(), f"{py} saknas"


def test_web_shell_shortcut_forwards_to_python_entrypoint() -> None:
    path = PROJECT_ROOT / "web.sh"

    result = subprocess.run(
        [str(path), "--help"],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "Användning: scripts/web.py [-- streamlit-flaggor...]\n"


def test_representative_entrypoints_answer_help() -> None:
    for name in ("run_pipeline.py", "ocr.py", "ingest.py", "quality.py", "download.py"):
        path = PROJECT_ROOT / "scripts" / name
        result = subprocess.run(
            [sys.executable, str(path), "--help"],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, f"{name} --help: {result.stderr}"


# ---------------------------------------------------------------------------
# Flaggeparitet: varje legacy-flagga ska finnas i registryns parametrar.
# ---------------------------------------------------------------------------

EXPECTED_OPERATION_FLAGS = {
    "ocr": (
        "--skip-redo", "--fallback-failed", "--redo", "--mode", "--source",
        "--no-update-pdf", "--in", "--ocr", "--txt", "--pages-out",
        "--jobs", "--per-file-jobs", "--threshold", "--from-list",
        "--retry-failed",
    ),
    "ocr-tesseract": (
        "--in", "--ocr", "--txt", "--tessdata", "--user-words",
        "--user-words-auto", "--tess-config", "--psm", "--langs",
        "--jobs", "--per-file-jobs", "--min-text-chars", "--image-dpi",
        "--errors-log", "--files-from", "--retry-failed", "--retry-blacklist",
    ),
    "ingest": (
        "--rebuild", "--limit", "--text-dir", "--db-dir", "--chunk-chars",
        "--chunk-overlap", "--model", "--unusable-list", "--reindex-since",
    ),
}


def test_operation_flag_parity_with_legacy_scripts() -> None:
    from operations.registry import get_registry

    registry = get_registry()
    for operation_id, expected in EXPECTED_OPERATION_FLAGS.items():
        flags = {
            flag
            for parameter in registry.get(operation_id).parameters
            for flag in parameter.flags
        }
        missing = [flag for flag in expected if flag not in flags]
        assert not missing, f"{operation_id} saknar flaggor i registryn: {missing}"


def test_legacy_migration_files_are_removed() -> None:
    project_root = Path(__file__).resolve().parents[1]
    for path in (
        project_root / "src" / "migrate_to_db.py",
        project_root / "migrate_to_db.sh",
        project_root / "cleanup_legacy_state.sh",
    ):
        assert not path.exists(), f"{path} ska vara borttagen"


def _run_setup_tessdata(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "setup_tessdata.py"), *args],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_setup_tessdata_unknown_flag_exits_2() -> None:
    result = _run_setup_tessdata("--okänd-flagga")
    assert result.returncode == 2
    assert "Traceback" not in result.stderr


def test_setup_tessdata_root_without_value_is_clean_error() -> None:
    # Regression: tidigare IndexError-traceback när --root saknade värde.
    result = _run_setup_tessdata("--root")
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert "--root" in result.stderr


def test_setup_tessdata_dest_without_value_is_clean_error() -> None:
    result = _run_setup_tessdata("--dest")
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert "--dest" in result.stderr


def test_setup_tessdata_help_exits_0() -> None:
    result = _run_setup_tessdata("--help")
    assert result.returncode == 0
    assert "--root" in result.stdout


def test_graph_page_handles_missing_link_analysis_import() -> None:
    project_root = Path(__file__).resolve().parents[1]
    text = (project_root / "src" / "pages" / "3_Graf.py").read_text(encoding="utf-8")
    assert "except ImportError" in text
    assert "st-link-analysis" in text


def test_graph_page_accepts_casebook_centers_query_param() -> None:
    project_root = Path(__file__).resolve().parents[1]
    text = (project_root / "src" / "pages" / "3_Graf.py").read_text(encoding="utf-8")
    assert "decode_graph_centers_param" in text
    assert 'st.query_params.get("centers")' in text
    assert "linked_centers" in text


def test_compare_page_installs_shared_pdf_opener_for_citation_links() -> None:
    project_root = Path(__file__).resolve().parents[1]
    text = (project_root / "src" / "pages" / "6_Jämförelse.py").read_text(encoding="utf-8")
    assert "_casebook_ui.render_pdf_opener(ROOT)" in text


def test_utredning_installs_shared_pdf_opener_for_citation_links() -> None:
    project_root = Path(__file__).resolve().parents[1]
    text = (project_root / "src" / "Utredning.py").read_text(encoding="utf-8")
    assert "_casebook_ui.render_pdf_opener(ROOT)" in text
    assert "urlsafe_b64decode" not in text


def test_casebook_page_installs_shared_pdf_opener_for_saved_answers() -> None:
    project_root = Path(__file__).resolve().parents[1]
    text = (project_root / "src" / "casebook_ui.py").read_text(encoding="utf-8")
    page_block = text.split("def render_casebook_page", 1)[1].split(
        "def _render_saved_answers", 1
    )[0]
    assert "render_pdf_opener(root)" in page_block


def test_utredning_sidebar_hides_rag_only_controls_in_mcp_mode() -> None:
    project_root = Path(__file__).resolve().parents[1]
    text = (project_root / "src" / "Utredning.py").read_text(encoding="utf-8")

    marker = (
        'if not mcp_mode:\n'
        '        do_rerank = st.toggle(\n'
        '            "Använd cross-encoder reranker"'
    )
    assert marker in text
    rag_block = text.split(marker, 1)[1].split('if backend["kind"] == "claude":', 1)[0]

    for label in (
        "Hämta top-K kandidater",
        "Skicka top-N till AI",
        "Sökfilter",
        "Begränsa till entiteter",
        "OCR-tolerant fuzzy-sökning",
    ):
        assert label in rag_block

    mcp_sidebar_block = text.split('mcp_mode = st.toggle(', 1)[1].split(marker, 1)[0]
    assert "Visa kunskapsgraf" in mcp_sidebar_block


# ---------------------------------------------------------------------------
# ocr_db_helper.py — text_mtime-stämpling
# ---------------------------------------------------------------------------

def _run_helper(args: list[str], db_path: Path) -> subprocess.CompletedProcess:
    import os
    import sys
    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["STATE_DB"] = str(db_path)
    return subprocess.run(
        [sys.executable, str(project_root / "src" / "ocr_db_helper.py"), *args],
        capture_output=True, text=True, env=env,
    )


def _db_row(db_path: Path, stem: str):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    import db
    conn = db.connect(db_path)
    return conn.execute(
        "SELECT tesseract_done_at, text_mtime FROM pdf_files WHERE pdf_stem=?",
        (stem,),
    ).fetchone()


def test_mark_done_with_txt_path_stamps_text_mtime(tmp_path: Path) -> None:
    """mark-done med txt-sökväg ska stämpla text_mtime så att
    normalize/quality-deltat ser filen."""
    txt = tmp_path / "doc.txt"
    txt.write_text("ocr-text", encoding="utf-8")
    db_path = tmp_path / "state.db"
    r = _run_helper(["mark-done", "doc", str(tmp_path / "doc.pdf"), str(txt)], db_path)
    assert r.returncode == 0, r.stderr
    row = _db_row(db_path, "doc")
    assert row["tesseract_done_at"] is not None
    assert row["text_mtime"] == txt.stat().st_mtime


def test_mark_done_without_txt_path_still_works(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    r = _run_helper(["mark-done", "doc", str(tmp_path / "doc.pdf")], db_path)
    assert r.returncode == 0, r.stderr
    row = _db_row(db_path, "doc")
    assert row["tesseract_done_at"] is not None
    assert row["text_mtime"] is None


def test_touch_mtime_command(tmp_path: Path) -> None:
    """touch-mtime ska uppdatera text_mtime för befintlig rad (används av
    ocr.sh --redo --mode files efter om-OCR)."""
    txt = tmp_path / "doc.txt"
    txt.write_text("ny text", encoding="utf-8")
    db_path = tmp_path / "state.db"
    _run_helper(["mark-done", "doc", str(tmp_path / "doc.pdf")], db_path)
    r = _run_helper(["touch-mtime", "doc", str(txt)], db_path)
    assert r.returncode == 0, r.stderr
    row = _db_row(db_path, "doc")
    assert row["text_mtime"] == txt.stat().st_mtime
