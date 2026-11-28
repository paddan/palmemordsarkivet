"""Tabellen ontology_extractions: rå modellutdata per (stam, sida) (migration 15)."""
import pytest

import db
from db import SCHEMA_VERSION, connect, init_schema, schema_version


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "state.db")
    init_schema(c)
    yield c
    c.close()


def _row(**over):
    row = {
        "pdf_stem": "EAE621-00 — PM", "sida": 3, "text_hash": "h1",
        "schemaversion": 1, "profile": "Standard", "model": "m1",
        "raw": '{"observationer": []}', "parsed": {"observationer": []},
        "n_observationer": 0,
    }
    row.update(over)
    return row


def test_fresh_db_is_version_15(tmp_path):
    c = connect(tmp_path / "fresh.db")
    try:
        init_schema(c)
        assert SCHEMA_VERSION == 15
        assert schema_version(c) == 15
        assert c.execute("PRAGMA user_version").fetchone()[0] == 15
    finally:
        c.close()


def test_migration_from_14_adds_table(tmp_path):
    path = tmp_path / "v14.db"
    c = connect(path)
    init_schema(c)
    c.execute("DROP TABLE ontology_extractions")
    c.execute("DELETE FROM schema_version WHERE version = 15")
    c.execute("PRAGMA user_version = 14")
    c.commit()
    c.close()
    c = connect(path)
    try:
        init_schema(c)
        assert db.list_ontology_extractions(c) == []
        assert schema_version(c) == 15
    finally:
        c.close()


def test_db_roundtrip_and_overwrite(conn):
    assert db.get_ontology_extraction(conn, "EAE621-00 — PM", 3) is None
    db.upsert_ontology_extraction(conn, _row())
    db.upsert_ontology_extraction(
        conn,
        _row(
            text_hash="h2", model="m2", n_observationer=1,
            parsed={"observationer": [{"utdrag": "å ä ö"}]},
        ),
    )
    rows = db.list_ontology_extractions(conn)
    assert len(rows) == 1
    got = db.get_ontology_extraction(conn, "EAE621-00 — PM", 3)
    assert got == rows[0]
    assert got["text_hash"] == "h2" and got["model"] == "m2"
    assert got["n_observationer"] == 1
    assert got["parsed"] == {"observationer": [{"utdrag": "å ä ö"}]}
    assert got["extracted_at"]


def test_list_is_sorted_by_stem_and_page(conn):
    db.upsert_ontology_extraction(conn, _row(pdf_stem="b", sida=1))
    db.upsert_ontology_extraction(conn, _row(pdf_stem="a", sida=2))
    db.upsert_ontology_extraction(conn, _row(pdf_stem="a", sida=1))
    assert [(r["pdf_stem"], r["sida"]) for r in db.list_ontology_extractions(conn)] == [
        ("a", 1), ("a", 2), ("b", 1),
    ]
