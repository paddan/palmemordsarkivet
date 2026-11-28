"""Urval av sidor för ontologi-extraktionen.

Ren funktion utan I/O: den som anropar läser dokumentmetadata, sidtexter,
tidigare extraktioner och sökträffar, och får tillbaka en ordnad lista sidor
plus statistik över varför sidor valdes eller valdes bort.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .config import Urval
from .extraction import text_hash

log = logging.getLogger(__name__)

MIN_ALFANUM = 100
_SERIE = re.compile(r"^[A-ZÅÄÖ]+")
# Lägre tal = högre prioritet i ordningen.
_PRIO = {"uttrycklig": 0, "sok": 1, "serie": 2}


@dataclass(frozen=True)
class PageRef:
    pdf_stem: str
    sida: int
    skal: str  # serie | sok | uttrycklig


def series_of(kod: str) -> str:
    """De inledande versalerna i en uppslagsnyckel (``EAE621-00`` → ``EAE``)."""
    m = _SERIE.match(kod)
    return m.group(0) if m else ""


def _alfanum(text: str) -> int:
    return sum(1 for ch in text if ch.isalnum())


def select_pages(
    metas: list[dict[str, Any]],
    page_texts: Mapping[str, list[str]],
    urval: Urval,
    extractions: Mapping[tuple[str, int], dict[str, Any]],
    *,
    schemaversion: int,
    reviewed: set[tuple[str, int]],
    search_hits: list[tuple[str, int]],
    max_pages: int,
    force: bool,
) -> tuple[list[PageRef], dict[str, int]]:
    """Välj de sidor som ska extraheras, i prioritetsordning.

    ``page_texts[stam][n - 1]`` är sida n. Kandidater kommer ur serier,
    sökträffar, hela stammar och uttryckligt angivna sidor; en sida med flera
    skäl får det starkaste (uttrycklig före sök före serie). ``force`` väljer
    om även granskade sidor som bara ändrat schemaversion; en sida med
    oförändrad text och schemaversion väljs aldrig om.
    """
    stats: dict[str, int] = {
        "serie": 0, "sok": 0, "uttrycklig": 0,
        "utanför": 0, "kort": 0, "oförändrad": 0, "granskad": 0, "avkortade": 0,
    }
    serier = set(urval.serier)
    kandidater: dict[tuple[str, int], str] = {}

    def lagg(stem: str, sida: int, skal: str) -> None:
        texts = page_texts.get(stem)
        if texts is None or not 1 <= sida <= len(texts):
            stats["utanför"] += 1
            log.debug("utanför: %s sida %s (%s) saknar text", stem, sida, skal)
            return
        nyckel = (stem, sida)
        if nyckel not in kandidater or _PRIO[skal] < _PRIO[kandidater[nyckel]]:
            kandidater[nyckel] = skal

    for meta in metas:
        stem = meta["pdf_stem"]
        if stem not in page_texts:
            continue
        if any(series_of(k) in serier for k in meta.get("uppslag") or []):
            for sida in range(1, len(page_texts[stem]) + 1):
                lagg(stem, sida, "serie")
    for stem, sida in search_hits:
        lagg(stem, sida, "sok")
    for stem in urval.stammar:
        if stem not in page_texts:
            stats["utanför"] += 1
            continue
        for sida in range(1, len(page_texts[stem]) + 1):
            lagg(stem, sida, "uttrycklig")
    for stem, sida in urval.sidor:
        lagg(stem, sida, "uttrycklig")

    valda: list[PageRef] = []
    for (stem, sida), skal in sorted(
        kandidater.items(), key=lambda kv: (_PRIO[kv[1]], kv[0])
    ):
        text = page_texts[stem][sida - 1]
        if _alfanum(text) < MIN_ALFANUM:
            stats["kort"] += 1
            continue
        tidigare = extractions.get((stem, sida))
        if tidigare is not None and tidigare["text_hash"] == text_hash(text):
            if tidigare["schemaversion"] == schemaversion:
                stats["oförändrad"] += 1
                continue
            if (stem, sida) in reviewed and not force:
                stats["granskad"] += 1
                log.debug(
                    "hoppar %s sida %s: granskad, ny schemaversion %s mot %s",
                    stem, sida, schemaversion, tidigare["schemaversion"],
                )
                continue
        valda.append(PageRef(stem, sida, skal))

    if len(valda) > max_pages:
        stats["avkortade"] = len(valda) - max_pages
        valda = valda[:max_pages]
    for ref in valda:
        stats[ref.skal] += 1
    return valda, stats
