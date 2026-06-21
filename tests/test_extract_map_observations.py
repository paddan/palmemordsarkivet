from pathlib import Path
import sys
import asyncio

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import db  # noqa: E402
import extract_map_observations as emo  # noqa: E402


def _conn(tmp_path):
    conn = db.connect(tmp_path / "state.db")
    db.init_schema(conn)
    return conn


def test_select_candidate_pages_skips_short_pages_and_existing_candidates(tmp_path):
    conn = _conn(tmp_path)
    text = "Olof Palme sågs vid Grand omkring klockan 21.15 enligt vittnet. " * 4
    db.record_map_observation_candidate(
        conn,
        pdf_stem="doc",
        page_num=3,
        person="Olof Palme",
        raw_place="Grand",
        place_name="Biografen Grand",
        lat=59.34057,
        lon=18.06024,
        time="21:15",
        uncertainty="ca",
        nr="doc",
        sida=3,
        quote="Olof Palme sågs vid Grand omkring klockan 21.15.",
        note=None,
        confidence="medium",
        place_match="exact",
        model="test-model",
    )

    pages = [text, "bild", text, text]

    assert emo.select_candidate_pages(conn, "doc", pages) == [(1, text), (4, text)]


def test_select_candidate_pages_skips_pages_with_empty_extraction(tmp_path):
    conn = _conn(tmp_path)
    text = "Olof Palme sågs vid Grand omkring klockan 21.15 enligt vittnet. " * 4
    db.mark_map_observation_extracted(
        conn,
        pdf_stem="doc",
        page_num=1,
        model="test-model",
        observations=0,
    )

    assert emo.select_candidate_pages(conn, "doc", [text, text]) == [(2, text)]


def test_extract_doc_stores_candidates(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    txt_dir = tmp_path / "text"
    txt_dir.mkdir()
    body = "Olof Palme sågs vid Grand omkring klockan 21.15 enligt vittnet. " * 4
    txt_path = txt_dir / "2055 — Grandbesökare.txt"
    txt_path.write_text(body, encoding="utf-8")

    async def fake_llm(text: str, cfg: dict) -> str:
        return """
        {"observationer": [{
          "person": "Olof Palme",
          "plats": "Grand",
          "tid": "ca 21.15",
          "citat": "Olof Palme sågs vid Grand omkring klockan 21.15.",
          "notering": "vittnesuppgift",
          "confidence": "high"
        }]}
        """

    monkeypatch.setattr(emo, "_call_llm", fake_llm)
    cfg = {"provider": "claude", "model": "test-model", "base_url": "", "api_key": ""}
    place_index = [{
        "name": "Biografen Grand",
        "lat": 59.34057,
        "lon": 18.06024,
        "aliases": ["Biografen Grand", "Grand"],
    }]

    count = asyncio.run(
        emo.extract_doc(
            conn,
            txt_path,
            cfg=cfg,
            dry_run=False,
            place_index=place_index,
            jobs=1,
        )
    )

    rows = db.list_map_observation_candidates(conn)
    assert count == 1
    assert rows[0]["person"] == "Olof Palme"
    assert rows[0]["place_name"] == "Biografen Grand"
    assert rows[0]["time"] == "21:15"
    assert rows[0]["nr"] == "2055"
    assert rows[0]["sida"] == 1


def test_extract_doc_marks_empty_llm_result_as_processed(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    txt_dir = tmp_path / "text"
    txt_dir.mkdir()
    body = "Olof Palme sågs vid Grand omkring klockan 21.15 enligt vittnet. " * 4
    txt_path = txt_dir / "2055 — Grandbesökare.txt"
    txt_path.write_text(body, encoding="utf-8")

    async def fake_llm(text: str, cfg: dict) -> str:
        return '{"observationer": []}'

    monkeypatch.setattr(emo, "_call_llm", fake_llm)
    cfg = {"provider": "claude", "model": "test-model", "base_url": "", "api_key": ""}

    count = asyncio.run(
        emo.extract_doc(
            conn,
            txt_path,
            cfg=cfg,
            dry_run=False,
            place_index=[],
            jobs=1,
        )
    )

    assert count == 1
    assert db.list_map_observation_candidates(conn) == []
    assert db.map_observation_extracted(conn, "2055 — Grandbesökare", 1)
    assert emo.select_candidate_pages(conn, "2055 — Grandbesökare", [body]) == []


def test_extract_doc_dry_run_does_not_store(tmp_path):
    conn = _conn(tmp_path)
    txt_dir = tmp_path / "text"
    txt_dir.mkdir()
    body = "Olof Palme sågs vid Grand omkring klockan 21.15 enligt vittnet. " * 4
    txt_path = txt_dir / "2055 — Grandbesökare.txt"
    txt_path.write_text(body + "\f" + body, encoding="utf-8")
    cfg = {"provider": "claude", "model": "test-model", "base_url": "", "api_key": ""}

    count = asyncio.run(
        emo.extract_doc(
            conn,
            txt_path,
            cfg=cfg,
            dry_run=True,
            place_index=[],
            jobs=1,
        )
    )

    assert count == 2
    assert db.list_map_observation_candidates(conn) == []
