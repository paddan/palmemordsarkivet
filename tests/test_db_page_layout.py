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


def test_schema_version_at_least_11():
    assert SCHEMA_VERSION >= 11


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
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
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


# --- Historikskälet rejected (migration 013) och urvalsfrågor -----------------

def _archive(conn, reason, **kw):
    from db import archive_page_reading
    args = dict(pdf_stem="a", page_num=1, engine="surya", text="t", markdown=None,
                layout_json=None, engine_identity=None, score=50.0, reason=reason)
    args.update(kw)
    return archive_page_reading(conn, **args)


def test_rejected_reason_is_allowed(tmp_path):
    conn = _fresh(tmp_path)
    try:
        assert _archive(conn, "rejected") > 0
    finally:
        conn.close()


def test_migration_13_keeps_readings_and_allows_rejected(tmp_path):
    conn = _fresh(tmp_path)
    try:
        conn.execute("DROP TABLE pdf_page_readings")
        conn.execute(
            """CREATE TABLE pdf_page_readings (
                id INTEGER PRIMARY KEY, pdf_stem TEXT NOT NULL,
                page_num INTEGER NOT NULL, engine TEXT NOT NULL, text TEXT,
                markdown TEXT, layout_json TEXT, engine_identity TEXT, score REAL,
                recorded_at TEXT NOT NULL,
                reason TEXT NOT NULL CHECK(reason IN ('rescan', 'restore')))"""
        )
        conn.execute("DELETE FROM schema_version WHERE version > 12")
        conn.execute("PRAGMA user_version = 12")
        rid = _archive(conn, "rescan")
        init_schema(conn)
        row = conn.execute("SELECT * FROM pdf_page_readings WHERE id=?", (rid,)).fetchone()
        assert row is not None and row["reason"] == "rescan"
        assert _archive(conn, "rejected") > rid
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        conn.close()


def test_list_rescan_candidates_filters_and_sorts(tmp_path):
    from db import list_rescan_candidates, record_quality_page, upsert_pdf_file
    conn = _fresh(tmp_path)
    try:
        upsert_pdf_file(conn, pdf_stem="a", source="files", pdf_path="a.pdf")
        upsert_pdf_file(conn, pdf_stem="w", source="wpu", pdf_path="w.pdf")
        record_quality_page(conn, pdf_stem="a", page_num=1, score=65)
        record_quality_page(conn, pdf_stem="a", page_num=2, score=30)
        record_quality_page(conn, pdf_stem="a", page_num=3, score=80)
        record_quality_page(conn, pdf_stem="a", page_num=4, score=10, image_page=True)
        record_quality_page(conn, pdf_stem="w", page_num=1, score=40)
        rows = list_rescan_candidates(conn, threshold=70, source="files")
        assert [r["score"] for r in rows] == [30, 65]
        wpu = list_rescan_candidates(conn, threshold=70, source="wpu")
        assert [r["pdf_stem"] for r in wpu] == ["w"]
        assert len(list_rescan_candidates(conn, threshold=70, source=None)) == 3
    finally:
        conn.close()


def test_page_read_with_matches_engine_and_identity(tmp_path):
    from db import page_read_with
    conn = _fresh(tmp_path)
    try:
        record_page(conn, pdf_stem="a", page_num=1, engine="surya", text="x", score=90.0)
        _archive(conn, "rescan", engine="llm-vision", engine_identity="llm-vision:A:m")
        _archive(conn, "rescan", engine="llm", engine_identity=None)
        assert page_read_with(conn, "a", 1, engine="surya", engine_identity=None)
        assert page_read_with(conn, "a", 1, engine="llm-vision",
                              engine_identity="llm-vision:A:m")
        assert not page_read_with(conn, "a", 1, engine="llm-vision",
                                  engine_identity="llm-vision:B:m")
        assert not page_read_with(conn, "a", 2, engine="surya", engine_identity=None)
        conn.execute("DELETE FROM pdf_page_readings WHERE engine='llm-vision'")
        assert not page_read_with(conn, "a", 1, engine="llm-vision", engine_identity=None)
    finally:
        conn.close()
