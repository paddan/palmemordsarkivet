"""Regressionstester för pipeline-wrappers."""

from __future__ import annotations

import subprocess
from pathlib import Path


def _stub(root: Path, name: str) -> None:
    path = root / name
    path.write_text(
        f"#!/bin/bash\nprintf '%s\\n' {name!r} >> {str(root / 'calls')!r}\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def test_run_pipeline_resumes_pending_steps_without_new_downloads(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    script = tmp_path / "run_pipeline.sh"
    script.write_text(
        (project_root / "run_pipeline.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    script.chmod(0o755)

    for name in ("download.sh", "download_wpu.sh", "ocr.sh", "ingest.sh"):
        _stub(tmp_path, name)

    result = subprocess.run(
        [str(script)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls").read_text(encoding="utf-8").splitlines()
    assert "ocr.sh" in calls
    assert "ingest.sh" in calls


def test_run_pipeline_refreshes_quality_after_llm(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    script = tmp_path / "run_pipeline.sh"
    script.write_text(
        (project_root / "run_pipeline.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    script.chmod(0o755)

    for name in ("download.sh", "ocr.sh", "llm_correct.sh", "quality.sh", "ingest.sh"):
        _stub(tmp_path, name)

    result = subprocess.run(
        [str(script), "--skip-wpu", "--with-llm", "--skip-redo"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls").read_text(encoding="utf-8").splitlines()
    assert calls.index("llm_correct.sh") < calls.index("quality.sh") < calls.index("ingest.sh")


def test_ocr_refreshes_per_page_quality_after_surya() -> None:
    project_root = Path(__file__).resolve().parents[1]
    text = (project_root / "ocr.sh").read_text(encoding="utf-8")
    after_surya = text.split('step "7/7  Uppdaterad kvalitetsbedömning"', 1)[1]
    assert "./quality.sh --per-page" in after_surya


def test_quality_help_mentions_state_db_not_legacy_files() -> None:
    project_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [str(project_root / "quality.sh"), "--help"],
        cwd=project_root,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert "state.db" in result.stdout
    assert "quality.csv" not in result.stdout
    assert "quality_pages.jsonl" not in result.stdout
    assert "--out FILE" not in result.stdout
    assert "--pages-out FILE" not in result.stdout


def test_download_wpu_help_only_lists_supported_flags() -> None:
    project_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [str(project_root / "download_wpu.sh"), "--help"],
        cwd=project_root,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert "--dry-run" in result.stdout
    assert "--limit" in result.stdout
    assert "id-only" not in result.stdout
    assert "da-only" not in result.stdout


def test_install_help_mentions_dev_flag() -> None:
    project_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [str(project_root / "install.sh"), "--help"],
        cwd=project_root,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert "--dev" in result.stdout
    assert "pytest, ruff och mypy" in result.stdout


def test_test_sh_makes_static_tools_opt_in() -> None:
    project_root = Path(__file__).resolve().parents[1]
    text = (project_root / "test.sh").read_text(encoding="utf-8")
    assert "--static" in text
    assert "RUN_STATIC=false" in text
    assert '.venv/bin/$tool' in text
    assert 'command -v "$tool"' not in text


def test_legacy_migration_files_are_removed() -> None:
    project_root = Path(__file__).resolve().parents[1]
    for path in (
        project_root / "src" / "migrate_to_db.py",
        project_root / "migrate_to_db.sh",
        project_root / "cleanup_legacy_state.sh",
    ):
        assert not path.exists(), f"{path} ska vara borttagen"


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
