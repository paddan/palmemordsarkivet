from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from llm_correct import _correct_all, _openai, _resolve_api_key


def test_claude_reads_anthropic_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    assert _resolve_api_key("claude", base_url="", explicit_key="") == "test-key"


def test_claude_reads_oauth_token(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-tok")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert _resolve_api_key("claude", base_url="", explicit_key="") == "oauth-tok"


def test_claude_missing_key_raises(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        _resolve_api_key("claude", base_url="", explicit_key="")


def test_openai_reads_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert _resolve_api_key("openai", base_url="", explicit_key="") == "sk-test"


def test_openai_missing_key_raises(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        _resolve_api_key("openai", base_url="", explicit_key="")


def test_openai_custom_base_url_no_validation(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # Inget fel ens utan nyckel när base_url är satt
    result = _resolve_api_key("openai", base_url="http://localhost:11434/v1", explicit_key="")
    assert result == ""


def test_explicit_key_overrides_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    result = _resolve_api_key("openai", base_url="", explicit_key="my-key")
    assert result == "my-key"


def test_deepseek_base_url_reads_deepseek_env(monkeypatch):
    # DeepSeek har custom base_url MEN kräver nyckel — ska läsas ur DEEPSEEK_API_KEY.
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    result = _resolve_api_key("openai", base_url="https://api.deepseek.com/v1", explicit_key="")
    assert result == "ds-key"


def test_localhost_base_url_stays_keyless_even_with_deepseek_env(monkeypatch):
    # Lokal server (Ollama) ska fortsatt köra utan nyckel, även om DEEPSEEK_API_KEY råkar vara satt.
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    result = _resolve_api_key("openai", base_url="http://localhost:11434/v1", explicit_key="")
    assert result == ""


def test_deepseek_base_url_missing_key_raises(monkeypatch):
    # Känd fjärr-provider utan sin env-nyckel ska avbryta med vägledning, inte tyst tom sträng.
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        _resolve_api_key("openai", base_url="https://api.deepseek.com/v1", explicit_key="")


def test_openai_returns_corrected_text():
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = "rättad text"

    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

    with patch("llm_correct.AsyncOpenAI", return_value=mock_client):
        result = asyncio.run(_openai(
            text="OCR-skräp text",
            model="gpt-4o-mini",
            base_url="",
            api_key="sk-test",
        ))
    assert result == "rättad text"


def test_openai_fallback_on_empty_response():
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = ""

    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

    with patch("llm_correct.AsyncOpenAI", return_value=mock_client):
        result = asyncio.run(_openai(
            text="original text",
            model="gpt-4o-mini",
            base_url="",
            api_key="sk-test",
        ))
    assert result == "original text"


def test_openai_uses_local_as_fallback_api_key():
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = "svar"

    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

    captured_kwargs = {}

    def capture_client(**kwargs):
        captured_kwargs.update(kwargs)
        return mock_client

    with patch("llm_correct.AsyncOpenAI", side_effect=capture_client):
        asyncio.run(_openai(
            text="text",
            model="llama3.1:8b",
            base_url="http://localhost:11434/v1",
            api_key="",
        ))
    assert captured_kwargs["api_key"] == "local"
    assert captured_kwargs["base_url"] == "http://localhost:11434/v1"


def test_correct_all_dispatches_to_claude(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    txt_dir = tmp_path / "text"
    txt_dir.mkdir()

    stem = "testdok"
    (txt_dir / f"{stem}.txt").write_text("sida ett\fsida två", encoding="utf-8")

    provider_cfg = {
        "provider": "claude",
        "model": "claude-haiku-4-5-20251001",
        "base_url": "",
        "api_key": "",
    }

    with patch("llm_correct._claude", new=AsyncMock(return_value="rättad")) as mock_claude, \
         patch("merge_pages.merge_one"):
        asyncio.run(_correct_all(
            bad={"testdok.txt": [1]},
            txt_dir=txt_dir,
            provider_cfg=provider_cfg,
            dry_run=False,
        ))
    mock_claude.assert_called_once()


def test_correct_all_dispatches_to_openai(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    txt_dir = tmp_path / "text"
    txt_dir.mkdir()

    stem = "testdok"
    (txt_dir / f"{stem}.txt").write_text("sida ett\fsida två", encoding="utf-8")

    provider_cfg = {
        "provider": "openai",
        "model": "gpt-4o-mini",
        "base_url": "",
        "api_key": "sk-test",
    }

    with patch("llm_correct._openai", new=AsyncMock(return_value="rättad")) as mock_openai, \
         patch("merge_pages.merge_one"):
        asyncio.run(_correct_all(
            bad={"testdok.txt": [1]},
            txt_dir=txt_dir,
            provider_cfg=provider_cfg,
            dry_run=False,
        ))
    mock_openai.assert_called_once()
