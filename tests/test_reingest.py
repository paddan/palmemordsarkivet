"""Tester för should_reingest, find_orphans, _table_exists och _source_predicate från rag/ingest.py."""

from __future__ import annotations

import pytest

import db as state_db
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
