"""Ren kartlogik för kartmodulen.

Modulen känner inte till Streamlit eller folium. Den validerar observationer,
bygger popup-HTML och producerar GeoJSON som ``TimestampedGeoJson`` kan
rendera.
"""

from __future__ import annotations

import hashlib
import html
import re
from collections import defaultdict
from datetime import datetime

PERSON_PALETTE: list[str] = [
    "#c1121f",
    "#f77f00",
    "#2a9d8f",
    "#457b9d",
    "#6d597a",
    "#588157",
    "#bc6c25",
    "#3a0ca3",
    "#0081a7",
    "#9d0208",
]

_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def person_color(name: str) -> str:
    """Returnera en stabil färg för ett personnamn."""
    clean = (name or "").strip().casefold()
    if not clean:
        return "#666666"
    digest = hashlib.sha1(clean.encode("utf-8")).hexdigest()
    return PERSON_PALETTE[int(digest[:8], 16) % len(PERSON_PALETTE)]


def _float_value(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _valid_time(value: str | None) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if not isinstance(value, str) or not _TIME_RE.fullmatch(value):
        return False
    hour, minute = (int(part) for part in value.split(":"))
    return 0 <= hour <= 23 and 0 <= minute <= 59


def validate_observation(obs: dict) -> list[str]:
    """Returnera fellista för en observation; tom lista betyder giltig."""
    errors: list[str] = []

    if not str(obs.get("person") or "").strip():
        errors.append("person saknas")

    lat = _float_value(obs.get("lat"))
    lon = _float_value(obs.get("lon"))
    if lat is None or lon is None:
        errors.append("lat/lon saknas")
    else:
        if not -90 <= lat <= 90:
            errors.append("lat måste ligga mellan -90 och 90")
        if not -180 <= lon <= 180:
            errors.append("lon måste ligga mellan -180 och 180")

    if not _valid_time(obs.get("time")):
        errors.append("time måste vara HH:MM")

    if not str(obs.get("nr") or "").strip() or obs.get("sida") in (None, ""):
        errors.append("källa saknas")

    return errors


def _time_key(obs: dict) -> tuple[int, str, int]:
    time_value = str(obs.get("time") or "").strip()
    if not time_value:
        return (1, "", int(obs.get("id") or 0))
    return (0, time_value, int(obs.get("id") or 0))


def observations_by_person(obs: list[dict]) -> dict[str, list[dict]]:
    """Gruppera observationer per person och sortera varje spår på tid."""
    grouped: dict[str, list[dict]] = defaultdict(list)
    for item in obs:
        person = str(item.get("person") or "").strip()
        if person:
            grouped[person].append(item)

    return {
        person: sorted(items, key=_time_key)
        for person, items in sorted(grouped.items(), key=lambda pair: pair[0].casefold())
    }


def classify_timeline_observations(obs: list[dict]) -> dict[str, list[dict]]:
    """Dela upp observationer i tidsatta, tidlösa och ogiltiga för tidslinjen."""
    classified = {
        "valid_timed": [],
        "missing_time": [],
        "invalid": [],
    }
    for item in obs:
        errors = validate_observation(item)
        if errors:
            classified["invalid"].append(item)
            continue
        if str(item.get("time") or "").strip():
            classified["valid_timed"].append(item)
        else:
            classified["missing_time"].append(item)
    return classified


def popup_html(obs: dict) -> str:
    """Bygg säker popup-HTML med person, plats, tid och källhänvisning."""
    person = html.escape(str(obs.get("person") or "Okänd").strip())
    place = html.escape(str(obs.get("place_name") or "Okänd plats").strip())
    time = html.escape(str(obs.get("time") or "okänd tid").strip())
    uncertainty = html.escape(str(obs.get("uncertainty") or "").strip())
    nr = html.escape(str(obs.get("nr") or "").strip())
    sida = html.escape(str(obs.get("sida") or "").strip())
    note = html.escape(str(obs.get("note") or "").strip())

    source = f"[Nr {nr}, sida {sida}]" if nr and sida else "[källa saknas]"
    time_label = f"kl {time}" + (f" ({uncertainty})" if uncertainty else "")

    parts = [
        f"<strong>{person}</strong>",
        f"<br>{place}",
        f"<br>{time_label}",
        f"<br>{source}",
    ]
    if note:
        parts.append(f"<br><em>{note}</em>")
    return "".join(parts)


def observation_source_payload(obs: dict, *, source_stem: str | None = None) -> dict | None:
    """Bygg källkortspayload när både källa och explicit arkivstam finns."""
    nr = str(obs.get("nr") or "").strip()
    sida = obs.get("sida")
    stem = str(source_stem or "").strip()
    if not nr or sida in (None, "") or not stem:
        return None

    title = str(obs.get("place_name") or obs.get("person") or nr).strip()
    source = stem if stem.endswith(".txt") else f"{stem}.txt"
    return {
        "source": source,
        "page": sida,
        "nr": nr,
        "title": title,
    }


def observation_source_payloads(obs: dict, *, source_stems: list[str]) -> list[dict]:
    """Bygg ett eller flera källkort utan att dubblera samma textkälla."""
    payloads: list[dict] = []
    seen_sources: set[str] = set()
    for stem in source_stems:
        payload = observation_source_payload(obs, source_stem=stem)
        if not payload:
            continue
        source = str(payload["source"])
        if source in seen_sources:
            continue
        seen_sources.add(source)
        payloads.append(payload)
    return payloads


def db_observation_payload(payload: dict) -> dict:
    """Normalisera payload före DB-anrop."""
    normalized = dict(payload)
    time_value = normalized.get("time")
    if isinstance(time_value, str) and not time_value.strip():
        normalized["time"] = None
    return normalized


def legend_person_html(person: str) -> str:
    """Bygg säker legendrad för en person i kartans sidospalt."""
    color = person_color(person)
    safe_person = html.escape(str(person).strip())
    return f"<span style='color:{color}'>■</span> {safe_person}"


def map_form_defaults(
    selected: dict | None,
    *,
    picked_place: dict | None,
    latest_click: dict | None,
    use_latest_click: bool,
) -> dict[str, float | str]:
    """Beräkna formulärets förifyllning från vald post, plats och kartklick."""
    place_name = str(selected.get("place_name") or "").strip() if selected else ""
    lat = float(selected["lat"]) if selected else 59.33695
    lon = float(selected["lon"]) if selected else 18.06324

    if picked_place:
        place_name = str(picked_place.get("name") or "").strip()
        lat = float(picked_place["lat"])
        lon = float(picked_place["lon"])

    if use_latest_click and latest_click:
        lat = float(latest_click["lat"])
        lon = float(latest_click["lng"])

    return {
        "place_name": place_name,
        "lat": lat,
        "lon": lon,
    }


def _iso_time(base_date: str, hhmm: str) -> str:
    stamp = datetime.strptime(f"{base_date} {hhmm}", "%Y-%m-%d %H:%M")
    return stamp.isoformat(timespec="seconds")


def _point_feature(obs: dict, base_date: str) -> dict:
    person = str(obs.get("person") or "").strip()
    color = person_color(person)
    return {
        "type": "Feature",
        "geometry": {
            "type": "Point",
            "coordinates": [float(obs["lon"]), float(obs["lat"])],
        },
        "properties": {
            "time": _iso_time(base_date, str(obs["time"]).strip()),
            "times": [_iso_time(base_date, str(obs["time"]).strip())],
            "popup": popup_html(obs),
            "icon": "circle",
            "iconstyle": {
                "fillColor": color,
                "fillOpacity": 0.9,
                "stroke": True,
                "color": color,
                "radius": 8,
            },
            "style": {"color": color},
        },
    }


def _line_feature(person: str, points: list[dict], base_date: str) -> dict | None:
    if len(points) < 2:
        return None
    color = person_color(person)
    return {
        "type": "Feature",
        "geometry": {
            "type": "LineString",
            "coordinates": [
                [float(obs["lon"]), float(obs["lat"])]
                for obs in points
            ],
        },
        "properties": {
            "times": [_iso_time(base_date, str(obs["time"]).strip()) for obs in points],
            "popup": popup_html(points[-1]),
            "style": {"color": color, "weight": 4, "opacity": 0.7},
        },
    }


def build_timestamped_geojson(
    obs: list[dict], *, base_date: str = "1986-02-28"
) -> dict:
    """Bygg GeoJSON för TimestampedGeoJson från observationer med tid."""
    features: list[dict] = []
    grouped = observations_by_person(classify_timeline_observations(obs)["valid_timed"])

    for person, items in grouped.items():
        for item in items:
            features.append(_point_feature(item, base_date))
        line = _line_feature(person, items, base_date)
        if line is not None:
            features.append(line)

    return {"type": "FeatureCollection", "features": features}
