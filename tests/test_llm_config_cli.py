"""Tester för llm_config_cli — visa/sätta LLM-konfig utan webgränssnittet."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import config  # noqa: E402
import llm_config_cli  # noqa: E402


def test_resolve_runtime_profile_uses_selected_model_and_secret_env() -> None:
    profile = {
        "backend_name": "OpenAI-kompatibel",
        "provider": "openai",
        "model": "privat-modell",
        "base_url": "https://llm.example/v1",
        "api_key_env": "PRIVATE_LLM_TOKEN",
    }
    catalog = {
        "OpenAI-kompatibel": {
            "kind": "openai",
            "model": "fallback",
            "base_url": "http://localhost:11434/v1",
            "env": None,
        }
    }

    runtime = config.resolve_runtime_profile(
        profile, catalog, environ={"PRIVATE_LLM_TOKEN": "hemlig"}
    )

    assert runtime["model"] == "privat-modell"
    assert runtime["api_key"] == "hemlig"
    assert runtime["api_key_env"] == "PRIVATE_LLM_TOKEN"


def test_resolve_runtime_profile_keeps_known_backend_env_compatible() -> None:
    profile = {
        "backend_name": "OpenAI",
        "provider": "openai",
        "model": "gpt-test",
        "base_url": "",
    }
    catalog = {
        "OpenAI": {
            "kind": "openai",
            "model": "gpt-default",
            "base_url": "https://api.openai.com/v1",
            "env": "OPENAI_API_KEY",
        }
    }

    runtime = config.resolve_runtime_profile(
        profile, catalog, environ={"OPENAI_API_KEY": "bakåtkompatibel"}
    )

    assert runtime["api_key"] == "bakåtkompatibel"
    assert runtime["api_key_env"] == "OPENAI_API_KEY"


def test_profile_cache_key_changes_with_selected_profile() -> None:
    profile_a = {"backend_name": "Claude", "model": "claude-a", "base_url": ""}
    profile_b = {"backend_name": "Claude", "model": "claude-b", "base_url": ""}

    assert config.profile_cache_key("A", profile_a) != config.profile_cache_key("B", profile_b)


def test_load_profile_rejects_unknown_explicit_name(tmp_path, monkeypatch) -> None:
    _patch_cfg(tmp_path, monkeypatch)
    config.CONFIG_FILE.write_text(
        json.dumps(
            {
                "profiles": {"Standard": {}, "Snabb": {}},
                "default": "Standard",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Okänd LLM-konfiguration 'Saknas'.*"):
        config.load_profile("Saknas")


def _patch_cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "llm_config.json")


def _stored_profile(tmp_path) -> dict:
    stored = json.loads((tmp_path / "llm_config.json").read_text(encoding="utf-8"))
    return stored["profiles"][stored["default"]]


def test_show_without_file_uses_defaults(tmp_path, monkeypatch, capsys) -> None:
    _patch_cfg(tmp_path, monkeypatch)
    assert llm_config_cli.main([]) == 0
    out = capsys.readouterr().out
    assert "claude" in out
    assert "ingen sparad konfig" in out


def test_set_model_persists(tmp_path, monkeypatch, capsys) -> None:
    _patch_cfg(tmp_path, monkeypatch)
    assert llm_config_cli.main(["--model", "claude-haiku-4-5-20251001"]) == 0
    stored = _stored_profile(tmp_path)
    assert stored["model"] == "claude-haiku-4-5-20251001"
    assert stored["provider"] == "claude"


def test_provider_switch_resets_model_to_default(tmp_path, monkeypatch, capsys) -> None:
    _patch_cfg(tmp_path, monkeypatch)
    llm_config_cli.main(["--model", "claude-opus-4-8"])
    assert llm_config_cli.main(["--provider", "openai"]) == 0
    stored = _stored_profile(tmp_path)
    assert stored["provider"] == "openai"
    assert stored["model"] == "gpt-4o-mini"
    assert stored["backend_name"] == "OpenAI"


def test_provider_switch_with_model_keeps_it(tmp_path, monkeypatch) -> None:
    _patch_cfg(tmp_path, monkeypatch)
    llm_config_cli.main(["--provider", "openai", "--model", "deepseek-chat",
                         "--base-url", "https://api.deepseek.com/v1"])
    stored = _stored_profile(tmp_path)
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


def _reader(answers):
    """Fejkad input() som matar svar i tur och ordning."""
    it = iter(answers)

    def read(_prompt=""):
        return next(it)

    return read


def test_menu_selects_backend_and_model(tmp_path, monkeypatch) -> None:
    _patch_cfg(tmp_path, monkeypatch)
    import backends
    monkeypatch.setattr(backends, "fetch_models", lambda base_url, api_key: [])
    names = list(backends.BACKENDS)
    deepseek_idx = names.index("DeepSeek") + 1
    # Backend = DeepSeek, modell = nr 2 (deepseek-v4-pro ur statisk fallback).
    read = _reader([str(deepseek_idx), "2"])
    assert llm_config_cli.run_menu(read=read, out=lambda *_: None) == 0
    stored = _stored_profile(tmp_path)
    assert stored["backend_name"] == "DeepSeek"
    assert stored["provider"] == "openai"
    assert stored["model"] == "deepseek-v4-pro"
    assert stored["base_url"] == "https://api.deepseek.com/v1"


def test_menu_custom_model_name(tmp_path, monkeypatch) -> None:
    _patch_cfg(tmp_path, monkeypatch)
    import backends
    names = list(backends.BACKENDS)
    claude_idx = names.index("Claude") + 1
    n_models = len(backends.BACKENDS["Claude"]["models"])
    # Claude, sedan "skriv eget namn"-alternativet (sista numret), sedan namnet.
    read = _reader([str(claude_idx), str(n_models + 1), "claude-future-9"])
    assert llm_config_cli.run_menu(read=read, out=lambda *_: None) == 0
    stored = _stored_profile(tmp_path)
    assert stored["backend_name"] == "Claude"
    assert stored["provider"] == "claude"
    assert stored["model"] == "claude-future-9"


def test_menu_default_backend_on_empty_input(tmp_path, monkeypatch) -> None:
    _patch_cfg(tmp_path, monkeypatch)
    import backends
    # Spara först en config så default blir den, tryck Enter (tomt) på båda valen.
    llm_config_cli.main(["--provider", "openai", "--model", "gpt-4o-mini"])
    monkeypatch.setattr(backends, "fetch_models", lambda base_url, api_key: [])
    read = _reader(["", ""])
    assert llm_config_cli.run_menu(read=read, out=lambda *_: None) == 0
    stored = _stored_profile(tmp_path)
    assert stored["backend_name"] == "OpenAI"
    assert stored["model"] == "gpt-4o-mini"


def test_noarg_non_tty_still_prints_config(tmp_path, monkeypatch, capsys) -> None:
    # Pytest-stdin är ingen TTY → no-arg ska skriva ut config, inte starta meny.
    _patch_cfg(tmp_path, monkeypatch)
    assert llm_config_cli.main([]) == 0
    assert "claude" in capsys.readouterr().out


def test_menu_handles_eof_gracefully(tmp_path, monkeypatch) -> None:
    # Ctrl-D (EOFError) vid en prompt ska ge ren avbrytning, inte traceback.
    _patch_cfg(tmp_path, monkeypatch)

    def read(_prompt=""):
        raise EOFError

    assert llm_config_cli.run_menu(read=read, out=lambda *_: None) == 1


def test_run_llm_config_logs_through_context(tmp_path, monkeypatch) -> None:
    # Som admin-jobb ska statusutskrifterna gå via ctx.log (jobbloggen),
    # inte försvinna i processens stdout.
    _patch_cfg(tmp_path, monkeypatch)
    logs: list[str] = []

    class FakeCtx:
        def log(self, message: str, *, level: str = "info") -> None:
            logs.append(message)

    assert llm_config_cli.run_llm_config(
        provider="openai", model=None, base_url=None, reset=False, context=FakeCtx()
    ) == 0
    assert any("Provider bytt" in msg for msg in logs)
    assert any("provider:  openai" in msg for msg in logs)
    assert any("model:     gpt-4o-mini" in msg for msg in logs)
