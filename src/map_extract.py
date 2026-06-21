"""Ren logik för att extrahera kartobservations-kandidater ur LLM-svar."""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_TIME_RE = re.compile(r"(?P<hour>[01]?\d|2[0-3])[\s.:](?P<minute>[0-5]\d)")
_CONFIDENCE = {"low", "medium", "high"}


def parse_map_observation_extraction(raw: str) -> list[dict]:
    """Returnera validerade observationsrader ur ett LLM-svar."""
    match = _JSON_RE.search(raw or "")
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    out: list[dict] = []
    for item in data.get("observationer", []):
        if not isinstance(item, dict):
            continue
        person = str(item.get("person") or "").strip()
        plats = str(item.get("plats") or "").strip()
        citat = str(item.get("citat") or "").strip()
        confidence = str(item.get("confidence") or "medium").strip()
        if confidence not in _CONFIDENCE:
            confidence = "medium"
        if not person or not plats or not citat:
            continue
        out.append({
            "person": person,
            "plats": plats,
            "tid": str(item.get("tid") or "").strip(),
            "citat": citat,
            "notering": str(item.get("notering") or "").strip(),
            "confidence": confidence,
        })
    return out


def normalize_observation_time(value: str | None) -> tuple[str | None, str | None]:
    """Normalisera vanliga tidsformer till HH:MM och separat osäkerhet."""
    text = str(value or "").strip()
    if not text:
        return None, None
    match = _TIME_RE.search(text)
    if not match:
        return None, text
    hhmm = f"{int(match.group('hour')):02d}:{match.group('minute')}"
    before = text[:match.start()].strip(" ,.;:-")
    after = text[match.end():].strip(" ,.;:-")
    uncertainty = " ".join(part for part in (before, after) if part) or None
    if uncertainty:
        uncertainty = uncertainty.replace("kl", "").strip() or uncertainty
    return hhmm, uncertainty


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.casefold()).strip()


def build_place_index(places: list[dict]) -> list[dict]:
    """Bygg enkel platsindexlista från kartans platskatalog."""
    index: list[dict] = []
    for place in places:
        name = str(place.get("name") or "").strip()
        if not name:
            continue
        aliases = {name}
        for part in re.split(r"[/(),]", name):
            clean = part.strip()
            if len(clean) >= 4:
                aliases.add(clean)
        index.append({
            "name": name,
            "lat": float(place["lat"]),
            "lon": float(place["lon"]),
            "aliases": sorted(aliases),
        })
    return index


def match_place(raw_place: str, place_index: list[dict]) -> dict:
    """Matcha rå plats mot katalogen och returnera koordinater om säkert."""
    raw = _norm(raw_place)
    if not raw:
        return {"place_name": None, "lat": None, "lon": None, "place_match": "none"}
    best: tuple[float, dict | None] = (0.0, None)
    for place in place_index:
        for alias in place["aliases"]:
            alias_norm = _norm(alias)
            if raw == alias_norm:
                return {
                    "place_name": place["name"],
                    "lat": place["lat"],
                    "lon": place["lon"],
                    "place_match": "exact",
                }
            if raw in alias_norm or alias_norm in raw:
                score = 0.86
            else:
                score = SequenceMatcher(a=raw, b=alias_norm).ratio()
            if score > best[0]:
                best = (score, place)
    if best[1] is not None and best[0] >= 0.72:
        place = best[1]
        return {
            "place_name": place["name"],
            "lat": place["lat"],
            "lon": place["lon"],
            "place_match": "fuzzy",
        }
    return {"place_name": None, "lat": None, "lon": None, "place_match": "none"}


def candidate_payload(
    obs: dict,
    *,
    pdf_stem: str,
    page_num: int,
    nr: str,
    model: str,
    place_index: list[dict],
) -> dict | None:
    """Bygg DB-payload för en kartobservationskandidat."""
    person = str(obs.get("person") or "").strip()
    raw_place = str(obs.get("plats") or "").strip()
    quote = str(obs.get("citat") or "").strip()
    if not person or not raw_place or not quote:
        return None
    time_value, inferred_uncertainty = normalize_observation_time(obs.get("tid"))
    matched = match_place(raw_place, place_index)
    confidence = str(obs.get("confidence") or "medium").strip()
    if confidence not in _CONFIDENCE:
        confidence = "medium"
    return {
        "pdf_stem": pdf_stem,
        "page_num": int(page_num),
        "person": person,
        "raw_place": raw_place,
        "place_name": matched["place_name"],
        "lat": matched["lat"],
        "lon": matched["lon"],
        "time": time_value,
        "uncertainty": inferred_uncertainty,
        "nr": nr,
        "sida": int(page_num),
        "quote": quote,
        "note": str(obs.get("notering") or "").strip() or None,
        "confidence": confidence,
        "place_match": matched["place_match"],
        "model": model,
    }
