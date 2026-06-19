"""Delad LLM-backend-katalog — en sanning för Utredning-sidan och llm_config_cli.

Innehåller listan över valbara backends (Claude/OpenAI/DeepSeek/Ollama/custom),
modell-filtret och hjälpfunktioner för att hämta tillgängliga modeller från en
OpenAI-kompatibel /v1/models-endpoint. Konsumeras av:

* ``Utredning.py`` — sidofältets backend-/modellval (cachar fetch via st.cache_data)
* ``llm_config_cli.py`` — den interaktiva menyn (samma val utan webgränssnitt)
"""
from __future__ import annotations

# Backend-katalog. ``kind`` matchar provider-nyckeln i generated/llm_config.json
# (claude/openai). ``models`` är den statiska fallback-listan; ``base_url``/``env``
# styr om live-modeller kan hämtas; ``configurable`` markerar backends där
# endpoint-URL och modellnamn matas in fritt.
BACKENDS: dict[str, dict] = {
    "Claude": {
        "kind": "claude",
        "model": "claude-opus-4-8",
        "models": [
            "claude-opus-4-8",
            "claude-opus-4-7",
            "claude-sonnet-4-6",
            "claude-haiku-4-5-20251001",
        ],
    },
    "OpenAI": {
        "kind": "openai",
        "model": "gpt-4o",
        "models": ["gpt-5", "gpt-5-mini", "gpt-4.1", "gpt-4.1-mini", "gpt-4o", "gpt-4o-mini", "o3", "o3-pro", "o4-mini"],
        "base_url": "https://api.openai.com/v1",
        "env": "OPENAI_API_KEY",
    },
    "DeepSeek": {
        "kind": "openai",
        "model": "deepseek-chat",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "base_url": "https://api.deepseek.com/v1",
        "env": "DEEPSEEK_API_KEY",
    },
    "Ollama (lokal)": {
        "kind": "openai",
        "model": "gemma3:12b",
        "models": ["gemma3:12b"],
        "base_url": "http://localhost:11434/v1",
        "env": None,
        "configurable": True,
    },
    "OpenAI-kompatibel (custom)": {
        "kind": "openai",
        "model": "llama3.1:8b",
        "models": ["llama3.1:8b"],
        "base_url": "http://localhost:1234/v1",
        "env": None,
        "configurable": True,
    },
}

# Modellnamn som filtreras bort ur en /v1/models-lista (embedding/ljud/bild m.m.).
MODEL_SKIP_SUBSTRINGS = {
    "embedding", "tts", "whisper", "dall", "instruct",
    "realtime", "audio", "transcription", "moderation",
    "babbage", "davinci", "search",
}


def fetch_models(base_url: str, api_key: str) -> list[str]:
    """Hämta tillgängliga modeller från en OpenAI-kompatibel /v1/models-endpoint.

    Returnerar en sorterad lista med modell-id, eller [] vid fel/saknad endpoint.
    Använder httpx om det finns, annars stdlib urllib.
    """
    import json as _json

    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        import httpx
        resp = httpx.get(url, headers=headers, timeout=5.0)
        resp.raise_for_status()
        data = resp.json()
    except ImportError:
        import urllib.request
        req = urllib.request.Request(url)
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                data = _json.loads(r.read())
        except Exception:
            return []
    except Exception:
        return []
    return sorted(m["id"] for m in data.get("data", []))


def available_models(backend: dict, api_key: str = "", fetcher=None) -> list[str]:
    """Returnera valbara modeller för en backend.

    Har backenden en /v1/models-endpoint (``base_url`` satt) hämtas live-listan
    via ``fetcher`` (default fetch_models), brusmodeller filtreras bort med
    MODEL_SKIP_SUBSTRINGS, och vid tomt/fel-resultat faller vi tillbaka på den
    statiska ``models``-listan. Backends utan ``base_url`` (t.ex. Claude)
    returnerar alltid sin statiska lista. ``fetcher`` låter Utredning-sidan skicka in sin
    st.cache_data-cachade variant så /v1/models inte slås upp på varje rerun.
    """
    static = list(backend.get("models", []))
    if not backend.get("base_url"):
        return static
    fetched = (fetcher or fetch_models)(backend["base_url"], api_key)
    fetched = [
        m for m in fetched
        if not any(s in m.lower() for s in MODEL_SKIP_SUBSTRINGS)
    ]
    return fetched or static
