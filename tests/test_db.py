import sqlite3
from pathlib import Path

import pytest

from db import (
    connect, init_schema, SCHEMA_VERSION,
    record_download, is_downloaded, find_download_by_sha1,
    upsert_pdf_file, get_pdf_file, mark_redaction_checked,
    mark_merged, mark_normalized, redaction_checked,
)


def _fresh(tmp_path):
    conn = connect(tmp_path / "state.db")
    init_schema(conn)
    return conn


def test_downloads_roundtrip(tmp_path):
    conn = _fresh(tmp_path)
    record_download(conn, source="files", drive_id="abc",
                    filename="00001-0001.pdf", sha1="deadbeef",
                    bytes_=1234)
    assert is_downloaded(conn, source="files", drive_id="abc")
    assert not is_downloaded(conn, source="files", drive_id="xyz")
    hit = find_download_by_sha1(conn, "deadbeef")
    assert hit["filename"] == "00001-0001.pdf"


def test_record_download_is_upsert(tmp_path):
    conn = _fresh(tmp_path)
    record_download(conn, source="files", drive_id="abc",
                    filename="a.pdf", sha1="x", bytes_=1)
    record_download(conn, source="files", drive_id="abc",
                    filename="a.pdf", sha1="x", bytes_=1, note="updated")
    rows = list(conn.execute("SELECT note FROM downloads"))
    assert len(rows) == 1
    assert rows[0]["note"] == "updated"


def test_record_download_requires_drive_id_or_url(tmp_path):
    conn = _fresh(tmp_path)
    with pytest.raises(ValueError):
        record_download(conn, source="files", filename="x.pdf")


def test_record_download_wpu_url(tmp_path):
    conn = _fresh(tmp_path)
    record_download(conn, source="wpu", url="https://wpu.nu/x",
                    filename="x.pdf", sha1="aaa", bytes_=100)
    assert is_downloaded(conn, source="wpu", url="https://wpu.nu/x")


def test_pdf_file_status_transitions(tmp_path):
    conn = _fresh(tmp_path)
    upsert_pdf_file(conn, pdf_stem="00001-0001", source="files",
                    pdf_path="downloaded/files/00001-0001.pdf")
    row = get_pdf_file(conn, "00001-0001")
    assert row["redaction_checked_at"] is None
    assert row["merged_at"] is None
    assert not redaction_checked(conn, "00001-0001")
    mark_redaction_checked(conn, "00001-0001", has_redactions=True)
    assert redaction_checked(conn, "00001-0001")
    mark_merged(conn, "00001-0001", text_mtime=123.0)
    mark_normalized(conn, "00001-0001", text_mtime=124.0)
    row = get_pdf_file(conn, "00001-0001")
    assert row["has_redactions"] == 1
    assert row["merged_at"] is not None
    assert row["normalized_at"] is not None
    assert row["text_mtime"] == 124.0


def test_state_db_env_override(tmp_path, monkeypatch):
    """connect() utan path ska respektera STATE_DB."""
    db_path = tmp_path / "env_state.db"
    monkeypatch.setenv("STATE_DB", str(db_path))
    conn = connect()
    init_schema(conn)
    conn.close()
    assert db_path.exists()


def test_connect_creates_file_with_wal(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect(db_path)
    try:
        cur = conn.execute("PRAGMA journal_mode")
        assert cur.fetchone()[0] == "wal"
        cur = conn.execute("PRAGMA foreign_keys")
        assert cur.fetchone()[0] == 1
    finally:
        conn.close()


def test_init_schema_is_idempotent(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect(db_path)
    try:
        init_schema(conn)
        init_schema(conn)
        version = conn.execute(
            "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
        ).fetchone()["version"]
        assert version == SCHEMA_VERSION
        tables = {
            r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"downloads", "pdf_files", "pdf_pages",
                "quality", "quality_pages", "ingest",
                "schema_version"}.issubset(tables)
    finally:
        conn.close()
