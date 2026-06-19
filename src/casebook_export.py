"""Exportformat för utredningspärmens sparade spår."""

from __future__ import annotations

import json


def _title_text(value: object, fallback: str) -> str:
    text = str(value or fallback)
    return " ".join(text.split())


def _source_line(source: dict) -> str:
    title = _title_text(
        source.get("title") or source.get("titel") or source.get("source"),
        "Källa",
    )
    page = f", sida {source['page']}" if source.get("page") else ""
    raw_source = source.get("source") or ""
    return f"{title}{page} (`{raw_source}`)"


def casebook_to_markdown(entries: list[dict], bookmarks: list[dict]) -> str:
    """Formatera sparade svar och bokmärken som Markdown."""
    lines = ["# Utredningspärm", ""]

    for entry in entries:
        title = _title_text(entry.get("note") or entry.get("question"), "Sparat svar")
        lines.extend([
            f"## {title}",
            "",
            f"**Fråga:** {entry.get('question') or ''}",
            "",
            str(entry.get("answer") or ""),
            "",
        ])
        sources = entry.get("sources") or []
        if sources:
            lines.extend(["### Källor", ""])
            for source in sources:
                lines.append(f"- Källa: {_source_line(source)}")
            lines.append("")
        entities = entry.get("entities") or []
        if entities:
            lines.extend(["### Entiteter", ""])
            for entity in entities:
                name = entity.get("namn") or entity.get("name") or ""
                label = entity.get("label") or ""
                suffix = f" ({label})" if label else ""
                lines.append(f"- Entitet: {name}{suffix}")
            lines.append("")

    lines.extend(["## Bokmärkta källor", ""])
    for bookmark in bookmarks:
        note = f" — {bookmark['note']}" if bookmark.get("note") else ""
        lines.append(f"- {_source_line(bookmark)}{note}")
    lines.append("")
    return "\n".join(lines)


def casebook_to_json(entries: list[dict], bookmarks: list[dict]) -> str:
    """Formatera sparade svar och bokmärken som läsbar JSON."""
    return json.dumps(
        {"entries": entries, "bookmarks": bookmarks},
        ensure_ascii=False,
        indent=2,
        default=str,
    )
