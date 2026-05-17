import sqlite3
import threading
import time
from pathlib import Path

import pytest

from db import (
    connect, init_schema, SCHEMA_VERSION,
    record_download, is_downloaded, find_download_by_sha1,
    upsert_pdf_file, get_pdf_file, mark_redaction_checked,
    mark_merged, mark_normalized, redaction_checked,
    record_page, page_exists, get_pages_for_stem,
    record_quality, record_quality_page, get_bad_pages,
    record_ingest, get_ingested_mtime,
    mark_llm_corrected, llm_corrected,
    mark_wpu_decided, wpu_decided,
    files_needing_normalize, files_needing_quality, files_needing_ingest,
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


def test_mark_functions_raise_on_missing_stem(tmp_path):
    conn = _fresh(tmp_path)
    with pytest.raises(KeyError):
        mark_redaction_checked(conn, "nonexistent", has_redactions=True)
    with pytest.raises(KeyError):
        mark_merged(conn, "nonexistent", text_mtime=1.0)
    with pytest.raises(KeyError):
        mark_normalized(conn, "nonexistent", text_mtime=1.0)


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


def test_pages_roundtrip(tmp_path):
    conn = _fresh(tmp_path)
    upsert_pdf_file(conn, pdf_stem="s1", source="files",
                    pdf_path="downloaded/files/s1.pdf")
    record_page(conn, pdf_stem="s1", page_num=1, engine="tesseract",
                text="hej", score=80.0)
    assert page_exists(conn, "s1", 1)
    assert not page_exists(conn, "s1", 2)
    pages = get_pages_for_stem(conn, "s1")
    assert [p["page_num"] for p in pages] == [1]


def test_record_page_is_upsert(tmp_path):
    conn = _fresh(tmp_path)
    upsert_pdf_file(conn, pdf_stem="s1", source="files", pdf_path="x")
    record_page(conn, pdf_stem="s1", page_num=1, engine="tesseract",
                text="a", score=50.0)
    record_page(conn, pdf_stem="s1", page_num=1, engine="surya",
                text="b", score=90.0)
    pages = get_pages_for_stem(conn, "s1")
    assert pages[0]["engine"] == "surya"
    assert pages[0]["text"] == "b"


def test_quality_and_delta(tmp_path):
    conn = _fresh(tmp_path)
    upsert_pdf_file(conn, pdf_stem="s1", source="files", pdf_path="x")
    mark_merged(conn, "s1", text_mtime=100.0)
    assert "s1" in files_needing_quality(conn)
    record_quality(conn, pdf_stem="s1", score=70.0, chars=1000,
                   text_mtime=100.0, extras={"pct_swe": 0.9})
    assert "s1" not in files_needing_quality(conn)
    mark_merged(conn, "s1", text_mtime=200.0)
    assert "s1" in files_needing_quality(conn)


def test_ingest_delta(tmp_path):
    conn = _fresh(tmp_path)
    upsert_pdf_file(conn, pdf_stem="s1", source="files", pdf_path="x")
    mark_merged(conn, "s1", text_mtime=100.0)
    assert "s1" in files_needing_ingest(conn)
    record_ingest(conn, pdf_stem="s1", text_mtime=100.0, chunks=5)
    assert "s1" not in files_needing_ingest(conn)
    assert get_ingested_mtime(conn, "s1") == 100.0


def test_files_needing_normalize(tmp_path):
    conn = _fresh(tmp_path)
    upsert_pdf_file(conn, pdf_stem="s1", source="files", pdf_path="x")
    # Ingen text_mtime → ska INTE behöva normalize
    assert "s1" not in files_needing_normalize(conn)
    mark_merged(conn, "s1", text_mtime=time.time())
    # Nu finns text_mtime men inget normalized_at → behöver normalize
    assert "s1" in files_needing_normalize(conn)
    mark_normalized(conn, "s1", text_mtime=time.time())
    # Nu är normalized_at >= text_mtime → behöver inte
    assert "s1" not in files_needing_normalize(conn)


def test_bad_pages(tmp_path):
    conn = _fresh(tmp_path)
    upsert_pdf_file(conn, pdf_stem="s1", source="files", pdf_path="x")
    record_quality_page(conn, pdf_stem="s1", page_num=1, score=20.0)
    record_quality_page(conn, pdf_stem="s1", page_num=2, score=80.0)
    record_quality_page(conn, pdf_stem="s1", page_num=3, score=10.0,
                        image_page=True)
    bad = get_bad_pages(conn, threshold=50.0)
    # page 3 är image_page → filtreras bort
    assert [(b["pdf_stem"], b["page_num"]) for b in bad] == [("s1", 1)]


def test_llm_corrections(tmp_path):
    conn = _fresh(tmp_path)
    assert not llm_corrected(conn, "s1", 1)
    mark_llm_corrected(conn, "s1", 1)
    assert llm_corrected(conn, "s1", 1)
    mark_llm_corrected(conn, "s1", 1)  # idempotent
    n = conn.execute("SELECT COUNT(*) FROM llm_corrections").fetchone()[0]
    assert n == 1


def test_wpu_decisions(tmp_path):
    conn = _fresh(tmp_path)
    assert not wpu_decided(conn, "s1")
    mark_wpu_decided(conn, "s1")
    assert wpu_decided(conn, "s1")
    mark_wpu_decided(conn, "s1")  # idempotent
    n = conn.execute("SELECT COUNT(*) FROM wpu_decisions").fetchone()[0]
    assert n == 1


def test_parallel_page_writes(tmp_path):
    """4 trådar skriver 25 sidor var — ska inte krascha eller tappa data."""
    db_path = tmp_path / "state.db"
    init_conn = connect(db_path)
    init_schema(init_conn)
    upsert_pdf_file(init_conn, pdf_stem="s1", source="files", pdf_path="x")
    init_conn.close()

    errors = []

    def worker(page_range):
        try:
            c = connect(db_path)
            for n in page_range:
                record_page(c, pdf_stem="s1", page_num=n,
                            engine="tesseract", text=f"p{n}", score=80.0)
            c.close()
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(range(i*25+1, i*25+26),))
               for i in range(4)]
    for t in threads: t.start()
    for t in threads: t.join()

    assert not errors
    c = connect(db_path)
    n = c.execute("SELECT COUNT(*) FROM pdf_pages").fetchone()[0]
    assert n == 100


def test_source_for_path(tmp_path):
    from db import source_for_path
    # Path-komponent 'wpu_files' → wpu
    assert source_for_path("/x/downloaded/wpu_files/a.pdf") == "wpu"
    assert source_for_path("/x/downloaded/files/a.pdf") == "files"
    # Filnamn som råkar innehålla "wpu" men ligger i files/ → "files"
    assert source_for_path("/x/downloaded/files/wpu-data.pdf") == "files"
    assert source_for_path("/x/Wpunkt.pdf") == "files"

    # .txt-fallback: leta efter matchande PDF under root
    root = tmp_path
    (root / "downloaded" / "wpu_files").mkdir(parents=True)
    (root / "downloaded" / "files").mkdir(parents=True)
    (root / "downloaded" / "wpu_files" / "doc1.pdf").write_bytes(b"")
    (root / "generated" / "text").mkdir(parents=True)
    txt1 = root / "generated" / "text" / "doc1.txt"
    txt1.write_text("x")
    assert source_for_path(txt1, root=root) == "wpu"
    # Saknar wpu-PDF → default 'files'
    txt2 = root / "generated" / "text" / "doc2.txt"
    txt2.write_text("x")
    assert source_for_path(txt2, root=root) == "files"
    # Utan root → 'files'
    assert source_for_path(txt1) == "files"
