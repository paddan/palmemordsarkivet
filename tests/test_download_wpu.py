"""Tester för wpu.nu-nedladdaren."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import db as state_db
import download_wpu


def test_display_out_dir_allows_absolute_outside_root() -> None:
    assert download_wpu._display_out_dir(Path("/tmp/wpu-test")).startswith("/tmp/")


def test_successful_wpu_download_is_recorded_in_state_db(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "state.db"
    out_dir = tmp_path / "wpu"
    monkeypatch.setenv("STATE_DB", str(db_path))
    monkeypatch.setattr(
        download_wpu,
        "fetch_all_wpu_files",
        lambda: [{"name": "DA14259-00.pdf", "url": "https://wpu.nu/wiki/Fil:DA14259-00.pdf", "size": 123}],
    )

    def fake_download(_session, _url: str, dest: Path) -> None:
        dest.write_bytes(b"%PDF-1.7\n")

    monkeypatch.setattr(download_wpu, "_download", fake_download)
    monkeypatch.setattr(sys, "argv", ["download_wpu.py", "--out", str(out_dir)])

    assert download_wpu.main() == 0

    conn = state_db.connect(db_path)
    state_db.init_schema(conn)
    row = conn.execute(
        "SELECT source, url, filename, bytes FROM downloads WHERE source='wpu'"
    ).fetchone()
    assert row is not None
    assert row["url"] == "https://wpu.nu/wiki/Fil:DA14259-00.pdf"
    assert row["filename"] == "DA14259-00.pdf"
    assert row["bytes"] == 123

    pdf = conn.execute(
        "SELECT source, pdf_path FROM pdf_files WHERE pdf_stem='DA14259-00'"
    ).fetchone()
    assert pdf is not None
    assert pdf["source"] == "wpu"
    assert pdf["pdf_path"] == str(out_dir / "DA14259-00.pdf")


def test_existing_wpu_file_is_backfilled_to_state_db(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "state.db"
    out_dir = tmp_path / "wpu"
    out_dir.mkdir()
    existing = out_dir / "DA14259-00.pdf"
    existing.write_bytes(b"%PDF-1.7\n")
    monkeypatch.setenv("STATE_DB", str(db_path))
    monkeypatch.setattr(
        download_wpu,
        "fetch_all_wpu_files",
        lambda: [{"name": "DA14259-00.pdf", "url": "https://wpu.nu/wiki/Fil:DA14259-00.pdf", "size": 123}],
    )

    def fail_download(_session, _url: str, _dest: Path) -> None:
        raise AssertionError("redan lokala filer ska inte laddas ner igen")

    monkeypatch.setattr(download_wpu, "_download", fail_download)
    monkeypatch.setattr(sys, "argv", ["download_wpu.py", "--out", str(out_dir)])

    assert download_wpu.main() == 0

    conn = state_db.connect(db_path)
    state_db.init_schema(conn)
    row = conn.execute(
        "SELECT source, url, filename, bytes FROM downloads WHERE source='wpu'"
    ).fetchone()
    assert row is not None
    assert row["url"] == "https://wpu.nu/wiki/Fil:DA14259-00.pdf"
    assert row["filename"] == "DA14259-00.pdf"
    assert row["bytes"] == 123

    pdf = conn.execute(
        "SELECT source, pdf_path FROM pdf_files WHERE pdf_stem='DA14259-00'"
    ).fetchone()
    assert pdf is not None
    assert pdf["source"] == "wpu"
    assert pdf["pdf_path"] == str(existing)
