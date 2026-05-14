from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from llm_correct import _resolve_api_key


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
