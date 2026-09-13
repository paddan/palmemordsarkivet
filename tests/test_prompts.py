# tests/test_prompts.py
"""Tester för redigerbara systempromptar (src/prompts.py)."""

from __future__ import annotations

import json

import prompts


def test_default_nar_fil_saknas(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(prompts, "PROMPTS_FILE", tmp_path / "saknas.json")
    assert prompts.rag_prompt() == prompts.SYSTEM_PROMPT
    assert prompts.mcp_prompt() == prompts.MCP_SYSTEM_PROMPT
    assert prompts.load_prompts() == {}


def test_roundtrip(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(prompts, "PROMPTS_FILE", tmp_path / "prompts.json")
    prompts.save_prompts({"rag": "Ny RAG-prompt", "mcp": "Ny MCP-prompt"})
    assert prompts.rag_prompt() == "Ny RAG-prompt"
    assert prompts.mcp_prompt() == "Ny MCP-prompt"


def test_partiell_fil_faller_tillbaka_per_nyckel(tmp_path, monkeypatch) -> None:
    fil = tmp_path / "prompts.json"
    fil.write_text(json.dumps({"rag": "Bara RAG"}), encoding="utf-8")
    monkeypatch.setattr(prompts, "PROMPTS_FILE", fil)
    assert prompts.rag_prompt() == "Bara RAG"
    assert prompts.mcp_prompt() == prompts.MCP_SYSTEM_PROMPT


def test_trasig_fil_faller_tillbaka(tmp_path, monkeypatch) -> None:
    fil = tmp_path / "prompts.json"
    fil.write_text("{inte json", encoding="utf-8")
    monkeypatch.setattr(prompts, "PROMPTS_FILE", fil)
    assert prompts.load_prompts() == {}
    assert prompts.rag_prompt() == prompts.SYSTEM_PROMPT


def test_tomt_varde_raknas_inte(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(prompts, "PROMPTS_FILE", tmp_path / "prompts.json")
    prompts.save_prompts({"rag": "  ", "mcp": "OK"})
    lagrat = json.loads((tmp_path / "prompts.json").read_text(encoding="utf-8"))
    assert lagrat == {"mcp": "OK"}
    assert prompts.rag_prompt() == prompts.SYSTEM_PROMPT


def test_inledande_och_avslutande_blanksteg_bevaras(tmp_path, monkeypatch) -> None:
    """Ett sparat fält ska bevaras ordagrant — bara helt tomma värden filtreras."""
    monkeypatch.setattr(prompts, "PROMPTS_FILE", tmp_path / "prompts.json")
    prompts.save_prompts({"rag": "\n  Rad ett\n\n"})
    assert prompts.rag_prompt() == "\n  Rad ett\n\n"


def test_save_lamnar_ingen_tempfil(tmp_path, monkeypatch) -> None:
    """Atomisk skrivning ska inte lämna kvar någon .tmp-fil."""
    monkeypatch.setattr(prompts, "PROMPTS_FILE", tmp_path / "prompts.json")
    prompts.save_prompts({"mcp": "Ny MCP"})
    assert [p.name for p in tmp_path.iterdir()] == ["prompts.json"]


def test_ask_anvander_override_vid_anropstillfallet(tmp_path, monkeypatch) -> None:
    """ask.py ska hämta prompten vid anropet, inte binda den vid import."""
    monkeypatch.setattr(prompts, "PROMPTS_FILE", tmp_path / "prompts.json")
    import ask

    assert ask.rag_prompt() == prompts.SYSTEM_PROMPT
    assert ask.mcp_prompt() == prompts.MCP_SYSTEM_PROMPT
    prompts.save_prompts({"rag": "Overridd RAG", "mcp": "Overridd MCP"})
    assert ask.rag_prompt() == "Overridd RAG"
    assert ask.mcp_prompt() == "Overridd MCP"
