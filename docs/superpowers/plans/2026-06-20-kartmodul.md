# Kartmodul Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bygg en ny Streamlit-flik, Karta, som visar och redigerar källhänvisade observationer på en tidsanimerad karta över mordkvällen.

**Architecture:** Lägg all databasåtkomst i `src/db.py`, all kart-/GeoJSON-logik i en Streamlit-fri `src/karta.py`, och håll `src/pages/7_Karta.py` som en tunn UI-yta. Seed-data ligger i incheckade JSON-filer under `data/karta/`, men rörelseobservationer börjar tomma tills verifierade källrader finns eftersom kartan inte ska påstå positioner utan källa.

**Tech Stack:** SQLite via `src/db.py`, Streamlit multipage, `folium`, `streamlit-folium`, `folium.plugins.TimestampedGeoJson`, pytest.

## Global Constraints

- Alla kommentarer och docstrings ska vara på svenska.
- Konsumenter skriver aldrig egen SQL; all CRUD går via `src/db.py`.
- Observationer ska ha verifierbar källa (`nr` + `sida`) för att visas i tidslinjen.
- Tid lagras som `HH:MM` och tolkas mot basdatumet `1986-02-28`.
- OSM-kartrutor kräver internet; sidan ska degradera utan att krascha.
- Uppdatera `README.md`, `docs/kom-igang.md`, `docs/teknisk-referens.md`, `AGENTS.md` och `CLAUDE.md` när funktionen ändrar användaryta, kommandon, filöversikt eller state-db.
- Kör inte `/cap`, `git commit` eller `git push`; användaren gör det själv när hen är redo.

---

## File Structure

- Create `src/karta.py`: ren logik för validering, färgsättning, popup-HTML, sortering och TimestampedGeoJson-GeoJSON.
- Create `src/pages/7_Karta.py`: Streamlit-sida med karta, personfilter, platskatalog, observationstabell, redigeringsform och källkort.
- Create `data/karta/platser.json`: platskatalog för snabbval på kartan.
- Create `data/karta/rorelser.json`: seed-fil för observationer; börja med `[]` för att undvika osourcade påståenden.
- Create `data/karta/README.md`: format, källkrav och exempelrad för senare manuell curation.
- Create `tests/test_karta.py`: enhetstester för `src/karta.py`.
- Modify `src/db.py`: schema, schema-version och CRUD/seed-helper för karttabellerna.
- Modify `tests/test_db.py`: importera och testa kart-CRUD + idempotent seed.
- Modify `pyproject.toml`: lägg `karta` i `py-modules`, lägg `folium` och `streamlit-folium` i `[project.optional-dependencies].web`.
- Modify docs: `README.md`, `docs/kom-igang.md`, `docs/teknisk-referens.md`, `AGENTS.md`, `CLAUDE.md`.

---

### Task 1: Datamodell, CRUD och web dependencies

**Files:**
- Modify: `src/db.py`
- Modify: `tests/test_db.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: befintliga `connect()`, `init_schema()`, `now()` och `sqlite3.Row`-mönster i `src/db.py`.
- Produces:
  - `seed_map_data_if_empty(conn, places: list[dict], observations: list[dict]) -> int`
  - `list_map_places(conn) -> list[dict]`
  - `record_map_place(conn, *, name: str, lat: float, lon: float) -> int`
  - `delete_map_place(conn, place_id: int) -> bool`
  - `list_map_observations(conn, *, person: str | None = None) -> list[dict]`
  - `record_map_observation(conn, *, person: str, place_name: str | None, lat: float, lon: float, time: str | None = None, uncertainty: str | None = None, nr: str | None = None, sida: int | None = None, note: str | None = None) -> int`
  - `update_map_observation(conn, obs_id: int, **fields) -> bool`
  - `delete_map_observation(conn, obs_id: int) -> bool`

- [ ] **Step 1: Write failing DB tests**

Append these imports in `tests/test_db.py`:

```python
from db import (
    connect, init_schema, SCHEMA_VERSION,
    record_download, is_downloaded, find_download_by_sha1,
    upsert_pdf_file, get_pdf_file, mark_redaction_checked,
    mark_merged, mark_normalized, redaction_checked,
    record_page, page_exists, get_pages_for_stem,
    record_quality, record_quality_page, get_bad_pages,
    record_ingest, get_ingested_mtime,
    mark_llm_corrected, llm_corrected,
    mark_wpu_decided, wpu_decided,
    mark_tesseract_failed, mark_tesseract_blacklisted, retry_tesseract_blacklisted,
    clear_tesseract_blacklisted,
    is_tesseract_blacklisted,
    record_casebook_entry, list_casebook_entries, delete_casebook_entry,
    record_source_bookmark, list_source_bookmarks, delete_source_bookmark,
    record_source_annotation, update_source_annotation,
    list_source_annotations, delete_source_annotation,
    record_map_place, list_map_places, delete_map_place,
    record_map_observation, list_map_observations,
    update_map_observation, delete_map_observation,
    seed_map_data_if_empty,
    files_needing_normalize, files_needing_quality, files_needing_ingest,
)
```

Append these tests after `test_doc_entities_roundtrip`:

```python
def test_map_places_crud(tmp_path):
    conn = _fresh(tmp_path)
    place_id = record_map_place(
        conn,
        name="Dekorima",
        lat=59.33695,
        lon=18.06324,
    )

    places = list_map_places(conn)

    assert places == [{
        "id": place_id,
        "name": "Dekorima",
        "lat": 59.33695,
        "lon": 18.06324,
        "created_at": places[0]["created_at"],
    }]
    assert places[0]["created_at"]
    assert delete_map_place(conn, place_id) is True
    assert delete_map_place(conn, place_id) is False
    assert list_map_places(conn) == []


def test_map_place_requires_name_and_coordinates(tmp_path):
    conn = _fresh(tmp_path)
    with pytest.raises(ValueError):
        record_map_place(conn, name=" ", lat=59.0, lon=18.0)
    with pytest.raises(ValueError):
        record_map_place(conn, name="Grand", lat=120.0, lon=18.0)
    with pytest.raises(ValueError):
        record_map_place(conn, name="Grand", lat=59.0, lon=220.0)


def test_map_observations_crud_and_person_filter(tmp_path):
    conn = _fresh(tmp_path)
    first = record_map_observation(
        conn,
        person="Olof Palme",
        place_name="Grand",
        lat=59.34057,
        lon=18.06024,
        time="21:15",
        uncertainty="ca",
        nr="2055",
        sida=1,
        note="Biobesök",
    )
    second = record_map_observation(
        conn,
        person="Lisbeth Palme",
        place_name="Grand",
        lat=59.34057,
        lon=18.06024,
        time="21:15",
        nr="2055",
        sida=1,
    )

    assert [r["id"] for r in list_map_observations(conn)] == [second, first]
    palme = list_map_observations(conn, person="Olof Palme")
    assert len(palme) == 1
    assert palme[0]["person"] == "Olof Palme"
    assert palme[0]["place_name"] == "Grand"
    assert palme[0]["sida"] == 1

    assert update_map_observation(
        conn,
        first,
        place_name="Dekorima",
        lat=59.33695,
        lon=18.06324,
        time="23:21",
        note="Uppdaterad",
    ) is True
    updated = list_map_observations(conn, person="Olof Palme")[0]
    assert updated["place_name"] == "Dekorima"
    assert updated["time"] == "23:21"
    assert updated["note"] == "Uppdaterad"
    assert updated["updated_at"] >= updated["created_at"]

    assert update_map_observation(conn, 9999, place_name="Saknas") is False
    assert delete_map_observation(conn, second) is True
    assert delete_map_observation(conn, second) is False


def test_map_observation_requires_person_and_coordinates(tmp_path):
    conn = _fresh(tmp_path)
    with pytest.raises(ValueError):
        record_map_observation(conn, person=" ", place_name=None, lat=59.0, lon=18.0)
    with pytest.raises(ValueError):
        record_map_observation(conn, person="A", place_name=None, lat=-100.0, lon=18.0)
    with pytest.raises(ValueError):
        record_map_observation(conn, person="A", place_name=None, lat=59.0, lon=200.0)
    with pytest.raises(ValueError):
        update_map_observation(conn, 1, forbidden="x")


def test_seed_map_data_if_empty_is_idempotent(tmp_path):
    conn = _fresh(tmp_path)
    places = [{"name": "Dekorima", "lat": 59.33695, "lon": 18.06324}]
    observations = [{
        "person": "Olof Palme",
        "place_name": "Dekorima",
        "lat": 59.33695,
        "lon": 18.06324,
        "time": "23:21",
        "uncertainty": "ca",
        "nr": "1000",
        "sida": 1,
        "note": "Testkälla",
    }]

    assert seed_map_data_if_empty(conn, places, observations) == 2
    assert seed_map_data_if_empty(conn, places, observations) == 0
    assert len(list_map_places(conn)) == 1
    assert len(list_map_observations(conn)) == 1
```

- [ ] **Step 2: Run DB tests to verify they fail**

Run:

```bash
.venv/bin/pytest tests/test_db.py -q
```

Expected: FAIL with import errors for the new `record_map_*`, `list_map_*`, `seed_map_data_if_empty`, `update_map_observation` and `delete_map_observation` symbols.

- [ ] **Step 3: Add schema and helpers in `src/db.py`**

Set:

```python
SCHEMA_VERSION: int = 3
```

Add this SQL near the other user-facing state tables inside `SCHEMA_SQL`, after `source_annotations`:

```sql
CREATE TABLE IF NOT EXISTS map_places (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    lat        REAL NOT NULL,
    lon        REAL NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_map_places_name
    ON map_places(name COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS map_observations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    person      TEXT NOT NULL,
    place_name  TEXT,
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    time        TEXT,
    uncertainty TEXT,
    nr          TEXT,
    sida        INTEGER,
    note        TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_map_observations_person_time
    ON map_observations(person COLLATE NOCASE, time, id);
CREATE INDEX IF NOT EXISTS idx_map_observations_source
    ON map_observations(nr, sida);
```

Add this code near the casebook/bookmark section, before the delta-query section:

```python
# --- karta ------------------------------------------------------------

_MAP_OBSERVATION_FIELDS = {
    "person", "place_name", "lat", "lon", "time", "uncertainty", "nr", "sida", "note"
}


def _validate_lat_lon(lat: float, lon: float) -> tuple[float, float]:
    """Validera och normalisera koordinater till float."""
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


def _row_dict(row: sqlite3.Row) -> dict:
    """Konvertera sqlite-rad till vanlig dict för UI och tester."""
    return dict(row)


def list_map_places(conn: sqlite3.Connection) -> list[dict]:
    """Lista kartans platskatalog alfabetiskt."""
    rows = conn.execute(
        """
        SELECT id, name, lat, lon, created_at
        FROM map_places
        ORDER BY name COLLATE NOCASE, id
        """
    )
    return [_row_dict(row) for row in rows]


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
    return int(cur.lastrowid)


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
    clean_person = person.strip()
    if not clean_person:
        raise ValueError("person får inte vara tom")
    lat_f, lon_f = _validate_lat_lon(lat, lon)
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
            time.strip() if time and time.strip() else None,
            uncertainty.strip() if uncertainty and uncertainty.strip() else None,
            nr.strip() if nr and nr.strip() else None,
            int(sida) if sida is not None else None,
            note.strip() if note and note.strip() else None,
            stamp,
            stamp,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


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
    inserted = 0
    stamp = now()
    for place in places:
        name = str(place.get("name") or "").strip()
        if not name:
            raise ValueError("seed-plats saknar name")
        lat_f, lon_f = _validate_lat_lon(place.get("lat"), place.get("lon"))
        conn.execute(
            "INSERT INTO map_places(name, lat, lon, created_at) VALUES (?, ?, ?, ?)",
            (name, lat_f, lon_f, stamp),
        )
        inserted += 1
    for obs in observations:
        person = str(obs.get("person") or "").strip()
        if not person:
            raise ValueError("seed-observation saknar person")
        lat_f, lon_f = _validate_lat_lon(obs.get("lat"), obs.get("lon"))
        conn.execute(
            """
            INSERT INTO map_observations(
                person, place_name, lat, lon, time, uncertainty,
                nr, sida, note, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                person,
                str(obs.get("place_name") or "").strip() or None,
                lat_f,
                lon_f,
                str(obs.get("time") or "").strip() or None,
                str(obs.get("uncertainty") or "").strip() or None,
                str(obs.get("nr") or "").strip() or None,
                int(obs["sida"]) if obs.get("sida") is not None else None,
                str(obs.get("note") or "").strip() or None,
                stamp,
                stamp,
            ),
        )
        inserted += 1
    conn.commit()
    return inserted
```

- [ ] **Step 4: Update `pyproject.toml`**

In `[project.optional-dependencies].web`, change:

```toml
web = [
    "streamlit",
    "openai",
    "folium",
    "streamlit-folium",
]
```

- [ ] **Step 5: Run DB/package tests**

Run:

```bash
.venv/bin/pytest tests/test_db.py tests/test_packaging.py -q
```

Expected: PASS.

---

### Task 2: Ren kartlogik och GeoJSON

**Files:**
- Create: `src/karta.py`
- Create: `tests/test_karta.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: dictar från `db.list_map_observations()`.
- Produces:
  - `PERSON_PALETTE: list[str]`
  - `person_color(name: str) -> str`
  - `validate_observation(obs: dict) -> list[str]`
  - `observations_by_person(obs: list[dict]) -> dict[str, list[dict]]`
  - `popup_html(obs: dict) -> str`
  - `build_timestamped_geojson(obs: list[dict], *, base_date: str = "1986-02-28") -> dict`

- [ ] **Step 1: Write failing `tests/test_karta.py`**

Create `tests/test_karta.py`:

```python
from karta import (
    build_timestamped_geojson,
    observations_by_person,
    person_color,
    popup_html,
    validate_observation,
)


def _obs(**overrides):
    base = {
        "id": 1,
        "person": "Olof Palme",
        "place_name": "Grand",
        "lat": 59.34057,
        "lon": 18.06024,
        "time": "21:15",
        "uncertainty": "ca",
        "nr": "2055",
        "sida": 1,
        "note": "Biobesök",
    }
    base.update(overrides)
    return base


def test_person_color_is_stable_and_hex():
    assert person_color("Olof Palme") == person_color("Olof Palme")
    assert person_color("Olof Palme").startswith("#")
    assert len(person_color("Olof Palme")) == 7
    assert person_color("Olof Palme") != person_color("Stig Engström")


def test_validate_observation_accepts_complete_source():
    assert validate_observation(_obs()) == []


def test_validate_observation_reports_missing_required_fields():
    errors = validate_observation(_obs(person=" ", lat=None, nr="", sida=None))
    assert "person saknas" in errors
    assert "lat/lon saknas" in errors
    assert "källa saknas" in errors


def test_validate_observation_reports_bad_time_and_coordinates():
    errors = validate_observation(_obs(lat=120, lon=18, time="25:99"))
    assert "lat måste ligga mellan -90 och 90" in errors
    assert "time måste vara HH:MM" in errors


def test_observations_by_person_sorts_time_and_unknown_last():
    grouped = observations_by_person([
        _obs(id=2, person="Olof Palme", time="23:21"),
        _obs(id=1, person="Olof Palme", time="21:15"),
        _obs(id=3, person="Olof Palme", time=None),
        _obs(id=4, person="Lisbeth Palme", time="21:15"),
    ])
    assert list(grouped) == ["Lisbeth Palme", "Olof Palme"]
    assert [o["id"] for o in grouped["Olof Palme"]] == [1, 2, 3]


def test_popup_html_escapes_text_and_includes_source():
    html = popup_html(_obs(person="<Olof>", place_name="Grand & Svea"))
    assert "&lt;Olof&gt;" in html
    assert "Grand &amp; Svea" in html
    assert "[Nr 2055, sida 1]" in html
    assert "kl 21:15" in html


def test_build_timestamped_geojson_points_and_lines():
    data = build_timestamped_geojson([
        _obs(id=1, person="Olof Palme", time="21:15", lat=59.34057, lon=18.06024),
        _obs(id=2, person="Olof Palme", time="23:21", lat=59.33695, lon=18.06324),
        _obs(id=3, person="Stig Engström", time=None, lat=59.3374, lon=18.0632),
    ])

    features = data["features"]
    points = [f for f in features if f["geometry"]["type"] == "Point"]
    lines = [f for f in features if f["geometry"]["type"] == "LineString"]

    assert data["type"] == "FeatureCollection"
    assert len(points) == 2
    assert len(lines) == 1
    assert points[0]["geometry"]["coordinates"] == [18.06024, 59.34057]
    assert points[0]["properties"]["times"] == ["1986-02-28T21:15:00"]
    assert lines[0]["properties"]["times"] == [
        "1986-02-28T21:15:00",
        "1986-02-28T23:21:00",
    ]
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
.venv/bin/pytest tests/test_karta.py -q
```

Expected: FAIL because `karta` module does not exist.

- [ ] **Step 3: Create `src/karta.py` och uppdatera `pyproject.toml`**

Create this module:

```python
"""Ren kartlogik för Streamlit-sidan Karta.

Modulen känner inte till Streamlit eller folium. Den validerar observationer,
bygger popup-HTML och producerar GeoJSON som folium.plugins.TimestampedGeoJson
kan rendera.
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

_TIME_RE = re.compile(r"^\d{2}:\d{2}$")


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
    if not value:
        return True
    if not _TIME_RE.match(value):
        return False
    hour, minute = [int(part) for part in value.split(":")]
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


def _time_key(obs: dict) -> tuple[str, int]:
    time = str(obs.get("time") or "").strip()
    return (time or "99:99", int(obs.get("id") or 0))


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


def _iso_time(base_date: str, hhmm: str) -> str:
    stamp = datetime.fromisoformat(f"{base_date}T{hhmm}:00")
    return stamp.isoformat()


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
            "times": [_iso_time(base_date, str(obs["time"]))],
            "style": {"color": color},
            "icon": "circle",
            "iconstyle": {
                "fillColor": color,
                "fillOpacity": 0.9,
                "stroke": "true",
                "radius": 6,
            },
            "popup": popup_html(obs),
            "person": person,
        },
    }


def _line_feature(person: str, items: list[dict], base_date: str) -> dict:
    color = person_color(person)
    return {
        "type": "Feature",
        "geometry": {
            "type": "LineString",
            "coordinates": [[float(o["lon"]), float(o["lat"])] for o in items],
        },
        "properties": {
            "times": [_iso_time(base_date, str(o["time"])) for o in items],
            "style": {"color": color, "weight": 3, "opacity": 0.75},
            "popup": html.escape(person),
            "person": person,
        },
    }


def build_timestamped_geojson(
    obs: list[dict], *, base_date: str = "1986-02-28"
) -> dict:
    """Bygg FeatureCollection för foliums TimestampedGeoJson-plugin."""
    timed_valid: list[dict] = []
    for item in obs:
        if not item.get("time"):
            continue
        if validate_observation(item):
            continue
        timed_valid.append(item)

    features: list[dict] = []
    for item in sorted(timed_valid, key=_time_key):
        features.append(_point_feature(item, base_date))

    for person, items in observations_by_person(timed_valid).items():
        if len(items) >= 2:
            features.append(_line_feature(person, items, base_date))

    return {"type": "FeatureCollection", "features": features}
```

Samtidigt, i `[tool.setuptools].py-modules`, lägg till:

```toml
    "karta",
```

- [ ] **Step 4: Run map logic tests**

Run:

```bash
.venv/bin/pytest tests/test_karta.py -q
```

Expected: PASS.

- [ ] **Step 5: Run DB + map tests together**

Run:

```bash
.venv/bin/pytest tests/test_db.py tests/test_karta.py -q
```

Expected: PASS.

---

### Task 3: Seed files and source policy

**Files:**
- Create: `data/karta/README.md`
- Create: `data/karta/platser.json`
- Create: `data/karta/rorelser.json`
- Modify: no code files in this task

**Interfaces:**
- Consumes: `db.seed_map_data_if_empty(conn, places, observations)`.
- Produces: JSON arrays that `src/pages/7_Karta.py` can load with `json.loads`.

- [ ] **Step 1: Create `data/karta/README.md`**

Create:

```markdown
# Karta seed-data

`platser.json` är en platskatalog för snabbval i kartfliken.

`rorelser.json` är en lista med observationer. Lägg bara in observationer som har
verifierad källa i arkivet. Varje observation ska ha:

- `person`: visningsnamn
- `place_name`: platsnamn som visas i popupen
- `lat`, `lon`: koordinater i WGS84
- `time`: `HH:MM` under mordkvällen
- `uncertainty`: fri text, till exempel `ca` eller `±10 min`
- `nr`: arkivnummer
- `sida`: sida i källan
- `note`: kort neutral notering

Exempel:

```json
{
  "person": "Olof Palme",
  "place_name": "Grand",
  "lat": 59.34057,
  "lon": 18.06024,
  "time": "21:15",
  "uncertainty": "ca",
  "nr": "2055",
  "sida": 1,
  "note": "Exempelrad; ersätt med verifierad källrad innan den seedas."
}
```

Seedade observationer börjar tomma för att kartan inte ska påstå positioner utan
granskad källhänvisning. Lägg till riktiga observationer via UI:t eller genom en
granskad ändring av `rorelser.json`.
```

- [ ] **Step 2: Create `data/karta/platser.json`**

Create:

```json
[
  {
    "name": "Mordplatsen / Dekorima",
    "lat": 59.33695,
    "lon": 18.06324
  },
  {
    "name": "Tunnelgatan / trapporna",
    "lat": 59.33734,
    "lon": 18.06519
  },
  {
    "name": "Biografen Grand",
    "lat": 59.34057,
    "lon": 18.06024
  },
  {
    "name": "Skandiahuset",
    "lat": 59.33764,
    "lon": 18.06295
  },
  {
    "name": "Sveavägen / Adolf Fredriks kyrkogata",
    "lat": 59.33876,
    "lon": 18.06198
  },
  {
    "name": "Kungsgatan / Sveavägen",
    "lat": 59.33536,
    "lon": 18.06393
  },
  {
    "name": "Gamla stans tunnelbana",
    "lat": 59.32339,
    "lon": 18.06772
  },
  {
    "name": "Sabbatsbergs sjukhus",
    "lat": 59.33844,
    "lon": 18.04629
  }
]
```

- [ ] **Step 3: Create `data/karta/rorelser.json`**

Create:

```json
[]
```

- [ ] **Step 4: Verify JSON parses**

Run:

```bash
python3 -m json.tool data/karta/platser.json >/tmp/platser.json
python3 -m json.tool data/karta/rorelser.json >/tmp/rorelser.json
```

Expected: both commands exit with code 0.

---

### Task 4: Streamlit Karta page

**Files:**
- Create: `src/pages/7_Karta.py`

**Interfaces:**
- Consumes:
  - `casebook_ui.state_conn()`
  - `casebook_ui.render_source_cards(root, sources, conn, key_prefix=...)`
  - `db.seed_map_data_if_empty`, `db.list_map_places`, `db.list_map_observations`, `db.record_map_place`, `db.record_map_observation`, `db.update_map_observation`, `db.delete_map_observation`
  - `karta.build_timestamped_geojson`, `karta.person_color`, `karta.validate_observation`
- Produces: a new Streamlit page `Karta` visible in multipage navigation.

- [ ] **Step 1: Create page imports and dependency fallback**

Create `src/pages/7_Karta.py` with this top section:

```python
"""Streamlit-sida: karta över källhänvisade observationer mordkvällen."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import streamlit as st

try:
    import folium
    from folium.plugins import TimestampedGeoJson
    from streamlit_folium import st_folium
except ImportError:  # pragma: no cover - optional web extra
    st.set_page_config(page_title="Palmemordsarkivet — Karta", layout="wide")
    st.title("Karta")
    st.warning("Installera kartstödet med `pip install -e .[web]`.")
    st.stop()

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import casebook_ui as _casebook_ui  # noqa: E402
import db as _state_db  # noqa: E402
import karta as _karta  # noqa: E402
```

- [ ] **Step 2: Add seed loading and source conversion helpers**

Append:

```python
SEED_DIR = ROOT / "data" / "karta"
DEFAULT_CENTER = [59.33695, 18.06324]


@st.cache_data(show_spinner=False)
def _load_seed_file(name: str) -> list[dict]:
    path = SEED_DIR / name
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else []


def _seed_if_needed(conn) -> int:
    return _state_db.seed_map_data_if_empty(
        conn,
        _load_seed_file("platser.json"),
        _load_seed_file("rorelser.json"),
    )


def _source_for_observation(obs: dict) -> dict | None:
    nr = str(obs.get("nr") or "").strip()
    if not nr:
        return None
    title = str(obs.get("place_name") or obs.get("person") or nr).strip()
    return {
        "source": f"{nr}.txt",
        "page": obs.get("sida"),
        "nr": nr,
        "title": title,
    }
```

- [ ] **Step 3: Add map-building helpers**

Append:

```python
def _add_place_markers(map_obj: folium.Map, places: list[dict]) -> None:
    for place in places:
        folium.CircleMarker(
            location=[place["lat"], place["lon"]],
            radius=4,
            color="#555555",
            fill=True,
            fill_color="#555555",
            fill_opacity=0.55,
            tooltip=place["name"],
        ).add_to(map_obj)


def _build_map(observations: list[dict], places: list[dict]) -> folium.Map:
    m = folium.Map(location=DEFAULT_CENTER, zoom_start=16, tiles="OpenStreetMap")
    _add_place_markers(m, places)
    invalid = [o for o in observations if _karta.validate_observation(o)]
    if invalid:
        st.warning(
            f"{len(invalid)} observationer saknar giltig tid, koordinat eller källa "
            "och visas inte i tidslinjen."
        )
    geojson = _karta.build_timestamped_geojson(observations)
    if geojson["features"]:
        TimestampedGeoJson(
            geojson,
            period="PT1M",
            add_last_point=True,
            auto_play=False,
            loop=False,
            max_speed=10,
            loop_button=True,
            date_options="HH:mm",
            time_slider_drag_update=True,
        ).add_to(m)
    return m
```

- [ ] **Step 4: Add page state and filters**

Append:

```python
st.set_page_config(page_title="Palmemordsarkivet — Karta", layout="wide")
st.title("Karta")
st.caption("Källhänvisade observationer och rörelser runt mordkvällen 28 februari 1986.")

conn = _casebook_ui.state_conn()
seeded = _seed_if_needed(conn)
if seeded:
    st.toast(f"Seedade {seeded} kartposter")

places = _state_db.list_map_places(conn)
observations = _state_db.list_map_observations(conn)
people = sorted({o["person"] for o in observations})

with st.sidebar:
    st.header("Filter")
    selected_people = st.multiselect("Personer", people, default=people)
    show_places = st.toggle("Visa platskatalog", value=True)

filtered = [
    o for o in observations
    if not selected_people or o["person"] in selected_people
]
map_places = places if show_places else []
```

- [ ] **Step 5: Render map and remember latest click**

Append:

```python
left, right = st.columns([1, 3])
with left:
    st.subheader("Personer")
    if people:
        for person in people:
            color = _karta.person_color(person)
            marker = "■"
            st.markdown(f"<span style='color:{color}'>{marker}</span> {person}", unsafe_allow_html=True)
    else:
        st.info("Inga observationer ännu. Lägg till en källhänvisad observation nedan.")
    st.caption(f"{len(filtered)} visade observationer")

with right:
    result = st_folium(
        _build_map(filtered, map_places),
        height=650,
        use_container_width=True,
        key="karta_folium",
    )
    clicked = result.get("last_clicked") if result else None
    if clicked:
        st.session_state["karta_last_clicked"] = clicked
        st.caption(
            f"Senaste kartklick: {clicked['lat']:.5f}, {clicked['lng']:.5f}"
        )
```

- [ ] **Step 6: Add observation table and edit form**

Append:

```python
st.subheader("Redigera observationer")

if observations:
    labels = [
        f"{o['id']} · {o['person']} · {o.get('time') or 'utan tid'} · "
        f"{o.get('place_name') or 'okänd plats'}"
        for o in observations
    ]
    selected_index = st.selectbox(
        "Observation",
        range(len(observations)),
        format_func=lambda i: labels[i],
    )
    selected = observations[selected_index]
else:
    selected = None
    st.info("Databasen har inga observationer ännu.")

place_names = ["(fritt)"] + [p["name"] for p in places]
place_lookup = {p["name"]: p for p in places}
default_place = selected.get("place_name") if selected else ""
default_place_choice = default_place if default_place in place_lookup else "(fritt)"

with st.form("map_observation_form", clear_on_submit=False):
    cols = st.columns(3)
    with cols[0]:
        person = st.text_input("Person", value=selected.get("person", "") if selected else "")
        place_choice = st.selectbox(
            "Plats ur katalog",
            place_names,
            index=place_names.index(default_place_choice),
        )
        place_name = st.text_input(
            "Platsnamn",
            value=default_place if selected else "",
        )
    with cols[1]:
        lat_default = float(selected["lat"]) if selected else DEFAULT_CENTER[0]
        lon_default = float(selected["lon"]) if selected else DEFAULT_CENTER[1]
        click = st.session_state.get("karta_last_clicked")
        use_click = st.checkbox("Använd senaste kartklick som koordinat", value=False)
        if use_click and click:
            lat_default = float(click["lat"])
            lon_default = float(click["lng"])
        picked = place_lookup.get(place_choice)
        if picked and not use_click:
            lat_default = float(picked["lat"])
            lon_default = float(picked["lon"])
            place_name = picked["name"]
        lat = st.number_input("Lat", value=lat_default, format="%.6f")
        lon = st.number_input("Lon", value=lon_default, format="%.6f")
    with cols[2]:
        time = st.text_input("Tid (HH:MM)", value=selected.get("time") or "" if selected else "")
        uncertainty = st.text_input(
            "Osäkerhet",
            value=selected.get("uncertainty") or "" if selected else "",
        )
        nr = st.text_input("Nr", value=selected.get("nr") or "" if selected else "")
        sida_value = int(selected["sida"]) if selected and selected.get("sida") else 1
        sida = st.number_input("Sida", min_value=1, value=sida_value, step=1)
    note = st.text_area("Notering", value=selected.get("note") or "" if selected else "")

    save, new, delete = st.columns(3)
    save_clicked = save.form_submit_button("Spara")
    new_clicked = new.form_submit_button("Lägg till ny")
    delete_clicked = delete.form_submit_button("Ta bort vald")

payload = {
    "person": person,
    "place_name": place_name,
    "lat": lat,
    "lon": lon,
    "time": time,
    "uncertainty": uncertainty,
    "nr": nr,
    "sida": int(sida),
    "note": note,
}
errors = _karta.validate_observation(payload)
if (save_clicked or new_clicked) and errors:
    st.error("Kan inte spara: " + ", ".join(errors))
elif save_clicked and selected:
    _state_db.update_map_observation(conn, selected["id"], **payload)
    st.toast("Observation uppdaterad")
    st.rerun()
elif new_clicked:
    _state_db.record_map_observation(conn, **payload)
    st.toast("Observation tillagd")
    st.rerun()
elif delete_clicked and selected:
    _state_db.delete_map_observation(conn, selected["id"])
    st.toast("Observation borttagen")
    st.rerun()
```

- [ ] **Step 7: Add selected source card**

Append:

```python
if selected:
    source = _source_for_observation(selected)
    if source:
        st.subheader("Källa för vald observation")
        _casebook_ui.render_source_cards(
            ROOT,
            [source],
            conn,
            key_prefix=f"karta_source_{selected['id']}",
        )
```

- [ ] **Step 8: Compile Streamlit page**

Run:

```bash
python3 -m compileall src
```

Expected: all files compile; Streamlit/optional dependency warnings are acceptable only if they come from imports at runtime, not syntax.

---

### Task 5: Documentation and verification

**Files:**
- Modify: `README.md`
- Modify: `docs/kom-igang.md`
- Modify: `docs/teknisk-referens.md`
- Modify: `AGENTS.md`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: all implemented files from Tasks 1-4.
- Produces: user-facing and agent-facing docs that mention the Karta page, map tables, seed files and dependencies.

- [ ] **Step 1: Update `README.md`**

Make these concrete edits:

```markdown
Streamlit-gränssnittet innehåller flikarna **Utredning**, **Utredningspärm**,
**Graf**, **Maskeringar**, **Jämförelse** och **Karta**.
```

Add a short paragraph near the Graf/Maskeringar/Jämförelse descriptions:

```markdown
**Karta** visar källhänvisade observationer på en tidsanimerad karta över
mordkvällen. Platskatalog och observationer kan redigeras i appen; rörelser
visas bara när de har tid, koordinater och källa.
```

- [ ] **Step 2: Update `docs/kom-igang.md`**

Add a short section in the web UI area:

```markdown
### Karta

Karta-fliken finns i Streamlit-appen och visar observationer runt mordkvällen på
en folium-karta. Nya observationer ska ha person, plats/koordinat, tid och
källhänvisning (`Nr` + `sida`). Kartan seedar platskatalogen från
`data/karta/platser.json`; `data/karta/rorelser.json` börjar tom tills
verifierade observationer läggs till.
```

- [ ] **Step 3: Update `docs/teknisk-referens.md`**

Add these facts in the state-db/schema section:

```markdown
- `map_places` — platskatalog för Karta-flikens snabbval.
- `map_observations` — källhänvisade observationer med person, koordinat,
  tid (`HH:MM`), osäkerhet, `nr`, `sida` och notering.
```

Add a Karta subsection near the other Streamlit feature descriptions:

```markdown
### Karta

`src/pages/7_Karta.py` renderar en folium-karta via `streamlit-folium`.
`src/karta.py` bygger `TimestampedGeoJson` från observationer i state.db.
Observationer utan giltig tid, koordinat eller källa visas inte i den animerade
tidslinjen, men kan redigeras i formuläret. Seed-data ligger i
`data/karta/platser.json` och `data/karta/rorelser.json`; seed körs bara när
karttabellerna är tomma.
```

Add `folium` and `streamlit-folium` to the web dependency description.

- [ ] **Step 4: Update `AGENTS.md` and `CLAUDE.md`**

Add `src/karta.py` and `src/pages/7_Karta.py` to the directory structure.
Add `data/karta/` to the data-file overview.
Add a non-obvious design decision:

```markdown
**Kartmodulen (`src/karta.py` + `src/pages/7_Karta.py`)**: Observationer lever i
state.db (`map_places`, `map_observations`) och seedas från `data/karta/*.json`
bara om karttabellerna är tomma. `src/karta.py` är Streamlit-fri och bygger
validerad TimestampedGeoJson; UI:t visar inte observationer i tidslinjen utan
tid, koordinat och källa.
```

Add a gotcha:

```markdown
**Kartor kräver nät för bakgrundsrutor**: folium-markörer och spår renderas även
offline, men OSM-bakgrunden kräver internet.
```

- [ ] **Step 5: Run focused verification**

Run:

```bash
.venv/bin/pytest tests/test_db.py tests/test_karta.py tests/test_packaging.py -q
python3 -m compileall src
```

Expected: pytest PASS; compileall reports successful compilation for changed modules.

- [ ] **Step 6: Optional manual smoke test**

Run:

```bash
./web.sh
```

Expected: Streamlit starts. Open the Karta page, verify that:

- The page loads without optional dependency warnings when `[web]` deps are installed.
- Place markers appear around Sveavägen.
- Empty observations show the "Inga observationer ännu" message.
- Adding a sourced observation with `Nr`, `sida`, `HH:MM`, lat/lon makes it appear in the map timeline.
- The selected observation renders a source card.

Do not run `git commit` or `git push`.

---

## Self-Review

- Spec coverage: DB schema/CRUD, seed files, pure map module, Streamlit page, dependencies, error handling, docs and tests are all represented. The only deliberate scope adjustment is that `rorelser.json` begins empty until source-reviewed observations exist; this preserves the spec's rule that no position should appear without a verifiable reference.
- Placeholder scan: inga förbjudna platshållarord eller ospecificerade teststeg är kvar.
- Type consistency: DB functions return plain `dict` lists consumed by `src/karta.py` and `src/pages/7_Karta.py`; observation field names match across DB, JSON, tests and UI.
