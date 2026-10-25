"""Markdown, layout och motoridentitet per sida i pdf_pages (migration 011)."""
import sqlite3
from pathlib import Path

from db import (
    SCHEMA_VERSION,
    connect,
    count_pages_for_stem,
    get_page_markdown_map,
    get_pages_for_stem,
    init_schema,
    record_page,
)

LAYOUT_COLUMNS = {"markdown", "layout_json", "engine_identity"}


def _columns(conn):
    return {row[1] for row in conn.execute("PRAGMA table_info(pdf_pages)")}


def _fresh(tmp_path):
    conn = connect(tmp_path / "state.db")
    init_schema(conn)
    return conn


def test_schema_version_is_11():
    assert SCHEMA_VERSION == 11


def test_migration_011_adds_layout_columns(tmp_path):
    db_path = tmp_path / "legacy_v4.db"
    fixture = Path(__file__).parent / "fixtures" / "state_db_v4.sql"
    raw = sqlite3.connect(db_path)
    try:
        raw.executescript(fixture.read_text(encoding="utf-8"))
        raw.commit()
    finally:
        raw.close()

    conn = connect(db_path)
    try:
        init_schema(conn)
        assert _columns(conn) >= LAYOUT_COLUMNS
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 11
    finally:
        conn.close()


def test_fresh_and_migrated_pdf_pages_have_same_columns(tmp_path):
    fresh = _fresh(tmp_path)
    db_path = tmp_path / "legacy_v4.db"
    fixture = Path(__file__).parent / "fixtures" / "state_db_v4.sql"
    raw = sqlite3.connect(db_path)
    try:
        raw.executescript(fixture.read_text(encoding="utf-8"))
        raw.commit()
    finally:
        raw.close()
    migrated = connect(db_path)
    try:
        init_schema(migrated)
        assert _columns(fresh) == _columns(migrated)
    finally:
        fresh.close()
        migrated.close()


def test_record_page_stores_markdown(tmp_path):
    conn = _fresh(tmp_path)
    try:
        record_page(
            conn, pdf_stem="a", page_num=1, engine="surya", text="Hej", score=90.0,
            markdown="# Hej", layout_json='{"blocks": []}',
            engine_identity="surya-layout-2",
        )
        row = get_pages_for_stem(conn, "a")[0]
        assert row["markdown"] == "# Hej"
        assert row["layout_json"] == '{"blocks": []}'
        assert row["engine_identity"] == "surya-layout-2"
    finally:
        conn.close()


def test_rerecord_without_markdown_clears_it(tmp_path):
    conn = _fresh(tmp_path)
    try:
        record_page(
            conn, pdf_stem="a", page_num=1, engine="surya", text="Hej", score=90.0,
            markdown="# Hej", layout_json="{}", engine_identity="x",
        )
        record_page(
            conn, pdf_stem="a", page_num=1, engine="tesseract", text="Hej", score=80.0,
        )
        row = get_pages_for_stem(conn, "a")[0]
        assert row["markdown"] is None
        assert row["layout_json"] is None
        assert row["engine_identity"] is None
    finally:
        conn.close()


def test_get_page_markdown_map(tmp_path):
    conn = _fresh(tmp_path)
    try:
        record_page(conn, pdf_stem="a", page_num=1, engine="surya", text="ett",
                    score=1.0, markdown="**ett**")
        record_page(conn, pdf_stem="a", page_num=2, engine="tesseract", text="två",
                    score=1.0)
        record_page(conn, pdf_stem="b", page_num=1, engine="surya", text="annan",
                    score=1.0, markdown="annan md")
        assert get_page_markdown_map(conn, "a") == {1: ("ett", "**ett**")}
        assert get_page_markdown_map(conn, "saknas") == {}
    finally:
        conn.close()


def test_count_pages_for_stem(tmp_path):
    conn = _fresh(tmp_path)
    assert count_pages_for_stem(conn, "doc") == 0
    record_page(conn, pdf_stem="doc", page_num=1, engine="tesseract", text="a", score=50.0)
    record_page(conn, pdf_stem="doc", page_num=2, engine="surya-layout", text="b", score=50.0)
    record_page(conn, pdf_stem="annan", page_num=1, engine="tesseract", text="c", score=50.0)
    assert count_pages_for_stem(conn, "doc") == 2
