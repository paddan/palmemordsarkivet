"""Tester för answer_entities — parsning och LLM-konfigval."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from graph.answer_entities import parse_entity_list


def test_parse_valid_list() -> None:
    raw = '["Stig Engström", "Dekorima", "Skandia"]'
    assert parse_entity_list(raw) == ["Stig Engström", "Dekorima", "Skandia"]


def test_parse_list_in_markdown_fence() -> None:
    raw = 'Här är listan:\n```json\n["Olof Palme"]\n```\nKlart.'
    assert parse_entity_list(raw) == ["Olof Palme"]


def test_parse_garbage_returns_empty() -> None:
    assert parse_entity_list("ingen json här") == []
    assert parse_entity_list("") == []
    assert parse_entity_list('["trasig') == []
    assert parse_entity_list('{"inte": "en lista"}') == []


def test_parse_filters_and_dedups() -> None:
    raw = '["Stig Engström", "", 42, "  ", "stig engström", "Skandia"]'
    assert parse_entity_list(raw) == ["Stig Engström", "Skandia"]


def test_parse_caps_at_max() -> None:
    from graph.answer_entities import MAX_ENTITIES
    raw = "[" + ", ".join(f'"Namn {i}"' for i in range(20)) + "]"
    assert len(parse_entity_list(raw)) == MAX_ENTITIES


def test_parse_skips_leading_citation_brackets() -> None:
    raw = ('Svaret handlar om [Nr 12, sida 3] och nämner: '
           '["Olof Palme", "Stig Engström"]')
    assert parse_entity_list(raw) == ["Olof Palme", "Stig Engström"]


def test_parse_handles_brackets_inside_names() -> None:
    assert parse_entity_list('["Olof [O] Palme"]') == ["Olof [O] Palme"]


def test_parse_degenerate_input_is_fast() -> None:
    import time
    start = time.monotonic()
    assert parse_entity_list("[" * 200_000) == []
    assert time.monotonic() - start < 1.0


def test_resolve_entity_cfg_prefers_haiku(monkeypatch) -> None:
    from graph.answer_entities import DEFAULT_CLAUDE_MODEL, resolve_entity_cfg
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    cfg = resolve_entity_cfg({"provider": "openai", "model": "gpt-4o"})
    assert cfg == {"provider": "claude", "model": DEFAULT_CLAUDE_MODEL,
                   "base_url": "", "api_key": ""}


def test_resolve_entity_cfg_falls_back_to_openai(monkeypatch) -> None:
    from graph.answer_entities import resolve_entity_cfg
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    cfg = resolve_entity_cfg({"provider": "openai", "model": "gpt-4o-mini",
                              "base_url": ""})
    assert cfg == {"provider": "openai", "model": "gpt-4o-mini",
                   "base_url": "", "api_key": "sk-x"}


def test_resolve_entity_cfg_local_base_url_needs_no_key(monkeypatch) -> None:
    from graph.answer_entities import resolve_entity_cfg
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = resolve_entity_cfg({"provider": "openai", "model": "llama3.1:8b",
                              "base_url": "http://localhost:11434/v1"})
    assert cfg is not None and cfg["base_url"] == "http://localhost:11434/v1"


def test_resolve_entity_cfg_no_usable_llm(monkeypatch) -> None:
    from graph.answer_entities import resolve_entity_cfg
    for var in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert resolve_entity_cfg({"provider": "claude"}) is None
    assert resolve_entity_cfg({"provider": "openai", "base_url": ""}) is None
