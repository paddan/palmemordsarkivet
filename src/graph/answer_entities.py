"""LLM-extraktion av nyckelentiteter ur ett RAG-svar — för inline-grafen i Utredning.

Isolerad från Streamlit och Neo4j: ren parser + ett LLM-anrop som återanvänder
anropsmönstret i ``graph.extract_entities``. Felhantering/degradering sköts av
anroparen (Utredning-sidan)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ClaudeAgentOptions,
    TextBlock,
    query,
)

try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None  # type: ignore[assignment,misc]

DEFAULT_CLAUDE_MODEL = "claude-haiku-4-5-20251001"
OPENAI_DEFAULT_MODEL = "gpt-4o-mini"
MAX_ENTITIES = 8
MAX_PARSE_CHARS = 4000  # giltiga entitetslistor är små — skydd mot degenererad LLM-output

_SYSTEM = """\
Du får ett svar om Palmeutredningen. Lista de personer, platser och
organisationer som svaret faktiskt handlar om. Returnera ENBART en giltig
JSON-lista av namnsträngar, max 8 stycken, viktigast först, t.ex.
["Stig Engström", "Dekorima", "Skandia"].
Regler:
- Personnamn på formen "Förnamn Efternamn" när det framgår.
- Ta bara med namn som nämns i svaret — hitta inte på.
- Inga förklaringar, ingen annan text än JSON-listan."""

def parse_entity_list(raw: str) -> list[str]:
    """Plocka JSON-listan av namn ur ett LLM-svar.

    Svaret kan innehålla hakparentestext före listan (t.ex. citatmarkörer som
    "[Nr 12, sida 3]"), så varje '['-position provas med raw_decode tills en
    giltig JSON-lista hittas. Trasig eller saknad JSON ger tom lista.
    Icke-strängar och tomma namn filtreras, dubbletter (skiftlägesokänsligt)
    dedupas, max MAX_ENTITIES.
    Indata trunkeras till MAX_PARSE_CHARS tecken — giltiga listor är små och
    utan tak blir skanningen O(n²) på degenererad output."""
    decoder = json.JSONDecoder()
    data = None
    pos = 0
    text = (raw or "")[:MAX_PARSE_CHARS]
    while True:
        start = text.find("[", pos)
        if start == -1:
            return []
        try:
            candidate, _ = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            pos = start + 1
            continue
        if isinstance(candidate, list):
            data = candidate
            break
        pos = start + 1
    out: list[str] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, str):
            continue
        name = item.strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append(name)
    return out[:MAX_ENTITIES]


def resolve_entity_cfg(saved: dict) -> dict | None:
    """Välj LLM för entitetslistningen.

    Den sparade providern styr: en vald OpenAI-kompatibel backend (inklusive
    DeepSeek) används när endpointens credentials finns. Claude använder Haiku
    för den lilla uppgiften, men bara när Claude faktiskt är vald. Saknas den
    valda providerns credentials returneras None och anroparen degraderar tyst.
    """
    if saved.get("provider") == "openai":
        base_url = saved.get("base_url", "")
        api_key = saved.get("api_key", "") or _api_key_for_openai_base_url(base_url)
        if base_url and _required_env_for_openai_base_url(base_url) and not api_key:
            return None
        if base_url or api_key:
            return {"provider": "openai",
                    "model": saved.get("model") or OPENAI_DEFAULT_MODEL,
                    "base_url": base_url, "api_key": api_key}
    if saved.get("provider") == "claude" and (
        os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        or os.environ.get("ANTHROPIC_API_KEY")
    ):
        return {"provider": "claude", "model": DEFAULT_CLAUDE_MODEL,
                "base_url": "", "api_key": ""}
    return None


def _api_key_for_openai_base_url(base_url: str) -> str:
    env = _required_env_for_openai_base_url(base_url)
    if env:
        return os.environ.get(env, "")
    return os.environ.get("OPENAI_API_KEY", "")


def _required_env_for_openai_base_url(base_url: str) -> str | None:
    host = (urlparse(base_url).hostname or "").lower()
    if "deepseek.com" in host:
        return "DEEPSEEK_API_KEY"
    if "openai.com" in host:
        return "OPENAI_API_KEY"
    return None


async def _claude_call(text: str, model: str) -> str:
    """Skicka svarstexten till Claude, returnera råsvaret."""
    options = ClaudeAgentOptions(
        system_prompt=_SYSTEM,
        model=model,
        allowed_tools=[],
        max_turns=1,
        setting_sources=[],
    )
    parts: list[str] = []
    async for msg in query(prompt=text, options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    parts.append(block.text)
    return "".join(parts)


async def _openai_call(text: str, model: str, base_url: str, api_key: str) -> str:
    """Skicka svarstexten till en OpenAI-kompatibel modell, returnera råsvaret."""
    if AsyncOpenAI is None:
        raise RuntimeError("openai-paketet saknas — kör: pip install openai")
    async with AsyncOpenAI(api_key=api_key or "local", base_url=base_url or None) as client:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": text},
            ],
            max_tokens=512,
        )
    return response.choices[0].message.content or ""


async def extract_answer_entities(answer: str, cfg: dict) -> list[str]:
    """Svar → LLM → lista av entitetsnamn. Exceptions bubblar till anroparen."""
    if cfg["provider"] == "claude":
        raw = await _claude_call(answer, cfg["model"])
    else:
        raw = await _openai_call(answer, cfg["model"], cfg["base_url"],
                                 cfg["api_key"])
    return parse_entity_list(raw)
