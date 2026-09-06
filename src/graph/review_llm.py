"""Källbundna LLM-förslag för flaggade objekt i grafgranskningen."""
from __future__ import annotations

import asyncio
import json
import re
from collections import defaultdict
from contextlib import closing

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

import config as _llm_config
import db as state_db
from backends import BACKENDS
from graph.review import audit_entries, validate_decision, validate_suggestion_evidence
from operations.context import ensure_terminal_context
from operations.exceptions import OperationFailed

try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None  # type: ignore[assignment,misc]


_SYSTEM = """\
Du granskar maskinellt extraherade entiteter och relationer mot en svensk
polis- eller utredningssida. Du lämnar endast förslag; du fattar aldrig ett
aktivt granskningsbeslut.

Returnera ENBART giltig JSON på formen:
{"suggestions":[{"item_key":"...","action":"keep|exclude|replace",
"target":{},"evidence":"kort ordagrant utdrag","motivation":"..."}]}

Regler:
- Granska endast angivna item_key och lämna högst ett förslag per nyckel.
- Gissa aldrig en persons identitet och utveckla aldrig initialer med hjälp av
  allmänkunskap eller andra dokument.
- evidence måste vara ett kort utdrag som faktiskt förekommer på den bifogade
  sidan. Hänvisa inte bara till ett sidnummer.
- replace kräver target {"namn":"...","typ":"person|plats|organisation"}
  för en entitet, eller {"fran":"...","typ":"...","till":"..."} för en
  relation.
- Om sidan saknar belägg för en ändring: föreslå keep, citera det relevanta
  osäkra stället och förklara osäkerheten i motivation. Gissa inte.
"""


def _json_object(raw: str) -> dict | None:
    match = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not match:
        return None
    try:
        value = json.loads(match.group(0))
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def parse_suggestions(raw: str, items: list[dict], source_text: str) -> list[dict]:
    """Validera LLM-förslag mot sidans objekt och faktiska källtext.

    Ogiltiga förslag hoppas över. Resultatet innehåller endast data som kan
    sparas i den separata förslagstabellen; funktionen skapar aldrig beslut.
    """
    data = _json_object(raw)
    suggestions = data.get("suggestions") if data else None
    if not isinstance(suggestions, list):
        return []

    by_key = {item.get("item_key"): item for item in items}
    result: list[dict] = []
    seen: set[str] = set()
    for proposal in suggestions:
        if not isinstance(proposal, dict):
            continue
        item_key = proposal.get("item_key")
        item = by_key.get(item_key)
        if item is None or not isinstance(item_key, str) or item_key in seen:
            continue
        action = proposal.get("action")
        target = proposal.get("target", {})
        evidence = proposal.get("evidence")
        motivation = proposal.get("motivation")
        if not isinstance(action, str) or not action.strip():
            continue
        if not isinstance(evidence, str) or not evidence.strip():
            continue
        if not isinstance(motivation, str) or not motivation.strip():
            continue
        if action != "replace":
            target = {}
        try:
            validate_decision(item, action, target, motivation)
            validate_suggestion_evidence(item, action, target, evidence, source_text)
        except (TypeError, ValueError):
            continue
        result.append({
            "item_key": item_key,
            "source_hash": item["source_hash"],
            "action": action,
            "target": target,
            "evidence": evidence.strip(),
            "note": motivation.strip(),
        })
        seen.add(item_key)
    return result


def _prompt(items: list[dict], source_text: str) -> str:
    review_input = [
        {
            "item_key": item["item_key"],
            "kind": item["kind"],
            "original": item["original"],
            "issues": item["issues"],
        }
        for item in items
    ]
    return (
        "Granska följande flaggade objekt. Gissa aldrig någons identitet.\n\n"
        "OBJEKT:\n"
        + json.dumps(review_input, ensure_ascii=False, indent=2)
        + "\n\nHELA SIDANS KÄLLTEXT:\n"
        + source_text
    )


async def _claude_call(prompt: str, cfg: dict) -> str:
    options = ClaudeAgentOptions(
        system_prompt=_SYSTEM,
        model=cfg["model"],
        allowed_tools=[],
        max_turns=1,
        setting_sources=[],
    )
    parts: list[str] = []
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    parts.append(block.text)
    return "".join(parts)


async def _openai_call(prompt: str, cfg: dict) -> str:
    if AsyncOpenAI is None:
        raise RuntimeError("openai-paketet saknas — kör: pip install openai")
    async with AsyncOpenAI(
        api_key=cfg.get("api_key") or "local",
        base_url=cfg.get("base_url") or None,
    ) as client:
        response = await client.chat.completions.create(
            model=cfg["model"],
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": prompt},
            ],
            max_tokens=4096,
        )
    return response.choices[0].message.content or ""


async def _call_llm(prompt: str, cfg: dict) -> str:
    if cfg["kind"] == "claude":
        return await _claude_call(prompt, cfg)
    return await _openai_call(prompt, cfg)


async def review_page(items: list[dict], source_text: str, cfg: dict) -> list[dict]:
    """Granska en sida i ett enda LLM-anrop och returnera rena förslag."""
    if not items or not source_text.strip():
        return []
    raw = await _call_llm(_prompt(items, source_text), cfg)
    return parse_suggestions(raw, items, source_text)


def run_llm_review(*, profile: str = "", limit: int = 0, context=None) -> int:
    """Granska flaggade objekt sidvis och spara förslag, aldrig beslut."""
    if limit < 0:
        raise OperationFailed("LLM-granskningens limit får inte vara negativ.")
    try:
        if profile:
            profile_name = profile
            saved = _llm_config.load_profile(profile)
        else:
            profiles = _llm_config.load_all()
            profile_name = profiles["default"]
            saved = profiles["profiles"][profile_name]
    except (KeyError, ValueError) as exc:
        raise OperationFailed(str(exc)) from exc
    cfg = _llm_config.resolve_runtime_profile(saved, BACKENDS)
    if cfg.get("kind") not in {"claude", "openai"}:
        raise OperationFailed(f"LLM-backenden {cfg.get('kind')!r} stöds inte.")
    if cfg["kind"] == "openai" and cfg.get("api_key_env") and not cfg.get("api_key"):
        raise OperationFailed(
            f"Miljövariabeln {cfg['api_key_env']} saknas för LLM-profilen."
        )

    ctx = context or ensure_terminal_context(None)
    with closing(state_db.connect()) as conn:
        state_db.init_schema(conn)
        entries, decisions = state_db.read_graph_review_snapshot(conn)
        flagged = [
            item for item in audit_entries(entries, decisions)
            if item["issues"] and (not item["decision"] or item["stale"])
        ]
        pages: dict[tuple[str, int], list[dict]] = defaultdict(list)
        for item in flagged:
            pages[(item["pdf_stem"], item["page_num"])].append(item)
        selected = list(pages.items())[:limit] if limit > 0 else list(pages.items())
        total = len(selected)
        saved_count = 0
        for index, ((stem, page_num), items) in enumerate(selected, start=1):
            ctx.check_cancelled()
            source_text = state_db.get_graph_review_page(conn, stem, page_num)
            suggestions = asyncio.run(review_page(items, source_text, cfg))
            if suggestions:
                state_db.save_graph_review_suggestions(
                    conn,
                    suggestions=suggestions,
                    profile=profile_name,
                    model=cfg["model"],
                )
                saved_count += len(suggestions)
            ctx.progress(index, total, f"{stem} sida {page_num}")
    ctx.log(f"LLM-granskningen sparade {saved_count} förslag för {total} sidor.")
    return 0
