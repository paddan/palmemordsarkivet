"""Hjälpfunktioner för manuell granskning av sökträffar."""

from __future__ import annotations

import re
from pathlib import Path


def _source_stem(hit: dict) -> str:
    raw_source = str(hit.get("source") or "").strip()
    if not raw_source:
        return "Källa"
    return Path(raw_source).stem


def hit_key(hit: dict) -> str:
    """Bygg en stabil valnyckel för en sökträff."""
    source = str(hit.get("source") or "")
    page = int(hit.get("page") or 0)
    chunk_idx = int(hit.get("chunk_idx") or -1)
    return f"{source}:{page}:{chunk_idx}"


def hit_title(hit: dict) -> str:
    """Formatera en tydlig träffrubrik för manuell granskning."""
    nr = str(hit.get("nr") or "").strip()
    page = hit.get("page")
    title = str(hit.get("titel") or hit.get("title") or _source_stem(hit)).strip()

    parts = []
    if nr:
        parts.append(f"Nr {nr}")
    if page:
        parts.append(f"sida {int(page)}")

    prefix = ", ".join(parts)
    return f"{prefix} — {title}" if prefix else title


def hit_excerpt(hit: dict, max_chars: int = 500) -> str:
    """Skapa ett kompakt utdrag med kollapsat blankutrymme."""
    text = re.sub(r"\s+", " ", str(hit.get("text") or "")).strip()
    if max_chars < 4:
        return text[:max_chars]
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 3].rstrip()}..."
