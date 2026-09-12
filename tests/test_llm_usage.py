"""Tester för token- och kostnadsräkningen (src/llm_usage.py)."""
from __future__ import annotations

import pytest

import llm_usage


class _Usage:
    """Objektformad usage (som OpenAI-SDK:ns CompletionUsage)."""

    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


class _Cached:
    def __init__(self, cached_tokens: int) -> None:
        self.cached_tokens = cached_tokens


def test_openai_usage_reads_prompt_and_completion_tokens():
    usage = llm_usage.usage_from_openai(
        _Usage(prompt_tokens=120, completion_tokens=8)
    )

    assert usage == {"input": 120, "output": 8, "cache_hit": 0}


def test_openai_usage_prefers_deepseek_cache_split():
    usage = llm_usage.usage_from_openai(
        _Usage(prompt_tokens=100, completion_tokens=5, prompt_cache_hit_tokens=64)
    )

    assert usage == {"input": 100, "output": 5, "cache_hit": 64}


def test_openai_usage_reads_nested_cached_tokens():
    usage = llm_usage.usage_from_openai(
        _Usage(prompt_tokens=50, completion_tokens=2, prompt_tokens_details=_Cached(32))
    )

    assert usage == {"input": 50, "output": 2, "cache_hit": 32}


def test_openai_usage_returns_none_without_tokens():
    assert llm_usage.usage_from_openai(None) is None
    assert llm_usage.usage_from_openai(_Usage(prompt_tokens=0, completion_tokens=0)) is None


def test_claude_usage_counts_cache_tokens_as_input():
    # Anthropic rapporterar cache-läsningar utanför input_tokens; de måste med i
    # indata-summan, annars underskattas både token och kostnad.
    usage = llm_usage.usage_from_claude(
        {
            "input_tokens": 10,
            "output_tokens": 4,
            "cache_creation_input_tokens": 900,
            "cache_read_input_tokens": 5000,
        }
    )

    assert usage == {"input": 5910, "output": 4, "cache_hit": 5000}


def test_claude_usage_tolerates_missing_and_unknown_keys():
    assert llm_usage.usage_from_claude({"input_tokens": 3}) == {
        "input": 3,
        "output": 0,
        "cache_hit": 0,
    }


def test_cost_uses_cache_hit_price_for_cached_input():
    usage = {"input": 1_000_000, "output": 1_000_000, "cache_hit": 800_000}
    prices = {"input": 0.435, "output": 0.87, "cache_hit": 0.003625}

    # 200 000 missar à 0,435 + 800 000 träffar à 0,003625 + 1M ut à 0,87
    assert llm_usage.cost_usd(usage, prices) == pytest.approx(
        0.087 + 0.0029 + 0.87
    )


def test_cost_falls_back_to_input_price_without_cache_price():
    usage = {"input": 1_000_000, "output": 0, "cache_hit": 400_000}

    assert llm_usage.cost_usd(usage, {"input": 0.14, "output": 0.28}) == pytest.approx(
        0.14
    )


def test_cost_is_none_without_prices():
    usage = {"input": 10, "output": 2, "cache_hit": 0}

    assert llm_usage.cost_usd(usage, {}) is None


def test_prices_from_profile_reads_positive_usd_prices():
    profile = {
        "input_price_usd": 0.14,
        "output_price_usd": "0.28",
        "cache_hit_price_usd": 0.0028,
        "model": "deepseek-v4-flash",
    }

    assert llm_usage.prices_from_profile(profile) == {
        "input": 0.14,
        "output": 0.28,
        "cache_hit": 0.0028,
    }


def test_prices_from_profile_treats_unset_and_broken_values_as_missing():
    assert llm_usage.prices_from_profile({}) == {}
    assert llm_usage.prices_from_profile({"input_price_usd": 0, "output_price_usd": 0}) == {}
    assert llm_usage.prices_from_profile({"input_price_usd": "abc"}) == {}


def test_add_usage_accumulates_tokens_calls_and_cost():
    totals: dict = {}

    llm_usage.add_usage(totals, {"input": 100, "output": 10, "cache_hit": 40}, 0.002)
    llm_usage.add_usage(totals, {"input": 50, "output": 5, "cache_hit": 0}, None)

    assert totals["calls"] == 2
    assert totals["input"] == 150
    assert totals["output"] == 15
    assert totals["cache_hit"] == 40
    assert totals["cost"] == 0.002
    assert totals["cost_partial"] is True


def test_add_usage_counts_a_call_that_reported_nothing():
    """En endpoint som inte skickar usage ska ändå räknas som ett anrop."""
    totals: dict = {}

    llm_usage.add_usage(totals, None, None)

    assert totals["calls"] == 1
    assert totals["cost_partial"] is True
    assert llm_usage.format_summary(totals) == "1 anrop · in 0 · ut 0 · kostnad okänd"


def test_format_summary_swedish_numbers_and_unknown_cost():
    totals = {
        "calls": 12,
        "input": 34_512,
        "output": 8_210,
        "cache_hit": 30_000,
        "cost": 0.0021,
        "cost_partial": False,
    }

    line = llm_usage.format_summary(totals)

    assert "12 anrop" in line
    assert "in 34 512" in line
    assert "ut 8 210" in line
    assert "≈ $0.0021" in line

    utan_pris = {**totals, "cost": 0.0, "cost_partial": True}
    assert "kostnad okänd" in llm_usage.format_summary(utan_pris)


def test_format_summary_handles_empty_totals():
    line = llm_usage.format_summary({})

    assert "0 anrop" in line
    assert "$" not in line
