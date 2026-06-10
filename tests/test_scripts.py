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
