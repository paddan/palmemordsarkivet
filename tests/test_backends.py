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


def test_deepseek_static_fallback_uses_current_provider_model_ids() -> None:
    """Regression: reservlistan ska inte erbjuda DeepSeeks utfasade alias."""
    assert backends.BACKENDS["DeepSeek"]["model"] == "deepseek-v4-flash"
    assert backends.BACKENDS["DeepSeek"]["models"] == [
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        "deepseek-v4-flash-vision-exp",
    ]


def test_available_models_uses_fetched_and_filters_skip(monkeypatch) -> None:
    fetched = ["gpt-4o", "text-embedding-3-small", "whisper-1", "gpt-4o-mini"]
    monkeypatch.setattr(backends, "fetch_models", lambda base_url, api_key: fetched)
    models = backends.available_models(backends.BACKENDS["OpenAI"], api_key="k")
    assert "gpt-4o" in models
    assert "gpt-4o-mini" in models
    assert "text-embedding-3-small" not in models
    assert "whisper-1" not in models


def test_openrouter_is_an_openai_compatible_backend() -> None:
    """OpenRouter nås som OpenAI-protokollet; listan hämtas live från /v1/models."""
    orouter = backends.BACKENDS["OpenRouter"]
    assert orouter["kind"] == "openai"
    assert orouter["base_url"] == "https://openrouter.ai/api/v1"
    assert orouter["env"] == "OPENROUTER_API_KEY"
    assert orouter["model"] in orouter["models"]


def test_batch_variants_are_filtered_from_fetched_models(monkeypatch) -> None:
    """Regression: OpenRouters ``:batch``-slugar kräver /api/beta/batches och
    svarar "no endpoints found" mot chat completions."""
    fetched = ["openai/gpt-4o", "anthropic/claude-opus-5:batch", "deepseek/deepseek-chat"]
    monkeypatch.setattr(backends, "fetch_models", lambda base_url, api_key: fetched)
    models = backends.available_models(backends.BACKENDS["OpenRouter"], api_key="k")
    assert "anthropic/claude-opus-5:batch" not in models
    assert "openai/gpt-4o" in models


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


def test_fetch_model_pricing_parses_openrouter_style_response(monkeypatch) -> None:
    """OpenRouter anger USD per token; priskatalogen ska räkna om till USD per 1M."""
    monkeypatch.setattr(
        backends, "_get_json",
        lambda url, headers: {
            "data": [
                {
                    "id": "openai/gpt-4o",
                    "pricing": {
                        "prompt": "0.0000025",
                        "completion": "0.0000075",
                        "input_cache_read": "0.00000025",
                    },
                },
            ],
        },
    )
    pricing = backends.fetch_model_pricing("https://openrouter.ai/api/v1", "k")
    assert pricing == {
        "openai/gpt-4o": {
            "input_price_usd": 2.5,
            "output_price_usd": 7.5,
            "cache_hit_price_usd": 0.25,
        },
    }


def test_fetch_model_pricing_avoids_float_multiplication_noise(monkeypatch) -> None:
    """Regression: float(value) * 1_000_000 gav 0.19999999999999998 i stället
    för 0.2 för prompt-priset "0.0000002" — binär flyttalsmultiplikation av en
    decimal sträng. Omräkningen måste ske med Decimal så priset blir exakt."""
    monkeypatch.setattr(
        backends, "_get_json",
        lambda url, headers: {
            "data": [{"id": "openai/gpt-4o", "pricing": {"prompt": "0.0000002"}}],
        },
    )
    pricing = backends.fetch_model_pricing("https://openrouter.ai/api/v1", "k")
    assert pricing["openai/gpt-4o"]["input_price_usd"] == 0.2
    assert repr(pricing["openai/gpt-4o"]["input_price_usd"]) == "0.2"


def test_fetch_model_pricing_ignores_models_without_pricing(monkeypatch) -> None:
    """OpenAI/DeepSeeks /v1/models saknar ett pricing-fält helt — ska ge tom katalog."""
    monkeypatch.setattr(
        backends, "_get_json",
        lambda url, headers: {"data": [{"id": "gpt-4o"}, {"id": "gpt-4o-mini", "pricing": None}]},
    )
    assert backends.fetch_model_pricing("https://api.openai.com/v1", "k") == {}


def test_fetch_model_pricing_returns_empty_dict_on_fetch_failure(monkeypatch) -> None:
    monkeypatch.setattr(backends, "_get_json", lambda url, headers: None)
    assert backends.fetch_model_pricing("https://openrouter.ai/api/v1", "k") == {}
