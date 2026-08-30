"""Tester för den delade backend-katalogen (src/backends.py)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import backends  # noqa: E402 — kräver src på sys.path ovan


def test_catalog_has_expected_backends() -> None:
    assert "Claude" in backends.BACKENDS
    assert backends.BACKENDS["Claude"]["kind"] == "claude"
    assert backends.BACKENDS["DeepSeek"]["kind"] == "openai"
    # Varje backend måste ha minst kind + model.
    for name, spec in backends.BACKENDS.items():
        assert spec.get("kind"), name
        assert spec.get("model"), name


def test_available_models_falls_back_to_static_when_fetch_empty(monkeypatch) -> None:
    monkeypatch.setattr(backends, "fetch_models", lambda base_url, api_key: [])
    models = backends.available_models(backends.BACKENDS["DeepSeek"], api_key="")
    assert models == backends.BACKENDS["DeepSeek"]["models"]


def test_available_models_uses_fetched_and_filters_skip(monkeypatch) -> None:
    fetched = ["gpt-4o", "text-embedding-3-small", "whisper-1", "gpt-4o-mini"]
    monkeypatch.setattr(backends, "fetch_models", lambda base_url, api_key: fetched)
    models = backends.available_models(backends.BACKENDS["OpenAI"], api_key="k")
    assert "gpt-4o" in models
    assert "gpt-4o-mini" in models
    assert "text-embedding-3-small" not in models
    assert "whisper-1" not in models


def test_available_models_uses_injected_fetcher(monkeypatch) -> None:
    # Webui injicerar sin cachade fetcher; den ska användas istället för modulens.
    monkeypatch.setattr(backends, "fetch_models", lambda base_url, api_key: ["wrong"])
    models = backends.available_models(
        backends.BACKENDS["OpenAI"], api_key="k",
        fetcher=lambda base_url, api_key: ["gpt-4o"],
    )
    assert models == ["gpt-4o"]


def test_available_models_claude_uses_static_list_without_fetch(monkeypatch) -> None:
    # Claude saknar base_url/env → ingen /v1/models-fetch, statisk lista används.
    monkeypatch.setattr(
        backends, "fetch_models",
        lambda base_url, api_key: (_ for _ in ()).throw(AssertionError("should not fetch")),
    )
    models = backends.available_models(backends.BACKENDS["Claude"], api_key="")
    assert models == backends.BACKENDS["Claude"]["models"]
