"""Tester för should_reingest, find_orphans och _table_exists från rag/ingest.py."""

from __future__ import annotations

from ingest import _table_exists, find_orphans, should_reingest


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
