"""Ingest fyller document_meta och LanceDB bär bara nr/titel."""

from __future__ import annotations

import lancedb
import pyarrow as pa
import pytest
from ingest import (
    DROPPED_NAME_FIELDS,
    LANCE_NAME_FIELDS,
    _chunk_schema,
    _chunka_fil,
    _oppna_tabell,
    _uppdatera_dokumentmeta,
)

import db
from dokumentmeta import lance_name_fields

PALME = "280.8 — Om vykort — 2020-08-15 — 2020-10-21 — HI-7504-10 — 2"
PALME2 = "1361 — Iakttagelser — 2021-12-11 — Z-8624 — 8"
WPU = "Pol-1986-E15-A12 Fotografier"


class _Ctx:
    def __init__(self):
        self.lines: list[tuple[str, str]] = []

    def log(self, msg, level="info"):
        self.lines.append((level, msg))


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    c = db.connect()
    db.init_schema(c)
    yield c
    c.close()


def _texts(tmp_path, *stems):
    d = tmp_path / "text"
    d.mkdir(exist_ok=True)
    for s in stems:
        (d / f"{s}.txt").write_text("x", encoding="utf-8")
    return d


def test_meta_refresh_covers_all_text_files(tmp_path, conn):
    d = _texts(tmp_path, PALME, WPU)
    assert _uppdatera_dokumentmeta(conn, d, None, _Ctx()) == 2
    p = db.get_document_meta(conn, PALME)
    w = db.get_document_meta(conn, WPU)
    assert p["arkiv"] == "palme" and p["upplagt"] == "2020-10-21"
    assert w["arkiv"] == "wpu"


def test_meta_refresh_prunes_removed_files(tmp_path, conn):
    d = _texts(tmp_path, PALME, PALME2)
    _uppdatera_dokumentmeta(conn, d, None, _Ctx())
    (d / f"{PALME2}.txt").unlink()
    _uppdatera_dokumentmeta(conn, d, None, _Ctx())
    assert db.get_document_meta(conn, PALME2) is None
    assert db.get_document_meta(conn, PALME) is not None


def test_empty_listing_does_not_prune(tmp_path, conn):
    d = _texts(tmp_path, PALME)
    _uppdatera_dokumentmeta(conn, d, None, _Ctx())
    (d / f"{PALME}.txt").unlink()
    ctx = _Ctx()
    assert _uppdatera_dokumentmeta(conn, d, None, ctx) == 0
    assert db.get_document_meta(conn, PALME) is not None
    assert any(level == "warning" for level, _ in ctx.lines)


def test_only_stems_updates_one_and_prunes_nothing(tmp_path, conn):
    d = _texts(tmp_path, PALME, PALME2, WPU)
    _uppdatera_dokumentmeta(conn, d, None, _Ctx())
    conn.execute("UPDATE document_meta SET titel = 'gammal'")
    conn.commit()
    assert _uppdatera_dokumentmeta(conn, d, {PALME}, _Ctx()) == 1
    assert db.get_document_meta(conn, PALME)["titel"] != "gammal"
    assert db.get_document_meta(conn, PALME2)["titel"] == "gammal"
    assert db.get_document_meta(conn, WPU) is not None


def test_meta_refresh_logs_summary_and_warnings(tmp_path, conn):
    head = "32 — Titel — 2020-06-12 — 2020-07-"
    tail = " — Alla"
    stem = head + "x" * (180 - len(head) - len(tail)) + tail
    d = _texts(tmp_path, stem, PALME)
    ctx = _Ctx()
    _uppdatera_dokumentmeta(conn, d, None, ctx)
    infos = [m for lv, m in ctx.lines if lv == "info"]
    debugs = [m for lv, m in ctx.lines if lv == "debug"]
    assert len(infos) == 1 and "Dokumentmetadata: 2 palme, 0 wpu, 1 avkortade" in infos[0]
    assert debugs and all(len(m) < 2000 for m in debugs)


def test_old_table_loses_misparsed_columns(tmp_path):
    old = ["nr", "titel", "bestallt", "upplagt", "anmarkning", "antal_sidor"]
    schema = pa.schema([
        pa.field("vector", pa.list_(pa.float32(), 4)),
        pa.field("text", pa.string()),
        pa.field("source", pa.string()),
        pa.field("page", pa.int32()),
        pa.field("chunk_idx", pa.int32()),
        pa.field("mtime", pa.float64()),
        *[pa.field(f, pa.string()) for f in old],
    ])
    db_ = lancedb.connect(str(tmp_path / "lance"))
    t = db_.create_table("chunks", schema=schema)
    t.add([{
        "vector": [0.1, 0.2, 0.3, 0.4], "text": "hej", "source": "a.txt", "page": 1,
        "chunk_idx": 0, "mtime": 1.0, "nr": "1", "titel": "T", "bestallt": "b",
        "upplagt": "u", "anmarkning": "a", "antal_sidor": "3",
    }])
    ctx = _Ctx()
    table = _oppna_tabell(db_, False, ctx)
    assert not set(DROPPED_NAME_FIELDS) & set(table.schema.names)
    rows = table.search().select(["nr", "titel", "text"]).limit(5).to_list()
    assert len(rows) == 1 and rows[0]["nr"] == "1" and rows[0]["titel"] == "T"
    assert any("Migrerar tabell" in m for _, m in ctx.lines)


def test_new_table_schema_has_only_nr_and_titel():
    names = _chunk_schema().names
    assert set(LANCE_NAME_FIELDS) <= set(names)
    assert not set(DROPPED_NAME_FIELDS) & set(names)


def test_chunk_rows_keep_legacy_nr_and_titel():
    raw = "Det här är en tillräckligt lång mening om arkivet och dess dokument. " * 5
    rows = _chunka_fil(raw, "a.txt", lance_name_fields(PALME), 1.0, 800, 150)
    assert rows
    for r in rows:
        assert set(r) == {"text", "source", "page", "chunk_idx", "mtime", "nr", "titel"}
        assert r["nr"] == "280.8"


def test_select_columns_exclude_dropped_fields():
    import ask

    assert not set(DROPPED_NAME_FIELDS) & set(ask.SELECT_COLS)


def test_warnings_group_by_text_before_colon(tmp_path, conn):
    a = "Pol-2020-13-45_E1-00_A"
    b = "Pol-2021-14-50_E2-00_B"
    d = _texts(tmp_path, a, b)
    ctx = _Ctx()
    _uppdatera_dokumentmeta(conn, d, None, ctx)
    debugs = [m for lv, m in ctx.lines if lv == "debug"]
    assert len(debugs) == 1 and "för 2 dokument" in debugs[0]
