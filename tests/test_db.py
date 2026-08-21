import sqlite3
import threading
import time
from pathlib import Path

import pytest

from db import (
    SCHEMA_VERSION,
    approve_map_observation_candidate,
    clear_ocr_failures,
    clear_tesseract_blacklisted,
    connect,
    delete_casebook_entry,
    delete_map_observation,
    delete_map_place,
    delete_source_annotation,
    delete_source_bookmark,
    files_needing_ingest,
    files_needing_normalize,
    files_needing_quality,
    find_download_by_sha1,
    get_bad_pages,
    get_ingested_mtime,
    get_pages_for_stem,
    get_pdf_file,
    init_schema,
    is_downloaded,
    is_ocr_fully_failed,
    is_tesseract_blacklisted,
    list_casebook_entries,
    list_map_observation_candidates,
    list_map_observations,
    list_map_places,
    list_source_annotations,
    list_source_bookmarks,
    llm_corrected,
    mark_llm_corrected,
    mark_merged,
    mark_normalized,
    mark_redaction_checked,
    mark_surya_failed,
    mark_tesseract_blacklisted,
    mark_tesseract_done,
    mark_tesseract_failed,
    mark_wpu_decided,
    page_exists,
    record_casebook_entry,
    record_download,
    record_ingest,
    record_map_observation,
    record_map_observation_candidate,
    record_map_place,
    record_page,
    record_quality,
    record_quality_page,
    record_source_annotation,
    record_source_bookmark,
    redaction_checked,
    reject_map_observation_candidate,
    retry_tesseract_blacklisted,
    schema_version,
    seed_map_data_if_empty,
    touch_text_mtime,
    update_map_observation,
    update_map_observation_candidate,
    update_source_annotation,
    upsert_pdf_file,
    wpu_decided,
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


def test_init_schema_migrates_legacy_v4_fixture(tmp_path):
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

        assert schema_version(conn) == SCHEMA_VERSION
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        migrated = get_pdf_file(conn, "00001-0001")
        assert migrated["pdf_path"] == "downloaded/files/00001-0001.pdf"
        assert migrated["tesseract_failed"] == 0
        assert migrated["tesseract_done_at"] is None
        versions = [
            row["version"]
            for row in conn.execute(
                "SELECT version FROM schema_version ORDER BY version"
            )
        ]
        assert versions == [4, SCHEMA_VERSION]
    finally:
        conn.close()


def test_init_schema_migrates_v5_database_missing_surya_column(tmp_path):
    db_path = tmp_path / "legacy_v5_missing_surya.db"
    fixture = Path(__file__).parent / "fixtures" / "state_db_v5_missing_surya.sql"
    raw = sqlite3.connect(db_path)
    try:
        raw.executescript(fixture.read_text(encoding="utf-8"))
        raw.commit()
    finally:
        raw.close()

    conn = connect(db_path)
    try:
        init_schema(conn)

        assert schema_version(conn) == SCHEMA_VERSION
        migrated = get_pdf_file(conn, "wpu-legacy")
        assert migrated["tesseract_failed"] == 1
        assert migrated["tesseract_blacklisted_at"] == "2026-07-01T00:10:00+00:00"
        assert migrated["surya_failed_at"] is None
    finally:
        conn.close()


def test_init_schema_rejects_newer_database(tmp_path):
    conn = connect(tmp_path / "future.db")
    try:
        conn.execute(
            "CREATE TABLE schema_version(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO schema_version(version, applied_at) VALUES (?, '2026-07-18T00:00:00+00:00')",
            (SCHEMA_VERSION + 1,),
        )
        conn.commit()

        with pytest.raises(RuntimeError, match="nyare schema-version"):
            init_schema(conn)
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


def test_casebook_entries_roundtrip_with_sources_and_entities(tmp_path):
    conn = _fresh(tmp_path)
    entry_id = record_casebook_entry(
        conn,
        question="Vem nämner Skandia?",
        answer="Skandia nämns i flera förhör.",
        mode="rag",
        backend="Claude",
        model="claude-sonnet-4-6",
        sources=[
            {"source": "100 — Skandia.txt", "page": 28, "nr": "100", "titel": "Skandia"},
            {"source": "865 — Brev.txt", "page": 1, "nr": "865", "titel": "Brev"},
        ],
        entities=[
            {"namn": "Skandia", "label": "Organisation", "norm": "skandia"},
        ],
        note="Arbetsspår",
    )

    entries = list_casebook_entries(conn)

    assert len(entries) == 1
    assert entries[0]["id"] == entry_id
    assert entries[0]["question"] == "Vem nämner Skandia?"
    assert entries[0]["mode"] == "rag"
    assert entries[0]["sources"][0]["source"] == "100 — Skandia.txt"
    assert entries[0]["entities"][0]["namn"] == "Skandia"
    assert entries[0]["note"] == "Arbetsspår"


def test_casebook_entries_are_newest_first_and_deletable(tmp_path):
    conn = _fresh(tmp_path)
    first = record_casebook_entry(
        conn,
        question="Första frågan",
        answer="Första svaret",
        mode="mcp",
        backend="Claude",
        model="claude-sonnet-4-6",
        sources=[],
    )
    second = record_casebook_entry(
        conn,
        question="Andra frågan",
        answer="Andra svaret",
        mode="rag",
        backend="OpenAI",
        model="gpt-4o-mini",
        sources=[],
    )

    assert [e["id"] for e in list_casebook_entries(conn)] == [second, first]
    assert delete_casebook_entry(conn, first) is True
    assert delete_casebook_entry(conn, first) is False
    assert [e["id"] for e in list_casebook_entries(conn)] == [second]


def test_source_bookmarks_upsert_and_delete(tmp_path):
    conn = _fresh(tmp_path)
    first = record_source_bookmark(
        conn,
        source="100 — Skandia.txt",
        page=28,
        nr="100",
        title="Skandia",
        note="Kontrollera tidslinjen",
    )
    again = record_source_bookmark(
        conn,
        source="100 — Skandia.txt",
        page=28,
        nr="100",
        title="Skandia",
        note="Viktigare än först tänkt",
    )

    bookmarks = list_source_bookmarks(conn)

    assert again == first
    assert len(bookmarks) == 1
    assert bookmarks[0]["source"] == "100 — Skandia.txt"
    assert bookmarks[0]["page"] == 28
    assert bookmarks[0]["note"] == "Viktigare än först tänkt"
    assert delete_source_bookmark(conn, first) is True
    assert delete_source_bookmark(conn, first) is False
    assert list_source_bookmarks(conn) == []


def test_source_annotations_roundtrip(tmp_path):
    conn = _fresh(tmp_path)
    first = record_source_annotation(
        conn,
        source="100 — Skandia.txt",
        page=28,
        nr="100",
        title="Skandia",
        quote="vid 23-tiden lämnade han kontoret",
        note="Stämmer detta med Engströms uppgift?",
    )
    # Flera anteckningar per källa/sida tillåts (till skillnad från bokmärken).
    second = record_source_annotation(
        conn,
        source="100 — Skandia.txt",
        page=28,
        note="Andra anteckningen samma sida",
    )
    assert second != first

    notes = list_source_annotations(conn)
    assert len(notes) == 2
    # Senast skapad först.
    assert notes[0]["note"] == "Andra anteckningen samma sida"
    assert notes[1]["quote"] == "vid 23-tiden lämnade han kontoret"
    assert notes[1]["page"] == 28

    # Filtrera på källa.
    record_source_annotation(conn, source="200 — Annat.txt", note="annan källa")
    only = list_source_annotations(conn, source="100 — Skandia.txt")
    assert len(only) == 2
    assert {n["source"] for n in only} == {"100 — Skandia.txt"}

    assert update_source_annotation(conn, first, note="Reviderad anteckning") is True
    reread = list_source_annotations(conn, source="100 — Skandia.txt")
    assert any(n["note"] == "Reviderad anteckning" for n in reread)

    assert delete_source_annotation(conn, first) is True
    assert delete_source_annotation(conn, first) is False
    assert len(list_source_annotations(conn, source="100 — Skandia.txt")) == 1


def test_source_annotation_requires_source_and_note(tmp_path):
    conn = _fresh(tmp_path)
    with pytest.raises(ValueError):
        record_source_annotation(conn, source="  ", note="x")
    with pytest.raises(ValueError):
        record_source_annotation(conn, source="a.txt", note="   ")


def test_tesseract_blacklist(tmp_path):
    conn = _fresh(tmp_path)
    upsert_pdf_file(conn, pdf_stem="bad-pdf", source="wpu",
                    pdf_path="downloaded/wpu_files/bad-pdf.pdf")
    upsert_pdf_file(conn, pdf_stem="ok-pdf", source="wpu",
                    pdf_path="downloaded/wpu_files/ok-pdf.pdf")

    assert not is_tesseract_blacklisted(conn, "bad-pdf")
    mark_tesseract_blacklisted(conn, "bad-pdf")
    assert is_tesseract_blacklisted(conn, "bad-pdf")
    assert not is_tesseract_blacklisted(conn, "ok-pdf")
    assert not is_ocr_fully_failed(conn, "bad-pdf")

    # Idempotent — andra anropet stämplar bara om timestampen.
    mark_tesseract_blacklisted(conn, "bad-pdf")

    # clear-funktionen återställer blacklist och Surya-spärr.
    mark_surya_failed(conn, "bad-pdf")
    assert is_ocr_fully_failed(conn, "bad-pdf")
    assert clear_tesseract_blacklisted(conn) == 1
    row = get_pdf_file(conn, "bad-pdf")
    assert row["tesseract_blacklisted_at"] is None
    assert row["surya_failed_at"] is None
    assert clear_tesseract_blacklisted(conn) == 0  # idempotent


def test_mark_tesseract_blacklisted_requires_existing_stem(tmp_path):
    conn = _fresh(tmp_path)
    with pytest.raises(KeyError):
        mark_tesseract_blacklisted(conn, "nonexistent")


def test_retry_tesseract_blacklisted_clears_blacklist_and_failed(tmp_path):
    conn = _fresh(tmp_path)
    mark_tesseract_failed(
        conn, "bad-pdf", pdf_path="downloaded/files/bad-pdf.pdf", source="files"
    )
    mark_tesseract_blacklisted(conn, "bad-pdf")
    mark_surya_failed(conn, "bad-pdf")

    assert retry_tesseract_blacklisted(conn) == 1
    row = get_pdf_file(conn, "bad-pdf")
    assert row["tesseract_blacklisted_at"] is None
    assert row["tesseract_failed"] == 0
    assert row["surya_failed_at"] is None


def test_surya_failure_and_clear_ocr_failures(tmp_path):
    conn = _fresh(tmp_path)
    mark_tesseract_failed(
        conn, "bad-pdf", pdf_path="downloaded/files/bad-pdf.pdf", source="files"
    )
    mark_tesseract_blacklisted(conn, "bad-pdf")

    mark_surya_failed(conn, "bad-pdf")
    row = get_pdf_file(conn, "bad-pdf")
    assert row["surya_failed_at"] is not None

    assert clear_ocr_failures(conn, "bad-pdf") is True
    row = get_pdf_file(conn, "bad-pdf")
    assert row["tesseract_failed"] == 0
    assert row["tesseract_blacklisted_at"] is None
    assert row["surya_failed_at"] is None


def test_tesseract_done_clears_previous_ocr_failure_status(tmp_path):
    conn = _fresh(tmp_path)
    mark_tesseract_failed(
        conn, "bad-pdf", pdf_path="downloaded/files/bad-pdf.pdf", source="files"
    )
    mark_tesseract_blacklisted(conn, "bad-pdf")
    mark_surya_failed(conn, "bad-pdf")

    mark_tesseract_done(
        conn, "bad-pdf", pdf_path="downloaded/files/bad-pdf.pdf", source="files"
    )

    row = get_pdf_file(conn, "bad-pdf")
    assert row["tesseract_failed"] == 0
    assert row["tesseract_blacklisted_at"] is None
    assert row["surya_failed_at"] is None


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
    for t in threads:
        t.start()
    for t in threads:
        t.join()

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


def test_touch_text_mtime_updates_only_mtime(tmp_path):
    conn = _fresh(tmp_path)
    mark_tesseract_done(conn, "doc", pdf_path="downloaded/files/doc.pdf",
                        source="files")
    row = get_pdf_file(conn, "doc")
    assert row["text_mtime"] is None
    touch_text_mtime(conn, "doc", text_mtime=1234.5)
    row = get_pdf_file(conn, "doc")
    assert row["text_mtime"] == 1234.5
    # merged_at/normalized_at ska inte påverkas
    assert row["merged_at"] is None
    assert row["normalized_at"] is None


def test_touch_text_mtime_unknown_stem_raises(tmp_path):
    from db import touch_text_mtime
    conn = _fresh(tmp_path)
    with pytest.raises(KeyError):
        touch_text_mtime(conn, "finns-ej", text_mtime=1.0)


def test_doc_entities_roundtrip(tmp_path):
    from db import doc_entities_extracted, iter_doc_entities, record_doc_entities
    conn = _fresh(tmp_path)
    payload = {"entiteter": [{"typ": "person", "namn": "Stig Engström"}],
               "relationer": []}
    assert not doc_entities_extracted(conn, "doc", 1)
    record_doc_entities(conn, pdf_stem="doc", page_num=1,
                        payload=payload, model="haiku-test")
    assert doc_entities_extracted(conn, "doc", 1)
    rows = list(iter_doc_entities(conn))
    assert len(rows) == 1
    assert rows[0]["pdf_stem"] == "doc"
    assert rows[0]["payload"]["entiteter"][0]["namn"] == "Stig Engström"


def test_map_places_crud(tmp_path):
    conn = _fresh(tmp_path)
    place_id = record_map_place(
        conn,
        name="Dekorima",
        lat=59.33695,
        lon=18.06324,
    )

    places = list_map_places(conn)

    assert places == [{
        "id": place_id,
        "name": "Dekorima",
        "lat": 59.33695,
        "lon": 18.06324,
        "created_at": places[0]["created_at"],
    }]
    assert places[0]["created_at"]
    assert delete_map_place(conn, place_id) is True
    assert delete_map_place(conn, place_id) is False
    assert list_map_places(conn) == []


def test_map_places_are_listed_alphabetically(tmp_path):
    conn = _fresh(tmp_path)
    conn.execute(
        """
        INSERT INTO map_places(name, lat, lon, created_at)
        VALUES (?, ?, ?, ?)
        """,
        ("zeta", 59.0, 18.0, "2026-01-01T00:00:02+00:00"),
    )
    conn.execute(
        """
        INSERT INTO map_places(name, lat, lon, created_at)
        VALUES (?, ?, ?, ?)
        """,
        ("Alpha", 59.1, 18.1, "2026-01-01T00:00:01+00:00"),
    )
    conn.commit()

    places = list_map_places(conn)

    assert [place["name"] for place in places] == ["Alpha", "zeta"]


def test_map_place_requires_name_and_coordinates(tmp_path):
    conn = _fresh(tmp_path)
    with pytest.raises(ValueError):
        record_map_place(conn, name=" ", lat=59.0, lon=18.0)
    with pytest.raises(ValueError):
        record_map_place(conn, name="Grand", lat=120.0, lon=18.0)
    with pytest.raises(ValueError):
        record_map_place(conn, name="Grand", lat=59.0, lon=220.0)


def test_map_observations_crud_and_person_filter(tmp_path):
    conn = _fresh(tmp_path)
    first = record_map_observation(
        conn,
        person="Olof Palme",
        place_name="Grand",
        lat=59.34057,
        lon=18.06024,
        time="21:15",
        uncertainty="ca",
        nr="2055",
        sida=1,
        note="Biobesök",
    )
    second = record_map_observation(
        conn,
        person="Lisbeth Palme",
        place_name="Grand",
        lat=59.34057,
        lon=18.06024,
        time="21:15",
        nr="2055",
        sida=1,
    )

    assert [r["id"] for r in list_map_observations(conn)] == [second, first]
    palme = list_map_observations(conn, person="Olof Palme")
    assert len(palme) == 1
    assert palme[0]["person"] == "Olof Palme"
    assert palme[0]["place_name"] == "Grand"
    assert palme[0]["sida"] == 1

    assert update_map_observation(
        conn,
        first,
        place_name="Dekorima",
        lat=59.33695,
        lon=18.06324,
        time="23:21",
        note="Uppdaterad",
    ) is True
    updated = list_map_observations(conn, person="Olof Palme")[0]
    assert updated["place_name"] == "Dekorima"
    assert updated["time"] == "23:21"
    assert updated["note"] == "Uppdaterad"
    assert updated["updated_at"] >= updated["created_at"]

    assert update_map_observation(conn, 9999, place_name="Saknas") is False
    assert delete_map_observation(conn, second) is True
    assert delete_map_observation(conn, second) is False


def test_map_observation_requires_person_and_coordinates(tmp_path):
    conn = _fresh(tmp_path)
    with pytest.raises(ValueError):
        record_map_observation(conn, person=" ", place_name=None, lat=59.0, lon=18.0)
    with pytest.raises(ValueError):
        record_map_observation(conn, person="A", place_name=None, lat=-100.0, lon=18.0)
    with pytest.raises(ValueError):
        record_map_observation(conn, person="A", place_name=None, lat=59.0, lon=200.0)
    with pytest.raises(ValueError):
        update_map_observation(conn, 1, forbidden="x")


def test_map_time_must_be_strict_hh_mm(tmp_path):
    conn = _fresh(tmp_path)
    for bad_time in ("9:5", "24:00", "fri text"):
        with pytest.raises(ValueError):
            seed_map_data_if_empty(
                conn,
                [{"name": "Dekorima", "lat": 59.0, "lon": 18.0}],
                [{
                    "person": "Olof Palme",
                    "place_name": "Dekorima",
                    "lat": 59.0,
                    "lon": 18.0,
                    "time": bad_time,
                }],
            )


def test_update_map_observation_rejects_invalid_time(tmp_path):
    conn = _fresh(tmp_path)
    obs_id = record_map_observation(
        conn,
        person="Olof Palme",
        place_name=None,
        lat=59.0,
        lon=18.0,
        time="21:15",
    )

    with pytest.raises(ValueError):
        update_map_observation(conn, obs_id, time="24:00")


def test_seed_map_data_if_empty_ignores_invalid_input_when_data_exists(tmp_path):
    conn = _fresh(tmp_path)
    record_map_place(conn, name="Dekorima", lat=59.33695, lon=18.06324)
    record_map_observation(
        conn,
        person="Olof Palme",
        place_name="Dekorima",
        lat=59.33695,
        lon=18.06324,
        time="23:21",
        uncertainty="ca",
        nr="1000",
        sida=1,
        note="Testkälla",
    )

    inserted = seed_map_data_if_empty(
        conn,
        [{"name": "Dekorima", "lat": 59.0, "lon": 18.0}],
        [{
            "person": "Olof Palme",
            "place_name": "Dekorima",
            "lat": 59.0,
            "lon": 18.0,
            "time": "24:00",
        }],
    )

    assert inserted == 0
    assert len(list_map_places(conn)) == 1
    assert len(list_map_observations(conn)) == 1


def test_seed_map_data_if_empty_rejects_invalid_time_on_empty_database(tmp_path):
    conn = _fresh(tmp_path)

    with pytest.raises(ValueError):
        seed_map_data_if_empty(
            conn,
            [{"name": "Dekorima", "lat": 59.0, "lon": 18.0}],
            [{
                "person": "Olof Palme",
                "place_name": "Dekorima",
                "lat": 59.0,
                "lon": 18.0,
                "time": "24:00",
            }],
        )


def test_seed_map_data_if_empty_is_idempotent(tmp_path):
    conn = _fresh(tmp_path)
    places = [{"name": "Dekorima", "lat": 59.33695, "lon": 18.06324}]
    observations = [{
        "person": "Olof Palme",
        "place_name": "Dekorima",
        "lat": 59.33695,
        "lon": 18.06324,
        "time": "23:21",
        "uncertainty": "ca",
        "nr": "1000",
        "sida": 1,
        "note": "Testkälla",
    }]

    assert seed_map_data_if_empty(conn, places, observations) == 2
    assert seed_map_data_if_empty(conn, places, observations) == 0
    assert len(list_map_places(conn)) == 1
    assert len(list_map_observations(conn)) == 1


def test_map_observation_candidates_roundtrip(tmp_path):
    conn = _fresh(tmp_path)

    candidate_id = record_map_observation_candidate(
        conn,
        pdf_stem="2055 — Grandbesökare",
        page_num=3,
        person="Olof Palme",
        raw_place="Grand",
        place_name="Biografen Grand",
        lat=59.34057,
        lon=18.06024,
        time="21:15",
        uncertainty="ca",
        nr="2055",
        sida=3,
        quote="Olof Palme sågs vid Grand omkring 21.15.",
        note="Sågs vid platsen enligt texten.",
        confidence="high",
        place_match="exact",
        model="test-model",
    )

    rows = list_map_observation_candidates(conn)

    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == candidate_id
    assert row["status"] == "pending"
    assert row["person"] == "Olof Palme"
    assert row["place_name"] == "Biografen Grand"
    assert row["time"] == "21:15"
    assert row["quote"].startswith("Olof Palme")
    assert row["created_at"]
    assert row["updated_at"]


def test_map_observation_candidate_upsert_is_idempotent(tmp_path):
    conn = _fresh(tmp_path)
    kwargs = dict(
        pdf_stem="2055 — Grandbesökare",
        page_num=3,
        person="Olof Palme",
        raw_place="Grand",
        place_name="Biografen Grand",
        lat=59.34057,
        lon=18.06024,
        time="21:15",
        uncertainty="ca",
        nr="2055",
        sida=3,
        quote="Olof Palme sågs vid Grand omkring 21.15.",
        note="Första texten.",
        confidence="medium",
        place_match="exact",
        model="test-model",
    )

    first = record_map_observation_candidate(conn, **kwargs)
    second = record_map_observation_candidate(conn, **{**kwargs, "note": "Uppdaterad text."})

    rows = list_map_observation_candidates(conn)
    assert first == second
    assert len(rows) == 1
    assert rows[0]["note"] == "Uppdaterad text."


def test_map_observation_candidate_upsert_matches_case_insensitive_unique_index(tmp_path):
    conn = _fresh(tmp_path)
    kwargs = dict(
        pdf_stem="2055 — Grandbesökare",
        page_num=3,
        person="Olof Palme",
        raw_place="Grand",
        place_name="Biografen Grand",
        lat=59.34057,
        lon=18.06024,
        time="21:15",
        uncertainty="ca",
        nr="2055",
        sida=3,
        quote="Olof Palme sågs vid Grand omkring 21.15.",
        note="Första texten.",
        confidence="medium",
        place_match="exact",
        model="test-model",
    )

    first = record_map_observation_candidate(conn, **kwargs)
    second = record_map_observation_candidate(
        conn,
        **{
            **kwargs,
            "person": "olof palme",
            "raw_place": "grand",
            "note": "Uppdaterad text.",
        },
    )

    rows = list_map_observation_candidates(conn)
    assert first == second
    assert len(rows) == 1
    assert rows[0]["note"] == "Uppdaterad text."


def test_map_observation_candidate_requires_core_fields(tmp_path):
    conn = _fresh(tmp_path)
    base = dict(
        pdf_stem="doc",
        page_num=1,
        person="Olof Palme",
        raw_place="Grand",
        place_name=None,
        lat=None,
        lon=None,
        time=None,
        uncertainty=None,
        nr="2055",
        sida=1,
        quote="Kort citat.",
        note=None,
        confidence="low",
        place_match="none",
        model="test-model",
    )
    for field in ("pdf_stem", "person", "raw_place", "nr", "quote", "model"):
        with pytest.raises(ValueError):
            record_map_observation_candidate(conn, **{**base, field: " "})
    with pytest.raises(ValueError):
        record_map_observation_candidate(conn, **{**base, "confidence": "maybe"})
    with pytest.raises(ValueError):
        record_map_observation_candidate(conn, **{**base, "place_match": "magic"})
    with pytest.raises(ValueError):
        record_map_observation_candidate(conn, **{**base, "lat": 59.0, "lon": None})


def test_update_reject_and_approve_map_observation_candidate(tmp_path):
    conn = _fresh(tmp_path)
    candidate_id = record_map_observation_candidate(
        conn,
        pdf_stem="2055 — Grandbesökare",
        page_num=3,
        person="Olof Palme",
        raw_place="Grand",
        place_name=None,
        lat=None,
        lon=None,
        time=None,
        uncertainty="ca",
        nr="2055",
        sida=3,
        quote="Olof Palme sågs vid Grand.",
        note=None,
        confidence="medium",
        place_match="none",
        model="test-model",
    )

    assert update_map_observation_candidate(
        conn,
        candidate_id,
        place_name="Biografen Grand",
        lat=59.34057,
        lon=18.06024,
        time="21:15",
        note="Godkänd rad.",
    ) is True

    observation_id = approve_map_observation_candidate(conn, candidate_id)
    approved = list_map_observation_candidates(conn, status="approved")
    observations = list_map_observations(conn, person="Olof Palme")

    assert approved[0]["map_observation_id"] == observation_id
    assert observations[0]["person"] == "Olof Palme"
    assert observations[0]["place_name"] == "Biografen Grand"
    assert observations[0]["time"] == "21:15"
    assert observations[0]["nr"] == "2055"
    assert observations[0]["sida"] == 3
    assert "Godkänd rad." in observations[0]["note"]

    reject_id = record_map_observation_candidate(
        conn,
        pdf_stem="doc",
        page_num=4,
        person="Lisbeth Palme",
        raw_place="okänd plats",
        place_name=None,
        lat=None,
        lon=None,
        time=None,
        uncertainty=None,
        nr="2055",
        sida=4,
        quote="Oklar rad.",
        note=None,
        confidence="low",
        place_match="none",
        model="test-model",
    )
    assert reject_map_observation_candidate(conn, reject_id) is True
    assert list_map_observation_candidates(conn, status="rejected")[0]["id"] == reject_id


def test_approve_candidate_rolls_back_observation_when_status_update_fails(tmp_path):
    conn = _fresh(tmp_path)
    candidate_id = record_map_observation_candidate(
        conn,
        pdf_stem="2055 — Grandbesökare",
        page_num=3,
        person="Olof Palme",
        raw_place="Grand",
        place_name="Biografen Grand",
        lat=59.34057,
        lon=18.06024,
        time="21:15",
        uncertainty="ca",
        nr="2055",
        sida=3,
        quote="Olof Palme sågs vid Grand omkring 21.15.",
        note="Källbelagd observation.",
        confidence="medium",
        place_match="exact",
        model="test-model",
    )
    conn.execute(
        """
        CREATE TRIGGER fail_candidate_approval
        BEFORE UPDATE OF status ON map_observation_candidates
        WHEN NEW.status = 'approved'
        BEGIN
            SELECT RAISE(ABORT, 'simulerat uppdateringsfel');
        END
        """
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="simulerat uppdateringsfel"):
        approve_map_observation_candidate(conn, candidate_id)
    conn.rollback()

    assert list_map_observations(conn, person="Olof Palme") == []
    assert list_map_observation_candidates(conn, status="pending")[0]["id"] == candidate_id


def test_approve_candidate_requires_time_source_and_coordinates(tmp_path):
    conn = _fresh(tmp_path)
    candidate_id = record_map_observation_candidate(
        conn,
        pdf_stem="doc",
        page_num=1,
        person="Olof Palme",
        raw_place="Grand",
        place_name="Biografen Grand",
        lat=59.34057,
        lon=18.06024,
        time=None,
        uncertainty=None,
        nr="2055",
        sida=1,
        quote="Olof Palme vid Grand.",
        note=None,
        confidence="medium",
        place_match="exact",
        model="test-model",
    )

    with pytest.raises(ValueError, match="time"):
        approve_map_observation_candidate(conn, candidate_id)


def test_record_doc_entities_is_upsert(tmp_path):
    from db import iter_doc_entities, record_doc_entities
    conn = _fresh(tmp_path)
    record_doc_entities(conn, pdf_stem="doc", page_num=1,
                        payload={"entiteter": [], "relationer": []}, model="a")
    record_doc_entities(conn, pdf_stem="doc", page_num=1,
                        payload={"entiteter": [], "relationer": []}, model="b")
    rows = list(iter_doc_entities(conn))
    assert len(rows) == 1
