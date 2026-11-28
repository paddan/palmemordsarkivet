"""Rader till dokumentlagret i Neo4j, byggda ur ``document_meta`` (ren logik).

Ingen databas- eller Neo4j-kod här: ``build_document_rows`` tar färdiga
metadatarader och sidantal och ger listor av parametrar till inläsaren.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


def count_pages(text: str) -> int:
    """Antal sidor i en textfil — samma indelning som ``rag.ingest.split_pages``.

    Så att ``Sida.sida`` motsvarar LanceDB:s ``page``. Textfilerna slutar med
    ``\f``, så ett tomt sista segment (efter ``strip()``) är ingen sida och
    räknas inte; sidnumren för övriga sidor ändras inte. ``rag.ingest``
    importeras inte (tung import).
    """
    segment = text.split("\f")
    if len(segment) > 1 and not segment[-1].strip():
        return len(segment) - 1
    return len(segment)


def nr_key(nr: str) -> str:
    """Normaliserat dokumentnummer: blanksteg bort och komma som punkt."""
    return nr.strip().replace(",", ".")


@dataclass(frozen=True)
class DocumentRows:
    documents: list[dict[str, Any]]
    pages: list[dict[str, Any]]
    uppslag: list[dict[str, Any]]
    references: list[dict[str, Any]]
    parts: list[dict[str, Any]]
    unresolved_references: int


def build_document_rows(
    metas: list[dict[str, Any]], page_counts: Mapping[str, int]
) -> DocumentRows:
    """Bygg dokument-, sid-, uppslags-, hänvisnings- och delrader."""
    documents: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    uppslag: list[dict[str, Any]] = []
    by_nr: dict[str, list[str]] = defaultdict(list)

    for meta in metas:
        stem = meta["pdf_stem"]
        sidor = page_counts.get(stem, 0)
        documents.append({
            "stem": stem, "arkiv": meta["arkiv"], "nr": meta["nr"],
            "titel": meta["titel"], "bestallt": meta["bestallt"],
            "upplagt": meta["upplagt"], "anmarkning": meta["anmarkning"],
            "antal_sidor": meta["antal_sidor"],
            "dokumentdatum": meta["dokumentdatum"],
            "avkortat": meta["avkortat"], "sidor": sidor,
        })
        pages.extend(
            {"id": f"{stem}#{n}", "stem": stem, "sida": n}
            for n in range(1, sidor + 1)
        )
        uppslag.extend({"stem": stem, "kod": kod} for kod in meta["uppslag"])
        if meta["arkiv"] == "palme" and meta.get("nr"):
            by_nr[nr_key(meta["nr"])].append(stem)

    references: list[dict[str, Any]] = []
    parts: list[dict[str, Any]] = []
    unresolved = 0
    for meta in metas:
        if meta["arkiv"] != "palme":
            continue
        stem = meta["pdf_stem"]
        for ref in meta["hanvisningar"]:
            matches = by_nr.get(nr_key(ref), [])
            if not matches:  # en självhänvisning är löst, men ger ingen kant
                unresolved += 1
            targets = [t for t in matches if t != stem]
            references.extend({"stem": stem, "target_stem": t} for t in targets)
        if not meta.get("nr"):
            continue
        segments = [s for s in nr_key(meta["nr"]).split(".") if s]
        while len(segments) > 1:
            segments.pop()
            parents = by_nr.get(".".join(segments))
            if parents:
                parts.extend(
                    {"stem": stem, "parent_stem": p} for p in parents if p != stem
                )
                break
    return DocumentRows(documents, pages, uppslag, references, parts, unresolved)
