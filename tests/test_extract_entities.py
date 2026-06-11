"""Tester för extract_entities — parsning och sidurval."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from graph.extract_entities import parse_extraction


def test_parse_valid_json() -> None:
    raw = ('{"entiteter": [{"typ": "person", "namn": "Stig Engström"}],'
           ' "relationer": [{"fran": "Stig Engström", "typ": "sågs vid",'
           ' "till": "Dekorima"}]}')
    out = parse_extraction(raw)
    assert out["entiteter"][0]["namn"] == "Stig Engström"
    assert out["relationer"][0]["typ"] == "sågs vid"


def test_parse_json_in_markdown_fence() -> None:
    raw = 'Här är resultatet:\n```json\n{"entiteter": [], "relationer": []}\n```'
    assert parse_extraction(raw) == {"entiteter": [], "relationer": []}


def test_parse_garbage_returns_empty() -> None:
    assert parse_extraction("ingen json här") == {"entiteter": [], "relationer": []}
    assert parse_extraction("") == {"entiteter": [], "relationer": []}
    assert parse_extraction('{"entiteter": [trasig') == {"entiteter": [], "relationer": []}


def test_parse_filters_invalid_entries() -> None:
    raw = ('{"entiteter": [{"typ": "person", "namn": "OK"},'
           ' {"typ": "djur", "namn": "Hund"},'
           ' {"typ": "person", "namn": ""}],'
           ' "relationer": [{"fran": "OK", "typ": "", "till": "X"},'
           ' {"fran": "OK", "typ": "känner", "till": "X"}]}')
    out = parse_extraction(raw)
    assert [e["namn"] for e in out["entiteter"]] == ["OK"]
    assert len(out["relationer"]) == 1


def _conn(tmp_path):
    import db
    conn = db.connect(tmp_path / "state.db")
    db.init_schema(conn)
    return conn


def test_select_pages_skips_extracted_and_image_pages(tmp_path) -> None:
    import db
    from graph.extract_entities import select_pages
    text = "Förhör med Stig Engström hölls på Norrmalmspolisen " * 5
    pages = [text, "x", text, text]  # sida 2 = bildsida (<100 alnum)
    conn = _conn(tmp_path)
    db.record_doc_entities(conn, pdf_stem="doc", page_num=3,
                           payload={"entiteter": [], "relationer": []}, model="t")
    out = select_pages(conn, "doc", pages)
    assert [p for p, _ in out] == [1, 4]


def test_select_pages_empty_doc(tmp_path) -> None:
    from graph.extract_entities import select_pages
    assert select_pages(_conn(tmp_path), "doc", [""]) == []


def test_resolve_provider_cfg_defaults_to_haiku() -> None:
    from graph.extract_entities import DEFAULT_CLAUDE_MODEL, resolve_provider_cfg
    cfg = resolve_provider_cfg({})
    assert cfg == {"provider": "claude", "model": DEFAULT_CLAUDE_MODEL, "base_url": ""}


def test_resolve_provider_cfg_uses_saved_config() -> None:
    from graph.extract_entities import resolve_provider_cfg
    saved = {"provider": "openai", "model": "deepseek-chat",
             "base_url": "https://api.deepseek.com/v1"}
    cfg = resolve_provider_cfg(saved)
    assert cfg == {"provider": "openai", "model": "deepseek-chat",
                   "base_url": "https://api.deepseek.com/v1"}


def test_resolve_provider_cfg_cli_overrides_saved() -> None:
    from graph.extract_entities import resolve_provider_cfg
    saved = {"provider": "claude", "model": "claude-opus-4-8", "base_url": ""}
    cfg = resolve_provider_cfg(saved, model="claude-haiku-4-5-20251001")
    assert cfg["model"] == "claude-haiku-4-5-20251001"


def test_resolve_provider_cfg_provider_switch_drops_saved_model() -> None:
    from graph.extract_entities import OPENAI_DEFAULT_MODEL, resolve_provider_cfg
    saved = {"provider": "claude", "model": "claude-opus-4-8", "base_url": ""}
    cfg = resolve_provider_cfg(saved, provider="openai")
    assert cfg["model"] == OPENAI_DEFAULT_MODEL


def test_extract_doc_stores_payload(tmp_path, monkeypatch) -> None:
    import asyncio
    import db
    from graph import extract_entities as ee

    async def fake_llm(text: str, cfg: dict) -> str:
        return '{"entiteter": [{"typ": "person", "namn": "Lisbeth Palme"}], "relationer": []}'

    monkeypatch.setattr(ee, "_call_llm", fake_llm)
    txt_dir = tmp_path / "text"
    txt_dir.mkdir()
    body = "Vittnesförhör med Lisbeth Palme angående händelsen vid Dekorima " * 4
    (txt_dir / "doc.txt").write_text(body + "\f" + body, encoding="utf-8")
    conn = _conn(tmp_path)
    cfg = {"provider": "claude", "model": "test-model", "base_url": "", "api_key": ""}

    n = asyncio.run(ee.extract_doc(conn, txt_dir / "doc.txt", cfg=cfg, dry_run=False))
    assert n == 2
    rows = list(db.iter_doc_entities(conn))
    assert len(rows) == 2
    assert rows[0]["payload"]["entiteter"][0]["namn"] == "Lisbeth Palme"
    row = conn.execute(
        "SELECT model FROM doc_entities WHERE page_num=1"
    ).fetchone()
    assert row["model"] == "test-model"
    # Idempotens: andra körningen gör inget
    assert asyncio.run(ee.extract_doc(conn, txt_dir / "doc.txt", cfg=cfg, dry_run=False)) == 0


def test_extract_doc_continues_after_llm_error(tmp_path, monkeypatch) -> None:
    """Ett LLM-fel på en sida får inte stoppa batchen — sidan loggas och hoppas."""
    import asyncio
    import db
    from graph import extract_entities as ee

    calls = {"n": 0}

    async def flaky_llm(text: str, cfg: dict) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("nätverksfel")
        return '{"entiteter": [], "relationer": []}'

    monkeypatch.setattr(ee, "_call_llm", flaky_llm)
    txt_dir = tmp_path / "text"
    txt_dir.mkdir()
    body = "Vittnesförhör med Lisbeth Palme angående händelsen vid Dekorima " * 4
    (txt_dir / "doc.txt").write_text(body + "\f" + body, encoding="utf-8")
    conn = _conn(tmp_path)
    cfg = {"provider": "claude", "model": "test-model", "base_url": "", "api_key": ""}

    n = asyncio.run(ee.extract_doc(conn, txt_dir / "doc.txt", cfg=cfg, dry_run=False))
    assert n == 2  # båda sidorna försöktes
    rows = list(db.iter_doc_entities(conn))
    assert [r["page_num"] for r in rows] == [2]  # bara sida 2 lyckades


def test_fmt_eta() -> None:
    from graph.extract_entities import fmt_eta
    assert fmt_eta(272) == "4m32s"
    assert fmt_eta(7500) == "2h05m"
    assert fmt_eta(0) == "0m00s"


def test_extract_doc_calls_on_page_even_on_error(tmp_path, monkeypatch) -> None:
    import asyncio
    from graph import extract_entities as ee

    calls = {"n": 0}

    async def flaky_llm(text: str, cfg: dict) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("nätverksfel")
        return '{"entiteter": [], "relationer": []}'

    monkeypatch.setattr(ee, "_call_llm", flaky_llm)
    txt_dir = tmp_path / "text"
    txt_dir.mkdir()
    body = "Vittnesförhör med Lisbeth Palme angående händelsen vid Dekorima " * 4
    (txt_dir / "doc.txt").write_text(body + "\f" + body, encoding="utf-8")
    conn = _conn(tmp_path)

    seen: list[int] = []
    cfg = {"provider": "claude", "model": "test-model", "base_url": "", "api_key": ""}
    asyncio.run(ee.extract_doc(conn, txt_dir / "doc.txt", cfg=cfg,
                               dry_run=False, on_page=seen.append))
    assert seen == [1, 2]  # callback även för den misslyckade sidan


def test_extract_doc_timeout_is_logged_and_skipped(tmp_path, monkeypatch) -> None:
    import asyncio
    import db
    from graph import extract_entities as ee

    async def slow_llm(text: str, cfg: dict) -> str:
        await asyncio.sleep(5)
        return "{}"

    monkeypatch.setattr(ee, "_call_llm", slow_llm)
    txt_dir = tmp_path / "text"
    txt_dir.mkdir()
    body = "Vittnesförhör med Lisbeth Palme angående händelsen vid Dekorima " * 4
    (txt_dir / "doc.txt").write_text(body, encoding="utf-8")
    conn = _conn(tmp_path)
    cfg = {"provider": "claude", "model": "test-model", "base_url": "", "api_key": ""}

    n = asyncio.run(ee.extract_doc(conn, txt_dir / "doc.txt", cfg=cfg,
                                   dry_run=False, timeout=0.05))
    assert n == 1
    assert list(db.iter_doc_entities(conn)) == []  # inget sparat — timeout


def test_extract_doc_parallel_jobs_process_all_pages(tmp_path, monkeypatch) -> None:
    import asyncio
    import db
    from graph import extract_entities as ee

    in_flight = {"now": 0, "max": 0}

    async def tracking_llm(text: str, cfg: dict) -> str:
        in_flight["now"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["now"])
        await asyncio.sleep(0.02)
        in_flight["now"] -= 1
        return '{"entiteter": [], "relationer": []}'

    monkeypatch.setattr(ee, "_call_llm", tracking_llm)
    txt_dir = tmp_path / "text"
    txt_dir.mkdir()
    body = "Vittnesförhör med Lisbeth Palme angående händelsen vid Dekorima " * 4
    (txt_dir / "doc.txt").write_text("\f".join([body] * 6), encoding="utf-8")
    conn = _conn(tmp_path)
    cfg = {"provider": "claude", "model": "test-model", "base_url": "", "api_key": ""}

    seen: list[int] = []
    n = asyncio.run(ee.extract_doc(conn, txt_dir / "doc.txt", cfg=cfg,
                                   dry_run=False, jobs=3, on_page=seen.append))
    assert n == 6
    assert sorted(seen) == [1, 2, 3, 4, 5, 6]
    assert len(list(db.iter_doc_entities(conn))) == 6
    assert 1 < in_flight["max"] <= 3  # faktiskt parallellt men max 3
