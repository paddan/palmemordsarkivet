"""LLM-anropet för en sida i ontologi-extraktionen.

``call_page`` skickar en prompt till en upplöst LLM-profil (``config.
resolve_runtime_profile``) — Claude via ``claude_agent_sdk.query`` (en tur, inga
verktyg) eller en OpenAI-kompatibel endpoint via ``chat.completions`` — och
returnerar svarstexten plus tokenantal (``None`` när leverantören inte anger dem).

Felklassning:

* 429, 5xx och nätfel försöks om, upp till ``MAX_FORSOK`` gånger med väntan
  2, 4 och 8 s;
* 401, 402 och 403 — och 429 efter sista försöket — är ``LlmFatal``: nyckeln,
  krediten eller kvoten är slut, så resten av körningen ska stoppas;
* övriga fel (t.ex. 400, eller 5xx efter sista försöket) är ``LlmPageError``:
  sidan misslyckas men körningen fortsätter;
* ett avklippt svar (slutorsak annan än ``stop``/``end_turn`` eller ingen
  uppgift, t.ex. ``length`` eller ``max_tokens``) är också ``LlmPageError`` —
  ett halvt JSON-svar får aldrig sparas som om sidan saknade iakttagelser.

Nyckeln och prompten loggas aldrig.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)

MAX_FORSOK = 4
VANTAN_S = (2, 4, 8)
# 4096 räckte inte: i piloten klipptes var fjärde sida (täta förhörssidor gav
# ~2 400 token i snitt och mycket mer i toppen) och betalades om vid varje körning.
MAX_TOKENS = 16000
_FATALA = {401, 402, 403}
# Slutorsaker som betyder att modellen skrev klart; None = leverantören angav ingen.
_KOMPLETTA = {None, "stop", "end_turn"}


class LlmPageError(Exception):
    """Sidan gick inte att läsa med LLM:en; körningen kan fortsätta."""


class LlmFatal(Exception):
    """Fel som gör att inga fler anrop ska göras (nyckel, kredit, kvot)."""


class _StatusError(Exception):
    """Internt: ett fel med HTTP-status som inte kom som undantag (Claude-vägen)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status


def _status(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    return status if isinstance(status, int) else None


def _ar_natfel(exc: BaseException) -> bool:
    """Nätverks- och tidsgränsfel som är värda ett nytt försök."""
    try:
        from openai import APIConnectionError
    except ImportError:  # pragma: no cover — openai är ett basberoende
        APIConnectionError = ()  # type: ignore[assignment,misc]
    try:
        from claude_agent_sdk import CLIConnectionError, CLINotFoundError
    except ImportError:  # pragma: no cover
        CLIConnectionError = CLINotFoundError = ()  # type: ignore[assignment,misc]
    if isinstance(exc, CLINotFoundError):
        return False
    return isinstance(exc, (APIConnectionError, CLIConnectionError, ConnectionError,
                            asyncio.TimeoutError, TimeoutError))


def _usage(input_tokens: object, output_tokens: object) -> dict[str, int | None]:
    def tal(v: object) -> int | None:
        return v if isinstance(v, int) and not isinstance(v, bool) else None

    return {"input_tokens": tal(input_tokens), "output_tokens": tal(output_tokens)}


def _default_client(cfg: dict) -> Any:
    from openai import AsyncOpenAI

    return AsyncOpenAI(api_key=cfg.get("api_key") or "local",
                       base_url=cfg.get("base_url") or None, max_retries=0)


async def _openai_call(prompt: str, system: str, cfg: dict,
                       client_factory: Callable[[dict], Any]) -> tuple[str, dict]:
    kwargs: dict[str, Any] = {
        "model": cfg["model"],
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": prompt}],
        "max_tokens": MAX_TOKENS,
    }
    # JSON-läget stöds säkert bara av OpenAI själv; övriga OpenAI-kompatibla
    # endpoints (Ollama, egna) kan vägra parametern.
    if str(cfg.get("backend_name") or "").lower() == "openai":
        kwargs["response_format"] = {"type": "json_object"}
    client = client_factory(cfg)
    try:
        response = await client.chat.completions.create(**kwargs)
    finally:
        close = getattr(client, "close", None)
        if close is not None:
            await close()
    choice = response.choices[0]
    finish = getattr(choice, "finish_reason", None)
    if finish not in _KOMPLETTA:
        log.debug("LLM-svaret avslutades med %s (modell %s)", finish, cfg["model"])
        raise LlmPageError(f"LLM-svaret avklippt ({finish})")
    usage = getattr(response, "usage", None)
    return (choice.message.content or "",
            _usage(getattr(usage, "prompt_tokens", None),
                   getattr(usage, "completion_tokens", None)))


async def _claude_call(prompt: str, system: str, cfg: dict) -> tuple[str, dict]:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )

    options = ClaudeAgentOptions(system_prompt=system, model=cfg["model"],
                                 allowed_tools=[], max_turns=1, setting_sources=[])
    parts: list[str] = []
    usage: dict[str, int | None] = _usage(None, None)
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            parts.extend(b.text for b in message.content if isinstance(b, TextBlock))
        elif isinstance(message, ResultMessage):
            u = message.usage or {}
            usage = _usage(u.get("input_tokens"), u.get("output_tokens"))
            if message.is_error:
                status = getattr(message, "api_error_status", None)
                if isinstance(status, int):
                    raise _StatusError(status, f"Claude svarade med HTTP {status}")
                raise LlmPageError(f"Claude-anropet misslyckades ({message.subtype})")
            stop = getattr(message, "stop_reason", None)
            if stop not in _KOMPLETTA:
                log.debug("Claude-svaret avslutades med %s (modell %s)", stop, cfg["model"])
                raise LlmPageError(f"LLM-svaret avklippt ({stop})")
    return "".join(parts), usage


async def call_page(
    prompt: str,
    system: str,
    cfg: dict,
    *,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    client_factory: Callable[[dict], Any] | None = None,
) -> tuple[str, dict]:
    """Ett LLM-anrop med återförsök; returnerar ``(text, {input_tokens, output_tokens})``."""
    vanta = sleep or asyncio.sleep
    fabrik = client_factory or _default_client
    for forsok in range(1, MAX_FORSOK + 1):
        try:
            if cfg.get("kind") == "claude":
                return await _claude_call(prompt, system, cfg)
            return await _openai_call(prompt, system, cfg, fabrik)
        except (LlmPageError, LlmFatal):
            raise
        except Exception as exc:  # noqa: BLE001 — klassas nedan
            status = _status(exc)
            if status in _FATALA:
                raise LlmFatal(f"HTTP {status} från LLM-tjänsten") from exc
            omforsok = status == 429 or (status is not None and status >= 500) \
                or (status is None and _ar_natfel(exc))
            if not omforsok:
                orsak = f"HTTP {status}" if status is not None else type(exc).__name__
                raise LlmPageError(f"LLM-anropet misslyckades: {orsak}") from exc
            if forsok == MAX_FORSOK:
                if status == 429:
                    raise LlmFatal("HTTP 429 efter sista försöket — kvoten är slut") from exc
                orsak = f"HTTP {status}" if status is not None else type(exc).__name__
                raise LlmPageError(
                    f"LLM-anropet misslyckades efter {MAX_FORSOK} försök: {orsak}") from exc
            vantan = VANTAN_S[forsok - 1]
            log.debug("LLM-anrop försök %s misslyckades (%s); väntar %s s", forsok,
                      status if status is not None else type(exc).__name__, vantan)
            await vanta(vantan)
    raise AssertionError("oåtkomligt")  # pragma: no cover
