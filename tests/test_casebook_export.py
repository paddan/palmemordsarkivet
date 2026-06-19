"""Tester för export av utredningspärmen."""

from __future__ import annotations

import json

import casebook_export


def _entries() -> list[dict]:
    return [
        {
            "id": 7,
            "question": "Vilka nämner Skandia?",
            "answer": "Skandia nämns i flera förhör.",
            "mode": "hybrid",
            "backend": "mcp",
            "model": "opus",
            "note": "Skandiaspåret",
            "sources": [
                {
                    "source": "100 — Skandia.txt",
                    "page": 28,
                    "title": "Skandiaförhör",
                }
            ],
            "entities": [
                {"namn": "Skandia", "label": "Organisation", "norm": "skandia"}
            ],
        }
    ]


def _bookmarks() -> list[dict]:
    return [
        {
            "id": 3,
            "source": "865 — Brev.txt",
            "page": 2,
            "nr": "865",
            "title": "Brev till Palmegruppen",
            "note": "Läs igen",
        }
    ]


def test_casebook_to_markdown_exports_entries_and_bookmarks() -> None:
    markdown = casebook_export.casebook_to_markdown(_entries(), _bookmarks())

    assert markdown.startswith("# Utredningspärm\n")
    assert "## Skandiaspåret\n" in markdown
    assert "**Fråga:** Vilka nämner Skandia?" in markdown
    assert "Skandia nämns i flera förhör." in markdown
    assert "- Källa: Skandiaförhör, sida 28 (`100 — Skandia.txt`)" in markdown
    assert "- Entitet: Skandia (Organisation)" in markdown
    assert "## Bokmärkta källor\n" in markdown
    assert "- Brev till Palmegruppen, sida 2 (`865 — Brev.txt`) — Läs igen" in markdown


def test_casebook_to_json_exports_pretty_json() -> None:
    payload = casebook_export.casebook_to_json(_entries(), _bookmarks())

    assert "\n  \"entries\": [" in payload
    data = json.loads(payload)
    assert data["entries"] == _entries()
    assert data["bookmarks"] == _bookmarks()
