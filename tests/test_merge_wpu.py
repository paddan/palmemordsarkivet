"""Tester för merge_wpu — decide() + _process_one med radering av förlorare."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import db as state_db
import merge_wpu
from merge_wpu import _process_one, cleanup_phantom_decisions, decide


def test_decide_wpu_wins() -> None:
    assert decide(wpu_score=80, palme_score=60, margin=5) == "wpu"


def test_decide_palme_wins() -> None:
    assert decide(wpu_score=60, palme_score=80, margin=5) == "palme"


def test_decide_tie_within_margin() -> None:
    assert decide(wpu_score=70, palme_score=72, margin=5) == "tie"
    assert decide(wpu_score=70, palme_score=75, margin=5) == "tie"  # diff == margin


def test_decide_just_above_margin() -> None:
    assert decide(wpu_score=76, palme_score=70, margin=5) == "wpu"


# --- _process_one ---

@pytest.fixture
def db_env(tmp_path, monkeypatch):
    """Sätt STATE_DB till en tmp-fil så workers/state_db.connect() går dit."""
    db_path = tmp_path / "state.db"
    monkeypatch.setenv("STATE_DB", str(db_path))
    conn = state_db.connect(db_path)
    state_db.init_schema(conn)
    conn.close()
    return db_path


def _setup(tmp_path: Path) -> tuple[Path, Path, Path]:
    text = tmp_path / "text"
    ocr = tmp_path / "ocr"
    files_wpu = tmp_path / "files_wpu"
    for d in (text, ocr, files_wpu):
        d.mkdir()
    return text, ocr, files_wpu


def _fake_score(text: str, use_hunspell: bool = False) -> dict:
    """Returnerar score = första raden tolkad som int."""
    return {"score": int(text.splitlines()[0])}


def test_skips_when_wpu_text_missing(tmp_path: Path, db_env: Path) -> None:
    """När wpu-text saknas (OCR inte klar) ska filen hoppa över utan att
    markeras decided — annars hindras nästa körning från att försöka igen
    när OCR producerat texten."""
    text, ocr, files_wpu = _setup(tmp_path)
    pdf = files_wpu / "DA14259-00.pdf"
    pdf.write_bytes(b"x")
    merge_wpu._PALME_MAP = {}
    merge_wpu._USE_HUNSPELL = False

    res = _process_one(str(pdf), str(text), str(ocr), False, False, 5)

    assert res["category"] == "skip"
    assert "saknar text" in res["lines"][0]
    conn = state_db.connect(db_env)
    assert not state_db.wpu_decided(conn, "DA14259-00")
    conn.close()


def test_retries_after_text_appears(tmp_path: Path, db_env: Path) -> None:
    """Första körningen saknar text → skip utan markör. Andra körningen,
    efter att OCR producerat texten, ska göra ett riktigt beslut."""
    text, ocr, files_wpu = _setup(tmp_path)
    pdf = files_wpu / "DA14259-00.pdf"
    pdf.write_bytes(b"x")
    merge_wpu._PALME_MAP = {}
    merge_wpu._USE_HUNSPELL = False

    first = _process_one(str(pdf), str(text), str(ocr), False, False, 5)
    assert first["category"] == "skip"

    wpu_txt = text / "DA14259-00.txt"
    wpu_txt.write_text("70\nhej", encoding="utf-8")

    with patch.object(merge_wpu, "score_text", _fake_score):
        second = _process_one(str(pdf), str(text), str(ocr), False, False, 5)

    assert second["category"] == "new"
    conn = state_db.connect(db_env)
    assert state_db.wpu_decided(conn, "DA14259-00")
    conn.close()


def test_unmatched_wpu_kept_as_new(tmp_path: Path, db_env: Path) -> None:
    text, ocr, files_wpu = _setup(tmp_path)
    pdf = files_wpu / "DA14259-00.pdf"
    pdf.write_bytes(b"x")
    wpu_txt = text / "DA14259-00.txt"
    wpu_txt.write_text("70\nhej", encoding="utf-8")
    merge_wpu._PALME_MAP = {}
    merge_wpu._USE_HUNSPELL = False

    with patch.object(merge_wpu, "score_text", _fake_score):
        res = _process_one(str(pdf), str(text), str(ocr), False, False, 5)

    assert res["category"] == "new"
    assert wpu_txt.exists()
    conn = state_db.connect(db_env)
    assert state_db.wpu_decided(conn, "DA14259-00")
    conn.close()


def test_wpu_wins_deletes_palme(tmp_path: Path, db_env: Path) -> None:
    text, ocr, files_wpu = _setup(tmp_path)
    pdf = files_wpu / "DA14259-00.pdf"
    pdf.write_bytes(b"x")
    wpu_txt = text / "DA14259-00.txt"
    wpu_txt.write_text("80\nbra wpu-text", encoding="utf-8")
    palme_txt = text / "1 — palme — DA-14259.txt"
    palme_txt.write_text("60\ndålig palme", encoding="utf-8")
    palme_pdf = ocr / "1 — palme — DA-14259.pdf"
    palme_pdf.write_bytes(b"pdf")

    merge_wpu._PALME_MAP = {("DA", 14259, 0, ""): [palme_txt]}
    merge_wpu._USE_HUNSPELL = False

    with patch.object(merge_wpu, "score_text", _fake_score):
        res = _process_one(str(pdf), str(text), str(ocr), False, False, 5)

    assert res["category"] == "better"
    assert wpu_txt.exists()
    assert not palme_txt.exists()
    assert not palme_pdf.exists()


def test_palme_wins_deletes_wpu(tmp_path: Path, db_env: Path) -> None:
    text, ocr, files_wpu = _setup(tmp_path)
    pdf = files_wpu / "DA14259-00.pdf"
    pdf.write_bytes(b"x")
    wpu_txt = text / "DA14259-00.txt"
    wpu_txt.write_text("50\nwpu", encoding="utf-8")
    wpu_pdf = ocr / "DA14259-00.pdf"
    wpu_pdf.write_bytes(b"pdf")
    palme_txt = text / "1 — palme — DA-14259.txt"
    palme_txt.write_text("90\nbra palme", encoding="utf-8")

    merge_wpu._PALME_MAP = {("DA", 14259, 0, ""): [palme_txt]}
    merge_wpu._USE_HUNSPELL = False

    with patch.object(merge_wpu, "score_text", _fake_score):
        res = _process_one(str(pdf), str(text), str(ocr), False, False, 5)

    assert res["category"] == "lost"
    assert not wpu_txt.exists()
    assert not wpu_pdf.exists()
    assert palme_txt.exists()


def test_tie_keeps_both(tmp_path: Path, db_env: Path) -> None:
    text, ocr, files_wpu = _setup(tmp_path)
    pdf = files_wpu / "DA14259-00.pdf"
    pdf.write_bytes(b"x")
    wpu_txt = text / "DA14259-00.txt"
    wpu_txt.write_text("70\nwpu", encoding="utf-8")
    palme_txt = text / "1 — palme — DA-14259.txt"
    palme_txt.write_text("72\npalme", encoding="utf-8")

    merge_wpu._PALME_MAP = {("DA", 14259, 0, ""): [palme_txt]}
    merge_wpu._USE_HUNSPELL = False

    with patch.object(merge_wpu, "score_text", _fake_score):
        res = _process_one(str(pdf), str(text), str(ocr), False, False, 5)

    assert res["category"] == "kept"
    assert wpu_txt.exists()
    assert palme_txt.exists()


def test_cleanup_phantom_decisions(tmp_path: Path, db_env: Path) -> None:
    """Raderar wpu_decisions-rader vars text saknas på disk. Behåller övriga."""
    text, _, _ = _setup(tmp_path)
    (text / "har-text.txt").write_text("ok", encoding="utf-8")

    conn = state_db.connect(db_env)
    state_db.mark_wpu_decided(conn, "har-text")
    state_db.mark_wpu_decided(conn, "saknar-text-1")
    state_db.mark_wpu_decided(conn, "saknar-text-2")

    removed = cleanup_phantom_decisions(conn, text)
    assert removed == 2

    assert state_db.wpu_decided(conn, "har-text")
    assert not state_db.wpu_decided(conn, "saknar-text-1")
    assert not state_db.wpu_decided(conn, "saknar-text-2")

    # Idempotent — andra körningen raderar inget.
    assert cleanup_phantom_decisions(conn, text) == 0
    conn.close()


def test_dry_run_does_not_delete(tmp_path: Path, db_env: Path) -> None:
    text, ocr, files_wpu = _setup(tmp_path)
    pdf = files_wpu / "DA14259-00.pdf"
    pdf.write_bytes(b"x")
    wpu_txt = text / "DA14259-00.txt"
    wpu_txt.write_text("80\nbra wpu", encoding="utf-8")
    palme_txt = text / "1 — palme — DA-14259.txt"
    palme_txt.write_text("60\ndålig palme", encoding="utf-8")

    merge_wpu._PALME_MAP = {("DA", 14259, 0, ""): [palme_txt]}
    merge_wpu._USE_HUNSPELL = False

    with patch.object(merge_wpu, "score_text", _fake_score):
        res = _process_one(str(pdf), str(text), str(ocr), True, False, 5)

    assert res["category"] == "better"
    assert palme_txt.exists()  # ej raderad
    conn = state_db.connect(db_env)
    assert not state_db.wpu_decided(conn, "DA14259-00")  # ingen marker
    conn.close()
