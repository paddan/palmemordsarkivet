"""Tabellen document_meta: tolkad dokumentmetadata per stam (migration 14)."""
import sqlite3

import pytest

import db
from db import SCHEMA_VERSION, connect, init_schema, schema_version


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "state.db")
    init_schema(c)
    yield c
    c.close()


def _tables(c):
    return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _row(**over):
    row = {
        "pdf_stem": "1 — PM", "arkiv": "palme", "nr": "1", "titel": "PM",
        "bestallt": None, "upplagt": "2020-07-14",
        "anmarkning": "E-63-1, se 531.1", "antal_sidor": 10,
        "dokumentdatum": None, "uppslag": ["E63-01"],
        "hanvisningar": ["531.1"], "avkortat": False,
    }
    row.update(over)
    return row


def test_fresh_db_has_document_meta(conn):
    assert "document_meta" in _tables(conn)
    assert schema_version(conn) == SCHEMA_VERSION
    assert SCHEMA_VERSION >= 14


def test_migration_from_13_adds_table(tmp_path):
    path = tmp_path / "v13.db"
    c = connect(path)
    init_schema(c)
    c.execute("DROP TABLE document_meta")
    c.execute("DELETE FROM schema_version WHERE version = 14")
    c.execute("PRAGMA user_version = 13")
    c.commit()
    assert "document_meta" not in _tables(c)
    c.close()

    c = connect(path)
    try:
        init_schema(c)
        assert "document_meta" in _tables(c)
        assert schema_version(c) == SCHEMA_VERSION
    finally:
        c.close()


def test_upsert_roundtrip_and_overwrite(conn):
    row = _row()
    assert db.upsert_document_meta(conn, [row]) == 1
    got = db.get_document_meta(conn, "1 — PM")
    assert got is not None
    assert got["uppslag"] == ["E63-01"]
    assert got["hanvisningar"] == ["531.1"]
    assert got["avkortat"] is False
    assert got["updated_at"]
    assert got["upplagt"] == "2020-07-14"
    db.upsert_document_meta(conn, [{**row, "antal_sidor": 11, "avkortat": True}])
    got = db.get_document_meta(conn, "1 — PM")
    assert got["antal_sidor"] == 11
    assert got["avkortat"] is True
    assert conn.execute("SELECT COUNT(*) FROM document_meta").fetchone()[0] == 1


def test_upsert_empty_returns_zero(conn):
    assert db.upsert_document_meta(conn, []) == 0


def test_invalid_arkiv_rejected(conn):
    with pytest.raises(sqlite3.IntegrityError):
        db.upsert_document_meta(conn, [_row(arkiv="annat")])


def test_prune_keeps_listed_stems(conn):
    db.upsert_document_meta(
        conn, [_row(pdf_stem=s) for s in ("a", "b", "c")]
    )
    assert db.prune_document_meta(conn, {"a", "c"}) == 1
    assert db.get_document_meta(conn, "b") is None
    assert db.get_document_meta(conn, "a") is not None
    assert db.get_document_meta(conn, "c") is not None
    assert db.prune_document_meta(conn, ["a", "c"]) == 0


def test_get_missing_returns_none(conn):
    assert db.get_document_meta(conn, "saknas") is None


def test_list_document_meta_sorted_decoded(conn):
    db.upsert_document_meta(conn, [_row(pdf_stem="b", avkortat=True), _row(pdf_stem="a")])
    rows = db.list_document_meta(conn)
    assert [r["pdf_stem"] for r in rows] == ["a", "b"]
    assert rows[0]["uppslag"] == ["E63-01"] and rows[0]["hanvisningar"] == ["531.1"]
    assert rows[0]["avkortat"] is False and rows[1]["avkortat"] is True
    assert rows[0] == db.get_document_meta(conn, "a")
