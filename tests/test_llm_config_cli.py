"""Tester för llm_config_cli — visa/sätta LLM-konfig utan webui."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import config
import llm_config_cli


def _patch_cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "llm_config.json")


def test_show_without_file_uses_defaults(tmp_path, monkeypatch, capsys) -> None:
    _patch_cfg(tmp_path, monkeypatch)
    assert llm_config_cli.main([]) == 0
    out = capsys.readouterr().out
    assert "claude" in out
    assert "ingen sparad konfig" in out


def test_set_model_persists(tmp_path, monkeypatch, capsys) -> None:
    _patch_cfg(tmp_path, monkeypatch)
    assert llm_config_cli.main(["--model", "claude-haiku-4-5-20251001"]) == 0
    stored = json.loads((tmp_path / "llm_config.json").read_text(encoding="utf-8"))
    assert stored["model"] == "claude-haiku-4-5-20251001"
    assert stored["provider"] == "claude"


def test_provider_switch_resets_model_to_default(tmp_path, monkeypatch, capsys) -> None:
    _patch_cfg(tmp_path, monkeypatch)
    llm_config_cli.main(["--model", "claude-opus-4-8"])
    assert llm_config_cli.main(["--provider", "openai"]) == 0
    stored = json.loads((tmp_path / "llm_config.json").read_text(encoding="utf-8"))
    assert stored["provider"] == "openai"
    assert stored["model"] == "gpt-4o-mini"
    assert stored["backend_name"] == "OpenAI"


def test_provider_switch_with_model_keeps_it(tmp_path, monkeypatch) -> None:
    _patch_cfg(tmp_path, monkeypatch)
    llm_config_cli.main(["--provider", "openai", "--model", "deepseek-chat",
                         "--base-url", "https://api.deepseek.com/v1"])
    stored = json.loads((tmp_path / "llm_config.json").read_text(encoding="utf-8"))
    assert stored["model"] == "deepseek-chat"
    assert stored["base_url"] == "https://api.deepseek.com/v1"


def test_invalid_provider_exits_2(tmp_path, monkeypatch, capsys) -> None:
    _patch_cfg(tmp_path, monkeypatch)
    assert llm_config_cli.main(["--provider", "gemini"]) == 2


def test_reset_removes_file(tmp_path, monkeypatch) -> None:
    _patch_cfg(tmp_path, monkeypatch)
    llm_config_cli.main(["--model", "x"])
    assert (tmp_path / "llm_config.json").exists()
    assert llm_config_cli.main(["--reset"]) == 0
    assert not (tmp_path / "llm_config.json").exists()


def test_reset_with_other_flags_exits_2(tmp_path, monkeypatch) -> None:
    _patch_cfg(tmp_path, monkeypatch)
    assert llm_config_cli.main(["--reset", "--model", "x"]) == 2
