"""Tester för should_reingest, find_orphans, _table_exists och _source_predicate från rag/ingest.py."""

from __future__ import annotations

import pytest
from ingest import (
    _source_predicate,
    _table_exists,
    classify_index_action,
    classify_reingest,
    delete_source_for_reingest,
    find_orphans,
    get_table_sources,
    should_reingest,
)

import db as state_db

# ---------------------------------------------------------------------------
# Minimala mock-objekt för att testa _table_exists utan att starta LanceDB.
# ---------------------------------------------------------------------------

class _PydanticStyleResponse:
    """Simulerar ListTablesResponse från lancedb ≥0.20 (har .tables-attribut)."""
    def __init__(self, table_names: list[str]) -> None:
        self.tables = table_names

    def __iter__(self):
        # Pydantic-modellens __iter__ ger nyckel-värde-par, inte tabellnamn —
        # det är precis den fällan som orsakade ursprungsbuggarna.
        yield from [("tables", self.tables), ("page_token", None)]


class _MockDB:
    def __init__(self, result) -> None:
        self._result = result

    def list_tables(self):
        return self._result


class _MockSourceQuery:
    def __init__(self, sources: list[str]) -> None:
        self._sources = sources
        self.selected: list[str] | None = None
        self.limit_value: int | None = None

    def select(self, columns: list[str]):
        self.selected = columns
        return self

    def limit(self, value: int):
        self.limit_value = value
        return self

    def to_list(self):
        return [{"source": source} for source in self._sources[:self.limit_value]]


class _MockTable:
    def __init__(self, sources: list[str]) -> None:
        self.sources = sources
        self.deleted: list[str] = []
        self.query = _MockSourceQuery(sources)

    def search(self):
        return self.query

    def count_rows(self):
        return len(self.sources)

    def delete(self, predicate: str) -> None:
        self.deleted.append(predicate)


def test_table_exists_pydantic_response_found() -> None:
    # lancedb ≥0.20: list_tables() returnerar objekt med .tables, inte en ren lista.
    db = _MockDB(_PydanticStyleResponse(["chunks", "other"]))
    assert _table_exists(db, "chunks") is True


def test_table_exists_pydantic_response_not_found() -> None:
    db = _MockDB(_PydanticStyleResponse(["other"]))
    assert _table_exists(db, "chunks") is False


def test_table_exists_pydantic_response_empty() -> None:
    db = _MockDB(_PydanticStyleResponse([]))
    assert _table_exists(db, "chunks") is False


def test_table_exists_plain_list_fallback_found() -> None:
    # Äldre lancedb returnerade en ren lista — fallback via list(result).
    db = _MockDB(["chunks", "other"])
    assert _table_exists(db, "chunks") is True


def test_table_exists_plain_list_fallback_not_found() -> None:
    db = _MockDB([])
    assert _table_exists(db, "chunks") is False


def test_unchanged_file_skipped() -> None:
    assert should_reingest(stored_mtime=100.0, disk_mtime=100.0, reindex_since=None) is False


def test_modified_file_reingested() -> None:
    assert should_reingest(stored_mtime=100.0, disk_mtime=200.0, reindex_since=None) is True


def test_legacy_row_without_mtime_skipped_by_default() -> None:
    # stored_mtime=0 är sentinel för "indexerad innan mtime-tracking fanns" —
    # ska inte re-indexeras automatiskt (då skulle hela det gamla indexet
    # plöjas om vid första körning).
    assert should_reingest(stored_mtime=0.0, disk_mtime=200.0, reindex_since=None) is False


def test_reindex_since_forces_legacy_reingest() -> None:
    # --reindex-since 150 → alla filer modifierade efter 150 ska re-indexeras,
    # även de utan känd stored_mtime.
    assert should_reingest(stored_mtime=0.0, disk_mtime=200.0, reindex_since=150.0) is True


def test_reindex_since_excludes_files_modified_before_cutoff() -> None:
    assert should_reingest(stored_mtime=0.0, disk_mtime=100.0, reindex_since=150.0) is False


def test_reindex_since_with_tracked_file_modified_after_cutoff() -> None:
    # Filen är redan indexerad (stored=250), men disk-mtime är ny (300)
    # och over cutoff — ska re-indexeras.
    assert should_reingest(stored_mtime=250.0, disk_mtime=300.0, reindex_since=200.0) is True


def test_reindex_since_doesnt_force_unchanged_tracked_file() -> None:
    # stored == disk, ingen ändring sedan indexering — skip även om cutoff är satt.
    assert should_reingest(stored_mtime=100.0, disk_mtime=100.0, reindex_since=50.0) is False


def test_find_orphans_returns_files_only_in_table() -> None:
    stored = {"a.txt", "b.txt", "c.txt"}
    disk = {"a.txt", "c.txt"}
    assert find_orphans(stored, disk) == ["b.txt"]


def test_find_orphans_empty_when_all_match() -> None:
    assert find_orphans({"a.txt"}, {"a.txt"}) == []


def test_find_orphans_sorted() -> None:
    stored = {"z.txt", "a.txt", "m.txt"}
    assert find_orphans(stored, set()) == ["a.txt", "m.txt", "z.txt"]


def test_get_table_sources_reads_existing_lancedb_sources() -> None:
    table = _MockTable(["a.txt", "a.txt", "b.txt"])
    assert get_table_sources(table) == {"a.txt", "b.txt"}
    assert table.query.selected == ["source"]
    assert table.query.limit_value == 3


def test_existing_lancedb_source_is_reingest_when_sqlite_state_is_missing() -> None:
    assert classify_reingest(
        filename="a.txt",
        disk_mtime=200.0,
        already={},
        table_sources={"a.txt"},
        reindex_since=None,
    ) is True


def test_missing_lancedb_source_is_new_when_sqlite_state_still_exists() -> None:
    assert classify_index_action(
        filename="a.txt",
        disk_mtime=200.0,
        already={"a.txt": 200.0},
        table_sources=set(),
        reindex_since=None,
    ) == "new"


def test_unchanged_unusable_source_is_still_skipped() -> None:
    assert classify_index_action(
        filename="bad.txt",
        disk_mtime=200.0,
        already={"bad.txt": 200.0},
        table_sources=set(),
        reindex_since=None,
        unusable_sources={"bad.txt"},
    ) == "skip"


def test_delete_source_for_reingest_removes_old_chunks() -> None:
    table = _MockTable(["a.txt"])
    delete_source_for_reingest(table, "a.txt", is_reingest=True)
    assert table.deleted == ["source = 'a.txt'"]


def test_delete_source_for_new_file_keeps_table_unchanged() -> None:
    table = _MockTable([])
    delete_source_for_reingest(table, "a.txt", is_reingest=False)
    assert table.deleted == []


# ---------------------------------------------------------------------------
# _source_predicate — SQL-sanitering för LanceDB delete
# ---------------------------------------------------------------------------

def test_source_predicate_normal_filename() -> None:
    pred = _source_predicate("281 — Titel.txt")
    assert pred == "source = '281 — Titel.txt'"


def test_source_predicate_escapes_single_quote() -> None:
    pred = _source_predicate("fil'test.txt")
    assert pred == "source = 'fil''test.txt'"


def test_source_predicate_double_single_quote() -> None:
    pred = _source_predicate("it's here.txt")
    assert pred == "source = 'it''s here.txt'"


def test_source_predicate_rejects_control_characters() -> None:
    with pytest.raises(ValueError, match="kontrolltecken"):
        _source_predicate("fil\x00hack.txt")


def test_source_predicate_rejects_newline() -> None:
    with pytest.raises(ValueError, match="kontrolltecken"):
        _source_predicate("fil\nhack.txt")


# ---------------------------------------------------------------------------
# `already`-dict byggs från state.db.ingest (Task 11) — speglar SQL:en i main().
# ---------------------------------------------------------------------------

def test_already_dict_built_from_ingest_table(tmp_path, monkeypatch) -> None:
    """Den dict-comprehension som main() använder ska mappa stem→mtime
    med ``.txt``-suffix på nyckeln (källan i LanceDB är filnamn, men
    ingest-tabellen lagrar bara pdf_stem)."""
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    conn = state_db.connect()
    state_db.init_schema(conn)
    state_db.record_ingest(conn, pdf_stem="123 — Titel", text_mtime=100.0, chunks=5)
    state_db.record_ingest(conn, pdf_stem="456 — Annan", text_mtime=200.0, chunks=8)

    already: dict[str, float] = {
        row["pdf_stem"] + ".txt": row["text_mtime"]
        for row in conn.execute("SELECT pdf_stem, text_mtime FROM ingest")
    }

    assert already == {
        "123 — Titel.txt": 100.0,
        "456 — Annan.txt": 200.0,
    }


def test_already_dict_empty_when_ingest_empty(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    conn = state_db.connect()
    state_db.init_schema(conn)

    already: dict[str, float] = {
        row["pdf_stem"] + ".txt": row["text_mtime"]
        for row in conn.execute("SELECT pdf_stem, text_mtime FROM ingest")
    }
    assert already == {}


def test_record_ingest_updates_mtime_on_reingest(tmp_path, monkeypatch) -> None:
    """record_ingest UPSERTar — re-index uppdaterar mtime+chunks."""
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    conn = state_db.connect()
    state_db.init_schema(conn)
    state_db.record_ingest(conn, pdf_stem="stem1", text_mtime=100.0, chunks=3)
    state_db.record_ingest(conn, pdf_stem="stem1", text_mtime=200.0, chunks=7)

    assert state_db.get_ingested_mtime(conn, "stem1") == 200.0
    rows = list(conn.execute("SELECT * FROM ingest WHERE pdf_stem=?", ("stem1",)))
    assert len(rows) == 1
    assert rows[0]["chunks"] == 7


def test_orphan_cleanup_deletes_from_ingest(tmp_path, monkeypatch) -> None:
    """Speglar orphan-rensningen i main(): DELETE FROM ingest WHERE pdf_stem=?."""
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    conn = state_db.connect()
    state_db.init_schema(conn)
    state_db.record_ingest(conn, pdf_stem="alive", text_mtime=100.0, chunks=1)
    state_db.record_ingest(conn, pdf_stem="orphan", text_mtime=100.0, chunks=1)

    # Simulera samma cleanup-logik
    s = "orphan.txt"
    stem = s[:-4] if s.endswith(".txt") else s
    conn.execute("DELETE FROM ingest WHERE pdf_stem=?", (stem,))
    conn.commit()

    assert state_db.get_ingested_mtime(conn, "orphan") is None
    assert state_db.get_ingested_mtime(conn, "alive") == 100.0


# ---------------------------------------------------------------------------
# run_ingest — fallback till MODEL_NAME när modellnamnet är tomt
# (registry-default och pipeline skickar ""; utan fallback byggs ett tomt index).
# ---------------------------------------------------------------------------

class _RecordingSentenceTransformer:
    """Fångar vilket modellnamn run_ingest instansierar med."""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name

    def encode(self, *args, **kwargs):
        # Anropas aldrig när text_dir saknar .txt-filer.
        raise AssertionError("encode ska inte anropas i dessa tester")


def _run_ingest_with_stubbed_model(tmp_path, monkeypatch, model_name: str):
    import ingest as ingest_module

    seen: dict[str, str] = {}

    class _Stub:
        def __init__(self, name: str) -> None:
            seen["model"] = name

        def encode(self, *args, **kwargs):
            return None

    monkeypatch.setattr(ingest_module, "SentenceTransformer", _Stub)
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))

    text_dir = tmp_path / "text"
    text_dir.mkdir()
    rc = ingest_module.run_ingest(
        rebuild=False,
        limit=None,
        text_dir=text_dir,
        db_dir=tmp_path / "lancedb",
        chunk_chars=800,
        chunk_overlap=150,
        model_name=model_name,
        unusable_list=tmp_path / "unusable.txt",
        reindex_since=None,
    )
    return rc, seen


def test_run_ingest_falls_back_to_default_model_when_name_empty(tmp_path, monkeypatch) -> None:
    import ingest as ingest_module

    rc, seen = _run_ingest_with_stubbed_model(tmp_path, monkeypatch, model_name="")

    assert rc == 0
    assert seen["model"] == ingest_module.MODEL_NAME


def test_run_ingest_keeps_explicit_model_name(tmp_path, monkeypatch) -> None:
    rc, seen = _run_ingest_with_stubbed_model(
        tmp_path, monkeypatch, model_name="custom/embedding-modell"
    )

    assert rc == 0
    assert seen["model"] == "custom/embedding-modell"


class _RecorderContext:
    """Minimal kontext som sparar loggrader för assertions."""

    def __init__(self) -> None:
        self.rader: list[tuple[str, str]] = []

    def log(self, message: str, level: str = "info") -> None:
        self.rader.append((level, message))

    def check_cancelled(self) -> None:
        return None

    def progress(self, *args, **kwargs) -> None:
        return None


def test_corrupt_unusable_mtimes_file_is_logged_and_ingest_continues(
    tmp_path, monkeypatch,
) -> None:
    import ingest as ingest_module

    class _Stub:
        def __init__(self, name: str) -> None:
            pass

        def encode(self, *args, **kwargs):
            return None

    monkeypatch.setattr(ingest_module, "SentenceTransformer", _Stub)
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    text_dir = tmp_path / "text"
    text_dir.mkdir()
    (tmp_path / "unusable_mtimes.json").write_text("{ trasig", encoding="utf-8")
    ctx = _RecorderContext()

    rc = ingest_module.run_ingest(
        rebuild=False,
        limit=None,
        text_dir=text_dir,
        db_dir=tmp_path / "lancedb",
        chunk_chars=800,
        chunk_overlap=150,
        model_name="test-modell",
        unusable_list=tmp_path / "unusable.txt",
        reindex_since=None,
        context=ctx,
    )

    assert rc == 0
    varningar = [msg for level, msg in ctx.rader if level == "warning"]
    assert any("unusable_mtimes.json" in m and "JSONDecodeError" in m for m in varningar)


# ---------------------------------------------------------------------------
# _chunka_fil — Markdown från layoutsidor och färskhetsregeln (Task 6)
# ---------------------------------------------------------------------------

def _gammal_chunka_fil(raw, filnamn, meta, disk_mtime, chunk_chars, chunk_overlap):
    """Ordagrann kopia av _chunka_fil före Task 6 — referens för regressionen."""
    from ingest import chunk_text, is_useful, split_pages

    rows = []
    chunk_idx = 0
    for page_idx, page in enumerate(split_pages(raw), start=1):
        for _, _, chunk in chunk_text(page, chunk_chars, chunk_overlap):
            if not is_useful(chunk):
                continue
            rows.append({
                "text": chunk, "source": filnamn, "page": page_idx,
                "chunk_idx": chunk_idx, "mtime": disk_mtime, **meta,
            })
            chunk_idx += 1
    return rows


_PLAIN = (
    "Protokoll   fört vid förhör med vittne.   Förhöret hölls i Stockholm.\n"
    + "Vittnet uppger att hon gick längs Sveavägen den kvällen. " * 30
    + "\f"
    + "  | Nr |  Namn  |\n|----|----|\n| 01 | Andersson, Karl |\n"
    + "Tabellen ovan  är  inskannad med pdftotext -layout och ska chunkas som i dag. " * 12
    + "\f\f"
    + "Kort."
)


def test_plain_file_chunks_unchanged() -> None:
    from ingest import _chunka_fil

    meta = {"nr": "1", "titel": "t"}
    expected = _gammal_chunka_fil(_PLAIN, "a.txt", meta, 1.5, 800, 150)
    assert expected  # testet ska mäta något
    assert _chunka_fil(_PLAIN, "a.txt", meta, 1.5, 800, 150) == expected
    assert _chunka_fil(_PLAIN, "a.txt", meta, 1.5, 800, 150, page_markdown={}) == expected
    # Markdown för en sida utanför filen påverkar inget
    assert _chunka_fil(
        _PLAIN, "a.txt", meta, 1.5, 800, 150, page_markdown={9: ("x", "# x")}
    ) == expected


def _layout_doc(tmp_path, monkeypatch, fixture: str = "forhor-dialog-a"):
    """Riktig väg: record_page → merge_one → normalize_text.process_file."""
    from test_surya_layout import load_fixture

    import surya_layout as sl
    from merge_pages import merge_one
    from normalize_text import process_file

    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    txt_dir = tmp_path / "text"
    txt_dir.mkdir()
    (txt_dir / "doc.txt").write_text(_PLAIN, encoding="utf-8")
    conn = state_db.connect()
    state_db.init_schema(conn)
    state_db.upsert_pdf_file(conn, pdf_stem="doc", source="files", pdf_path="x.pdf")
    blocks, _ = sl.parse_page(load_fixture(fixture))
    state_db.record_page(
        conn, pdf_stem="doc", page_num=2, engine="surya-layout",
        text=sl.page_text(blocks), score=90.0, markdown=sl.page_markdown(blocks),
    )
    assert merge_one("doc", txt_dir, conn=conn) is True
    process_file(txt_dir / "doc.txt")
    md_map = state_db.get_page_markdown_map(conn, "doc")
    conn.close()
    return txt_dir / "doc.txt", md_map


def test_fresh_markdown_used(tmp_path, monkeypatch) -> None:
    from ingest import _chunka_fil

    path, md_map = _layout_doc(tmp_path, monkeypatch)
    raw = path.read_text(encoding="utf-8")
    ctx = _RecorderContext()
    rows = _chunka_fil(raw, "doc.txt", {}, 2.0, 800, 150, page_markdown=md_map, context=ctx)

    page2 = [r["text"] for r in rows if r["page"] == 2]
    assert page2
    for text in page2:
        assert text.startswith("> Sidhuvud: SPANINGSUPPSLAG · Sid nr 2\n")
    assert any("**F:**" in t for t in page2)
    # övriga sidor chunkas exakt som i dag
    ref = _gammal_chunka_fil(raw, "doc.txt", {}, 2.0, 800, 150)
    assert [r["text"] for r in rows if r["page"] != 2] == [r["text"] for r in ref if r["page"] != 2]
    assert [r["chunk_idx"] for r in rows] == list(range(len(rows)))
    # en sammanfattning, ingen dokumenttext i loggen
    summary = [m for _, m in ctx.rader if "Markdown" in m]
    assert len(summary) == 1
    assert "1 sidor chunkade från Markdown" in summary[0]
    assert "0 med inaktuell Markdown" in summary[0]
    assert not any("Sarkaja" in m or "Aftonbladet" in m for _, m in ctx.rader)


def test_stale_markdown_ignored(tmp_path, monkeypatch) -> None:
    from ingest import _chunka_fil

    path, md_map = _layout_doc(tmp_path, monkeypatch)
    raw = path.read_text(encoding="utf-8")
    # Texten rättas efter OCR (LLM-korrigering, merge_wpu, manuell redigering)
    raw = raw.replace("Aftonbladet", "Expressen")
    ctx = _RecorderContext()
    rows = _chunka_fil(raw, "doc.txt", {}, 2.0, 800, 150, page_markdown=md_map, context=ctx)

    assert rows == _gammal_chunka_fil(raw, "doc.txt", {}, 2.0, 800, 150)
    assert not any("> Sidhuvud:" in r["text"] for r in rows)
    debug = [m for lvl, m in ctx.rader if lvl == "debug"]
    stale = [m for m in debug if "Markdown förkastad" in m]
    assert len(stale) == 1
    assert "1 sidor" in stale[0] and "[2]" in stale[0]
    summary = [m for _, m in ctx.rader if "sidor chunkade från Markdown" in m]
    assert len(summary) == 1
    assert "0 sidor chunkade från Markdown" in summary[0]
    assert "1 med inaktuell Markdown" in summary[0]
    assert not any("Expressen" in m for _, m in ctx.rader)


def test_empty_markdown_falls_back_to_text(tmp_path, monkeypatch) -> None:
    from ingest import _chunka_fil

    raw = "Första sidan. " * 20 + "\f" + "Andra sidan med text som räcker. " * 10
    page2 = raw.split("\f")[1]
    rows = _chunka_fil(raw, "a.txt", {}, 1.0, 800, 150, page_markdown={2: (page2, "")})
    assert rows == _gammal_chunka_fil(raw, "a.txt", {}, 1.0, 800, 150)


def test_run_ingest_reads_markdown_map(tmp_path, monkeypatch) -> None:
    import ingest as ingest_module

    path, _ = _layout_doc(tmp_path, monkeypatch)
    captured: list[str] = []

    class _Stub:
        def __init__(self, name: str) -> None:
            pass

        def encode(self, texts, **kwargs):
            import numpy as np

            captured.extend(texts)
            return np.ones((len(texts), ingest_module.EMBED_DIM), dtype="float32")

    monkeypatch.setattr(ingest_module, "SentenceTransformer", _Stub)
    rc = ingest_module.run_ingest(
        rebuild=False, limit=None, text_dir=path.parent, db_dir=tmp_path / "lancedb",
        chunk_chars=800, chunk_overlap=150, model_name="m",
        unusable_list=tmp_path / "unusable.txt", reindex_since=None,
        context=_RecorderContext(),
    )
    assert rc == 0
    assert any(t.startswith("passage: > Sidhuvud:") for t in captured)


def test_many_stale_pages_give_one_debug_line_cut_at_ten() -> None:
    from ingest import _chunka_fil

    pages = [f"Sida nummer {i} med tillräckligt mycket text för att vara användbar här." for i in range(1, 13)]
    raw = "\f".join(pages)
    md_map = {i: ("annan text än sidan", f"**M:** markdown {i}") for i in range(1, 13)}
    ctx = _RecorderContext()
    _chunka_fil(raw, "doc.txt", {}, 2.0, 800, 150, page_markdown=md_map, context=ctx)
    stale = [m for lvl, m in ctx.rader if lvl == "debug" and "Markdown förkastad" in m]
    assert len(stale) == 1
    assert "12 sidor" in stale[0]
    assert "[1, 2, 3, 4, 5, 6, 7, 8, 9, 10]" in stale[0] and "11" not in stale[0].split("första")[1]
