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
