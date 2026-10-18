"""Karta: platser, observationer och granskningskö för kartobservations-kandidater."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from ._schema import now

# --- karta ------------------------------------------------------------

_MAP_OBSERVATION_FIELDS = {
    "person", "place_name", "lat", "lon", "time", "uncertainty", "nr", "sida", "note"
}

_TIME_HH_MM_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def _row_dict(row: sqlite3.Row) -> dict:
    """Gör om en sqlite-rad till en vanlig dict."""
    return dict(row)


def _validate_lat_lon(lat: Any, lon: Any) -> tuple[float, float]:
    """Validera och normalisera koordinater till float.

    Parametrarna är Any eftersom seed-data och JSON-inmatning kan innehålla
    strängar — float()-anropet nedan är den faktiska normaliseringen.
    """
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError) as exc:
        raise ValueError("lat/lon måste vara tal") from exc
    if not -90 <= lat_f <= 90:
        raise ValueError("lat måste ligga mellan -90 och 90")
    if not -180 <= lon_f <= 180:
        raise ValueError("lon måste ligga mellan -180 och 180")
    return lat_f, lon_f


def _validate_time_hh_mm(time_value: str | None) -> str | None:
    """Validera strikt tid i formatet HH:MM eller returnera None."""
    if time_value is None:
        return None
    if not isinstance(time_value, str) or not _TIME_HH_MM_RE.fullmatch(time_value):
        raise ValueError("time måste vara i formatet HH:MM mellan 00:00 och 23:59")
    return time_value


def record_map_place(conn: sqlite3.Connection, *, name: str, lat: float, lon: float) -> int:
    """Spara en plats i kartans platskatalog och returnera rad-id."""
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("name får inte vara tom")
    lat_f, lon_f = _validate_lat_lon(lat, lon)
    cur = conn.execute(
        """
        INSERT INTO map_places(name, lat, lon, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (clean_name, lat_f, lon_f, now()),
    )
    conn.commit()
    row_id = cur.lastrowid
    assert row_id is not None  # INSERT med autoincrement ger alltid ett rad-id
    return row_id


def list_map_places(conn: sqlite3.Connection) -> list[dict]:
    """Lista alla platser i kartan alfabetiskt på namn."""
    rows = conn.execute(
        """
        SELECT id, name, lat, lon, created_at
        FROM map_places
        ORDER BY name COLLATE NOCASE, id
        """
    )
    return [_row_dict(row) for row in rows]


def delete_map_place(conn: sqlite3.Connection, place_id: int) -> bool:
    """Radera en plats ur platskatalogen."""
    cur = conn.execute("DELETE FROM map_places WHERE id=?", (place_id,))
    conn.commit()
    return cur.rowcount > 0


def list_map_observations(
    conn: sqlite3.Connection, *, person: str | None = None
) -> list[dict]:
    """Lista kartobservationer, nyast först inom samma tid/person."""
    params: tuple = ()
    where = ""
    if person and person.strip():
        where = "WHERE person = ?"
        params = (person.strip(),)
    rows = conn.execute(
        f"""
        SELECT id, person, place_name, lat, lon, time, uncertainty,
               nr, sida, note, created_at, updated_at
        FROM map_observations
        {where}
        ORDER BY COALESCE(time, '99:99') DESC, person COLLATE NOCASE, id DESC
        """,
        params,
    )
    return [_row_dict(row) for row in rows]


def _insert_map_observation(
    conn: sqlite3.Connection,
    *,
    person: str,
    place_name: str | None = None,
    lat: float,
    lon: float,
    time: str | None = None,
    uncertainty: str | None = None,
    nr: str | None = None,
    sida: int | None = None,
    note: str | None = None,
) -> int:
    """Infoga en kartobservation utan att avsluta anroparens transaktion."""
    clean_person = person.strip()
    if not clean_person:
        raise ValueError("person får inte vara tom")
    lat_f, lon_f = _validate_lat_lon(lat, lon)
    time_value = _validate_time_hh_mm(time)
    stamp = now()
    cur = conn.execute(
        """
        INSERT INTO map_observations(
            person, place_name, lat, lon, time, uncertainty,
            nr, sida, note, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            clean_person,
            place_name.strip() if place_name and place_name.strip() else None,
            lat_f,
            lon_f,
            time_value,
            uncertainty.strip() if uncertainty and uncertainty.strip() else None,
            nr.strip() if nr and nr.strip() else None,
            int(sida) if sida is not None else None,
            note.strip() if note and note.strip() else None,
            stamp,
            stamp,
        ),
    )
    row_id = cur.lastrowid
    assert row_id is not None  # INSERT med autoincrement ger alltid ett rad-id
    return row_id


def record_map_observation(
    conn: sqlite3.Connection,
    *,
    person: str,
    place_name: str | None = None,
    lat: float,
    lon: float,
    time: str | None = None,
    uncertainty: str | None = None,
    nr: str | None = None,
    sida: int | None = None,
    note: str | None = None,
) -> int:
    """Spara en källhänvisad kartobservation och returnera rad-id."""
    observation_id = _insert_map_observation(
        conn,
        person=person,
        place_name=place_name,
        lat=lat,
        lon=lon,
        time=time,
        uncertainty=uncertainty,
        nr=nr,
        sida=sida,
        note=note,
    )
    conn.commit()
    return observation_id


def update_map_observation(conn: sqlite3.Connection, obs_id: int, **fields) -> bool:
    """Uppdatera valda fält på en kartobservation."""
    unknown = set(fields) - _MAP_OBSERVATION_FIELDS
    if unknown:
        raise ValueError(f"okända kartfält: {', '.join(sorted(unknown))}")
    if not fields:
        return False
    cleaned = dict(fields)
    if "person" in cleaned:
        person = str(cleaned["person"]).strip()
        if not person:
            raise ValueError("person får inte vara tom")
        cleaned["person"] = person
    if "time" in cleaned:
        cleaned["time"] = _validate_time_hh_mm(cleaned["time"])
    if "lat" in cleaned or "lon" in cleaned:
        current = conn.execute(
            "SELECT lat, lon FROM map_observations WHERE id=?", (obs_id,)
        ).fetchone()
        if current is None:
            return False
        lat_f, lon_f = _validate_lat_lon(
            cleaned.get("lat", current["lat"]),
            cleaned.get("lon", current["lon"]),
        )
        cleaned["lat"] = lat_f
        cleaned["lon"] = lon_f
    for key in ("place_name", "time", "uncertainty", "nr", "note"):
        if key == "time":
            continue
        if key in cleaned:
            value = cleaned[key]
            cleaned[key] = value.strip() if isinstance(value, str) and value.strip() else None
    if "sida" in cleaned and cleaned["sida"] is not None:
        cleaned["sida"] = int(cleaned["sida"])
    cleaned["updated_at"] = now()
    assignments = ", ".join(f"{key}=?" for key in cleaned)
    values = list(cleaned.values()) + [obs_id]
    cur = conn.execute(
        f"UPDATE map_observations SET {assignments} WHERE id=?",
        values,
    )
    conn.commit()
    return cur.rowcount > 0


def delete_map_observation(conn: sqlite3.Connection, obs_id: int) -> bool:
    """Radera en kartobservation."""
    cur = conn.execute("DELETE FROM map_observations WHERE id=?", (obs_id,))
    conn.commit()
    return cur.rowcount > 0


def seed_map_data_if_empty(
    conn: sqlite3.Connection, places: list[dict], observations: list[dict]
) -> int:
    """Seeda karttabellerna om både plats- och observationstabell är tomma."""
    has_places = conn.execute("SELECT 1 FROM map_places LIMIT 1").fetchone()
    has_obs = conn.execute("SELECT 1 FROM map_observations LIMIT 1").fetchone()
    if has_places or has_obs:
        return 0

    normalized_places: list[tuple[str, float, float]] = []
    for place in places:
        name = str(place.get("name") or "").strip()
        if not name:
            raise ValueError("seed-plats saknar name")
        lat_f, lon_f = _validate_lat_lon(place.get("lat"), place.get("lon"))
        normalized_places.append((name, lat_f, lon_f))

    normalized_observations: list[tuple[str, str | None, float, float, str | None, str | None, str | None, int | None, str | None]] = []
    for obs in observations:
        person = str(obs.get("person") or "").strip()
        if not person:
            raise ValueError("seed-observation saknar person")
        lat_f, lon_f = _validate_lat_lon(obs.get("lat"), obs.get("lon"))
        normalized_observations.append(
            (
                person,
                str(obs.get("place_name") or "").strip() or None,
                lat_f,
                lon_f,
                _validate_time_hh_mm(obs.get("time")),
                str(obs.get("uncertainty") or "").strip() or None,
                str(obs.get("nr") or "").strip() or None,
                int(obs["sida"]) if obs.get("sida") is not None else None,
                str(obs.get("note") or "").strip() or None,
            )
        )

    inserted = 0
    stamp = now()
    for name, lat_f, lon_f in normalized_places:
        conn.execute(
            "INSERT INTO map_places(name, lat, lon, created_at) VALUES (?, ?, ?, ?)",
            (name, lat_f, lon_f, stamp),
        )
        inserted += 1
    for (
        person,
        place_name,
        lat_f,
        lon_f,
        time_value,
        uncertainty,
        nr,
        sida,
        note,
    ) in normalized_observations:
        conn.execute(
            """
            INSERT INTO map_observations(
                person, place_name, lat, lon, time, uncertainty,
                nr, sida, note, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                person,
                place_name,
                lat_f,
                lon_f,
                time_value,
                uncertainty,
                nr,
                sida,
                note,
                stamp,
                stamp,
            ),
        )
        inserted += 1
    conn.commit()
    return inserted


# --- kartobservations-kandidater (granskningskö) ----------------------

_MAP_CANDIDATE_FIELDS = {
    "person", "raw_place", "place_name", "lat", "lon", "time", "uncertainty",
    "nr", "sida", "quote", "note", "confidence", "place_match", "status",
}
_CANDIDATE_CONFIDENCE = {"low", "medium", "high"}
_CANDIDATE_PLACE_MATCH = {"none", "fuzzy", "exact"}
_CANDIDATE_STATUS = {"pending", "approved", "rejected"}


def _clean_required_text(value: str | None, field: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise ValueError(f"{field} får inte vara tom")
    return clean


def _validate_optional_lat_lon(lat, lon) -> tuple[float | None, float | None]:
    if lat is None and lon is None:
        return None, None
    if lat is None or lon is None:
        raise ValueError("lat och lon måste anges tillsammans")
    return _validate_lat_lon(lat, lon)


def _validate_choice(value: str, allowed: set[str], field: str) -> str:
    clean = _clean_required_text(value, field)
    if clean not in allowed:
        raise ValueError(f"{field} måste vara en av: {', '.join(sorted(allowed))}")
    return clean


def record_map_observation_candidate(
    conn: sqlite3.Connection,
    *,
    pdf_stem: str,
    page_num: int,
    person: str,
    raw_place: str,
    place_name: str | None,
    lat: float | None,
    lon: float | None,
    time: str | None,
    uncertainty: str | None,
    nr: str,
    sida: int,
    quote: str,
    note: str | None,
    confidence: str,
    place_match: str,
    model: str,
) -> int:
    """Spara eller uppdatera en granskningskandidat för kartan."""
    clean_pdf_stem = _clean_required_text(pdf_stem, "pdf_stem")
    clean_person = _clean_required_text(person, "person")
    clean_raw_place = _clean_required_text(raw_place, "raw_place")
    clean_nr = _clean_required_text(nr, "nr")
    clean_quote = _clean_required_text(quote, "quote")
    clean_model = _clean_required_text(model, "model")
    time_value = _validate_time_hh_mm(time)
    lat_f, lon_f = _validate_optional_lat_lon(lat, lon)
    clean_confidence = _validate_choice(confidence, _CANDIDATE_CONFIDENCE, "confidence")
    clean_place_match = _validate_choice(place_match, _CANDIDATE_PLACE_MATCH, "place_match")
    existing = conn.execute(
        """
        SELECT id FROM map_observation_candidates
        WHERE pdf_stem=?
          AND page_num=?
          AND person COLLATE NOCASE=?
          AND raw_place COLLATE NOCASE=?
          AND COALESCE(time, '')=COALESCE(?, '')
          AND quote=?
        """,
        (
            clean_pdf_stem,
            int(page_num),
            clean_person,
            clean_raw_place,
            time_value,
            clean_quote,
        ),
    ).fetchone()
    stamp = now()
    values = (
        place_name.strip() if place_name and place_name.strip() else None,
        lat_f,
        lon_f,
        uncertainty.strip() if uncertainty and uncertainty.strip() else None,
        clean_nr,
        int(sida),
        note.strip() if note and note.strip() else None,
        clean_confidence,
        clean_place_match,
        clean_model,
        stamp,
    )
    if existing:
        conn.execute(
            """
            UPDATE map_observation_candidates
            SET place_name=?, lat=?, lon=?, uncertainty=?, nr=?, sida=?, note=?,
                confidence=?, place_match=?, model=?, updated_at=?
            WHERE id=?
            """,
            (*values, existing["id"]),
        )
        conn.commit()
        return int(existing["id"])
    cur = conn.execute(
        """
        INSERT INTO map_observation_candidates(
            pdf_stem, page_num, person, raw_place, place_name, lat, lon,
            time, uncertainty, nr, sida, quote, note, confidence,
            place_match, status, model, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
        """,
        (
            clean_pdf_stem,
            int(page_num),
            clean_person,
            clean_raw_place,
            values[0],
            values[1],
            values[2],
            time_value,
            values[3],
            clean_nr,
            int(sida),
            clean_quote,
            values[6],
            clean_confidence,
            clean_place_match,
            clean_model,
            stamp,
            stamp,
        ),
    )
    conn.commit()
    row_id = cur.lastrowid
    assert row_id is not None  # INSERT med autoincrement ger alltid ett rad-id
    return row_id


def map_observation_candidate_exists(
    conn: sqlite3.Connection, pdf_stem: str, page_num: int
) -> bool:
    """Sann om en kandidat redan finns för en viss källsida (valfri status)."""
    return conn.execute(
        "SELECT 1 FROM map_observation_candidates WHERE pdf_stem=? AND page_num=? LIMIT 1",
        (pdf_stem, int(page_num)),
    ).fetchone() is not None


def mark_map_observation_extracted(
    conn: sqlite3.Connection,
    *,
    pdf_stem: str,
    page_num: int,
    model: str,
    observations: int,
) -> None:
    """Markera att kartobservations-extraktion körts för en sida."""
    clean_pdf_stem = _clean_required_text(pdf_stem, "pdf_stem")
    clean_model = _clean_required_text(model, "model")
    count = int(observations)
    if count < 0:
        raise ValueError("observations får inte vara negativt")
    conn.execute(
        """
        INSERT INTO map_observation_extractions(
            pdf_stem, page_num, model, observations, extracted_at
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(pdf_stem, page_num) DO UPDATE SET
            model        = excluded.model,
            observations = excluded.observations,
            extracted_at = excluded.extracted_at
        """,
        (clean_pdf_stem, int(page_num), clean_model, count, now()),
    )
    conn.commit()


def map_observation_extracted(
    conn: sqlite3.Connection, pdf_stem: str, page_num: int
) -> bool:
    """Sann om kartobservations-extraktion redan körts för sidan."""
    return conn.execute(
        """
        SELECT 1 FROM map_observation_extractions
        WHERE pdf_stem=? AND page_num=?
        LIMIT 1
        """,
        (pdf_stem, int(page_num)),
    ).fetchone() is not None


def list_map_observation_candidates(
    conn: sqlite3.Connection, *, status: str = "pending", limit: int = 100
) -> list[dict]:
    """Lista kartkandidater för granskning."""
    clean_status = _validate_choice(status, _CANDIDATE_STATUS, "status")
    rows = conn.execute(
        """
        SELECT id, pdf_stem, page_num, person, raw_place, place_name, lat, lon,
               time, uncertainty, nr, sida, quote, note, confidence,
               place_match, status, model, map_observation_id,
               created_at, updated_at, reviewed_at
        FROM map_observation_candidates
        WHERE status=?
        ORDER BY updated_at DESC, id DESC
        LIMIT ?
        """,
        (clean_status, int(limit)),
    )
    return [_row_dict(row) for row in rows]


def update_map_observation_candidate(
    conn: sqlite3.Connection, candidate_id: int, **fields
) -> bool:
    """Uppdatera granskningsfält på en kartkandidat."""
    unknown = set(fields) - _MAP_CANDIDATE_FIELDS
    if unknown:
        raise ValueError(f"okända kandidatfält: {', '.join(sorted(unknown))}")
    if not fields:
        return False
    cleaned = dict(fields)
    for key in ("person", "raw_place", "nr", "quote"):
        if key in cleaned:
            cleaned[key] = _clean_required_text(cleaned[key], key)
    if "time" in cleaned:
        cleaned["time"] = _validate_time_hh_mm(cleaned["time"])
    if "confidence" in cleaned:
        cleaned["confidence"] = _validate_choice(cleaned["confidence"], _CANDIDATE_CONFIDENCE, "confidence")
    if "place_match" in cleaned:
        cleaned["place_match"] = _validate_choice(cleaned["place_match"], _CANDIDATE_PLACE_MATCH, "place_match")
    if "status" in cleaned:
        cleaned["status"] = _validate_choice(cleaned["status"], _CANDIDATE_STATUS, "status")
    if "lat" in cleaned or "lon" in cleaned:
        current = conn.execute(
            "SELECT lat, lon FROM map_observation_candidates WHERE id=?",
            (candidate_id,),
        ).fetchone()
        if current is None:
            return False
        lat_f, lon_f = _validate_optional_lat_lon(
            cleaned.get("lat", current["lat"]),
            cleaned.get("lon", current["lon"]),
        )
        cleaned["lat"] = lat_f
        cleaned["lon"] = lon_f
    for key in ("place_name", "uncertainty", "note"):
        if key in cleaned:
            value = cleaned[key]
            cleaned[key] = value.strip() if isinstance(value, str) and value.strip() else None
    if "sida" in cleaned:
        cleaned["sida"] = int(cleaned["sida"])
    cleaned["updated_at"] = now()
    assignments = ", ".join(f"{key}=?" for key in cleaned)
    values = list(cleaned.values()) + [candidate_id]
    cur = conn.execute(
        f"UPDATE map_observation_candidates SET {assignments} WHERE id=?",
        values,
    )
    conn.commit()
    return cur.rowcount > 0


def reject_map_observation_candidate(conn: sqlite3.Connection, candidate_id: int) -> bool:
    """Markera en kartkandidat som avvisad."""
    stamp = now()
    cur = conn.execute(
        """
        UPDATE map_observation_candidates
        SET status='rejected', updated_at=?, reviewed_at=?
        WHERE id=? AND status='pending'
        """,
        (stamp, stamp, candidate_id),
    )
    conn.commit()
    return cur.rowcount > 0


def approve_map_observation_candidate(conn: sqlite3.Connection, candidate_id: int) -> int:
    """Skapa en publicerad kartobservation från en granskad kandidat."""
    with conn:
        row = conn.execute(
            """
            SELECT id, person, raw_place, place_name, lat, lon, time, uncertainty,
                   nr, sida, note, quote
            FROM map_observation_candidates
            WHERE id=? AND status='pending'
            """,
            (candidate_id,),
        ).fetchone()
        if row is None:
            raise ValueError("kandidaten saknas eller är redan granskad")
        if row["lat"] is None or row["lon"] is None:
            raise ValueError("lat/lon krävs innan kandidaten kan godkännas")
        if not row["time"]:
            raise ValueError("time krävs innan kandidaten kan godkännas")
        if not row["nr"] or row["sida"] is None:
            raise ValueError("nr och sida krävs innan kandidaten kan godkännas")
        note = row["note"] or row["quote"]
        observation_id = _insert_map_observation(
            conn,
            person=row["person"],
            place_name=row["place_name"] or row["raw_place"],
            lat=row["lat"],
            lon=row["lon"],
            time=row["time"],
            uncertainty=row["uncertainty"],
            nr=row["nr"],
            sida=row["sida"],
            note=note,
        )
        stamp = now()
        cur = conn.execute(
            """
            UPDATE map_observation_candidates
            SET status='approved', map_observation_id=?, updated_at=?, reviewed_at=?
            WHERE id=? AND status='pending'
            """,
            (observation_id, stamp, stamp, candidate_id),
        )
        if cur.rowcount != 1:
            raise ValueError("kandidaten saknas eller är redan granskad")
    return observation_id
