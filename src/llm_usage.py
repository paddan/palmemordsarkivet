"""Token- och kostnadsräkning för LLM-anrop (Streamlit-fri).

Anropas från ``Utredning.py`` (sidofältets räknare) och ``admin_ui.py``
(ackumulerade siffror per profil). Modulen innehåller bara ren logik — ingen
Streamlit, ingen databas — så att räkningen kan testas direkt.

Principer:

* **Token kommer alltid från leverantörens svar** (OpenAI: ``prompt_tokens``/
  ``completion_tokens``, Claude: ``usage`` på ``ResultMessage``).
* **Kostnaden används som leverantören rapporterar den när den finns** (Claude
  Agent SDK:s ``total_cost_usd``). För OpenAI-kompatibla backends finns ingen
  sådan siffra; då räknas kostnaden ur priserna som användaren lagt på profilen
  (USD per 1M token). Saknas priserna visas "kostnad okänd" i stället för en
  gissning.
* Cachade indata-token prissätts med ``cache_hit``-priset när det är satt —
  DeepSeeks cache är automatisk och ~50x billigare, så utan det skulle
  kostnaden överskattas kraftigt.
"""
from __future__ import annotations

import math
from collections.abc import Mapping

# Nycklar i en LLM-profil (generated/llm_config.json), USD per 1M token.
PRICE_KEYS: dict[str, str] = {
    "input": "input_price_usd",
    "output": "output_price_usd",
    "cache_hit": "cache_hit_price_usd",
}

# Samma nycklar som en tupel, för konsumenter som bara ska bevara/rensa dem.
PROFILE_PRICE_KEYS: tuple[str, ...] = tuple(PRICE_KEYS.values())

_TOKENS_PER_UNIT = 1_000_000


def _value(obj: object, key: str) -> object:
    """Läs ett fält från både dict-formad och objektformad usage."""
    if isinstance(obj, Mapping):
        return obj.get(key)
    return getattr(obj, key, None)


def _int(value: object) -> int:
    """Tolerant heltalsläsning: saknade/okända värden blir 0."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _number(value: object) -> float | None:
    """Tolerant flyttalsläsning: otolkbara värden blir None."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _tokens(obj: object, key: str) -> int:
    """Antal token ur ett fält, 0 när fältet saknas."""
    return _int(_value(obj, key))


def usage_from_openai(usage: object) -> dict | None:
    """Token ur OpenAI-kompatibel ``usage``, eller None när inget rapporterats.

    ``cache_hit`` läses från DeepSeeks ``prompt_cache_hit_tokens`` eller ur
    ``prompt_tokens_details.cached_tokens`` (OpenAI/Ollama). Båda ligger inom
    ``prompt_tokens``.
    """
    if usage is None:
        return None
    input_tokens = _tokens(usage, "prompt_tokens")
    output_tokens = _tokens(usage, "completion_tokens")
    if not input_tokens and not output_tokens:
        return None
    cache_hit = _tokens(usage, "prompt_cache_hit_tokens")
    if not cache_hit:
        details = _value(usage, "prompt_tokens_details")
        cache_hit = _tokens(details, "cached_tokens") if details is not None else 0
    return {"input": input_tokens, "output": output_tokens, "cache_hit": cache_hit}


def usage_from_claude(usage: object) -> dict:
    """Token ur Claude Agent SDK:s ``ResultMessage.usage``.

    Anthropic rapporterar cache-skrivningar och cache-läsningar utanför
    ``input_tokens``; de räknas in i indata-summan (annars underskattas både
    token och kostnad), medan läsningarna hålls isär som ``cache_hit``.
    """
    cache_read = _tokens(usage, "cache_read_input_tokens")
    return {
        "input": _tokens(usage, "input_tokens")
        + _tokens(usage, "cache_creation_input_tokens")
        + cache_read,
        "output": _tokens(usage, "output_tokens"),
        "cache_hit": cache_read,
    }


def prices_from_profile(profile: Mapping[str, object]) -> dict:
    """Plocka ut satta priser (USD per 1M token) ur en LLM-profil.

    Saknade, nollade eller otolkbara värden utelämnas: en profil utan priser
    ska ge "kostnad okänd", inte en nollkostnad som ser ut som ett svar.
    """
    prices: dict[str, float] = {}
    for kind, key in PRICE_KEYS.items():
        number = _number(profile.get(key))
        if number is not None and number > 0:
            prices[kind] = number
    return prices


def cost_usd(usage: Mapping[str, int], prices: Mapping[str, float]) -> float | None:
    """Beräkna kostnad i USD, eller None när priser saknas."""
    if not prices:
        return None
    input_price = float(prices.get("input", 0.0))
    output_price = float(prices.get("output", 0.0))
    cache_price = float(prices.get("cache_hit", input_price))
    input_tokens = int(usage.get("input", 0))
    cache_hit = min(int(usage.get("cache_hit", 0)), input_tokens)
    miss_tokens = input_tokens - cache_hit
    return (
        miss_tokens * input_price
        + cache_hit * cache_price
        + int(usage.get("output", 0)) * output_price
    ) / _TOKENS_PER_UNIT


def add_usage(
    totals: dict, usage: Mapping[str, int] | None, cost: float | None
) -> dict:
    """Lägg ett anrops token och kostnad till en summeringsdict (muteras)."""
    totals["calls"] = _int(totals.get("calls")) + 1
    if usage:
        for key in ("input", "output", "cache_hit"):
            totals[key] = _int(totals.get(key)) + _int(usage.get(key))
    if cost is None:
        # Ett anrop utan pris gör summan till en undre gräns; markera det så
        # gränssnittet kan säga "ofullständig" i stället för att ljuga.
        totals["cost_partial"] = True
    else:
        totals["cost"] = _number(totals.get("cost")) or 0.0
        totals["cost"] += float(cost)
    totals.setdefault("cost", 0.0)
    totals.setdefault("cost_partial", False)
    return totals


def totals_from_row(row: Mapping[str, object]) -> dict:
    """Gör en ``llm_usage``-rad ur state.db till samma form som sessionssumman."""
    return {
        "calls": _int(row.get("calls")),
        "input": _int(row.get("input_tokens")),
        "output": _int(row.get("output_tokens")),
        "cache_hit": _int(row.get("cache_hit_tokens")),
        "cost": _number(row.get("cost_usd")) or 0.0,
        "cost_partial": bool(row.get("cost_partial")),
    }


def format_count(count: int) -> str:
    """Kompakt antal: ``512``, ``335k``, ``1.5m``.

    Tusental avrundas uppåt vid halva steget (2 500 → ``3k``; ``round()`` är
    banker's rounding och skulle ge ``2k``). Når det avrundade tusentalet 1 000
    byter vi till miljoner, så 999 500 blir ``1.0m`` i stället för ``1000k``.
    Miljoner visas med en decimal under tio miljoner — sidofältet är smalt och
    exakta token är ointressanta där."""
    value = _int(count)
    if abs(value) < 1_000:
        return str(value)
    tusental = math.floor(value / 1_000 + 0.5)
    if abs(tusental) < 1_000:
        return f"{tusental}k"
    miljoner = value / 1_000_000
    if abs(miljoner) < 10:
        return f"{miljoner:.1f}m"
    return f"{math.floor(miljoner + 0.5)}m"


def format_cost(cost: float) -> str:
    """Kostnad i USD — fyra decimaler under en dollar, annars två."""
    return f"${cost:.4f}" if abs(cost) < 1 else f"${cost:.2f}"


def format_summary(totals: Mapping[str, object]) -> str:
    """Kompakt rad: `12 anrop · ↑34k ↓8k · ≈ $0.0021`."""
    calls = _int(totals.get("calls"))
    if not calls:
        return "0 anrop"
    parts = [
        f"{calls} anrop",
        # Pilarna hör ihop som en enhet (↑indata ↓utdata).
        f"↑{format_count(_int(totals.get('input')))} "
        f"↓{format_count(_int(totals.get('output')))}",
    ]
    cost = _number(totals.get("cost")) or 0.0
    if not cost and not totals.get("cost_partial"):
        return " · ".join(parts)
    if not cost:
        parts.append("kostnad okänd")
    else:
        parts.append(
            f"≈ {format_cost(cost)}"
            + (" (ofullständig — anrop utan pris)" if totals.get("cost_partial") else "")
        )
    return " · ".join(parts)
