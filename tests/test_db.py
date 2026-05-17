import sqlite3
from pathlib import Path

import pytest

from db import connect, init_schema, SCHEMA_VERSION


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
