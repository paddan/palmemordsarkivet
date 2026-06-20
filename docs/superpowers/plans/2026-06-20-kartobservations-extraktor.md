# Kartobservations-extraktor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bygg en granskningsbar extractor som hittar källbelagda person-positioner i arkivtexten och låter användaren godkänna dem till Karta-flikens `map_observations`.

**Architecture:** Extraktorn ska aldrig skriva direkt till kartans publicerade observationer. Den läser `generated/text/*.txt` sida för sida, ber en LLM extrahera tidsatta observationer, normaliserar plats/tid/källa till kandidater i en ny SQLite-tabell, och exponerar kandidaterna i Karta-fliken för godkännande eller avslag. Ren parsing, platsmatchning och kandidatvalidering ligger i en Streamlit-fri modul; LLM-körningen ligger i ett CLI-script; UI:t anropar bara DB-funktioner.

**Tech Stack:** SQLite via `src/db.py`, Claude Agent SDK / OpenAI-kompatibel klient enligt `src/graph/extract_entities.py`, Streamlit, befintliga källkort i `casebook_ui`, `pytest`.

## Global Constraints

- Alla kommentarer och docstrings ska vara på svenska.
- Extraktorn får bara skapa kandidater; publicerade kartobservationer skapas först efter manuell granskning.
- Varje kandidat ska bära källa: `pdf_stem`, `nr`, `sida` och ett kort citatutdrag.
- Karta-tidslinjen använder fortsatt bara observationer med person, koordinat, tid och källa.
- Tid normaliseras till `HH:MM` när det går; osäkerhet sparas separat i `uncertainty`.
- Platsmatchning ska vara transparent: exakt/fuzzy match mot platskatalogen markeras, okända platser kräver manuell koordinat innan approve.
- OCR-fel och osäkra formuleringar ska bevaras som kandidater med låg/medium confidence, inte tyst slängas om de har person, plats och källa.
- Uppdatera `README.md`, `docs/kom-igang.md`, `docs/teknisk-referens.md`, `AGENTS.md` och `CLAUDE.md` när nya kommandon, tabeller, filer eller UI-flöden tillkommer.
- Kör inte `/cap`, `git commit` eller `git push`; användaren gör det själv när hen är redo.

---

## File Structure

- Create `src/map_extract.py`: ren logik för LLM-JSON-parsning, tidsnormalisering, platsindex, platsmatchning och kandidatpayloads.
- Create `src/extract_map_observations.py`: CLI som läser textfiler, väljer sidor, anropar LLM, sparar kandidater idempotent.
- Create `extract_map_observations.sh`: wrapper som aktiverar `.venv` och vidarebefordrar flaggor.
- Modify `src/db.py`: ny tabell `map_observation_candidates` och CRUD/approve/reject-funktioner.
- Modify `src/pages/7_Karta.py`: review-yta för pending-kandidater med källkort, approve och reject.
- Modify `pyproject.toml`: lägg `map_extract` och `extract_map_observations` i `py-modules`.
- Create `tests/test_map_extract.py`: ren logik.
- Create `tests/test_extract_map_observations.py`: CLI-/LLM-flöde med monkeypatchad LLM.
- Modify `tests/test_db.py`: kandidat-CRUD och approve/reject.
- Modify docs: `README.md`, `docs/kom-igang.md`, `docs/teknisk-referens.md`, `AGENTS.md`, `CLAUDE.md`, `data/karta/README.md`.

---

### Task 1: Kandidat-tabell och DB-API

**Files:**
- Modify: `src/db.py`
- Modify: `tests/test_db.py`

**Interfaces:**
- Consumes:
  - `record_map_observation(conn, *, person: str, place_name: str | None, lat: float, lon: float, time: str | None, uncertainty: str | None, nr: str | None, sida: int | None, note: str | None) -> int`
  - `now() -> str`
- Produces:
  - `record_map_observation_candidate(conn, *, pdf_stem: str, page_num: int, person: str, raw_place: str, place_name: str | None, lat: float | None, lon: float | None, time: str | None, uncertainty: str | None, nr: str, sida: int, quote: str, note: str | None, confidence: str, place_match: str, model: str) -> int`
  - `list_map_observation_candidates(conn, *, status: str = "pending", limit: int = 100) -> list[dict]`
  - `update_map_observation_candidate(conn, candidate_id: int, **fields) -> bool`
  - `reject_map_observation_candidate(conn, candidate_id: int) -> bool`
  - `approve_map_observation_candidate(conn, candidate_id: int) -> int`

- [ ] **Step 1: Write failing DB tests**

Append these imports to the existing import block in `tests/test_db.py`:

```python
from db import (
    record_map_observation_candidate,
    list_map_observation_candidates,
    update_map_observation_candidate,
    reject_map_observation_candidate,
    approve_map_observation_candidate,
)
```

Append these tests near the existing map tests:

```python
def test_map_observation_candidates_roundtrip(tmp_path):
    conn = _fresh(tmp_path)

    candidate_id = record_map_observation_candidate(
        conn,
        pdf_stem="2055 — Grandbesökare",
        page_num=3,
        person="Olof Palme",
        raw_place="Grand",
        place_name="Biografen Grand",
        lat=59.34057,
        lon=18.06024,
        time="21:15",
        uncertainty="ca",
        nr="2055",
        sida=3,
        quote="Olof Palme sågs vid Grand omkring 21.15.",
        note="Sågs vid platsen enligt texten.",
        confidence="high",
        place_match="exact",
        model="test-model",
    )

    rows = list_map_observation_candidates(conn)

    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == candidate_id
    assert row["status"] == "pending"
    assert row["person"] == "Olof Palme"
    assert row["place_name"] == "Biografen Grand"
    assert row["time"] == "21:15"
    assert row["quote"].startswith("Olof Palme")
    assert row["created_at"]
    assert row["updated_at"]


def test_map_observation_candidate_upsert_is_idempotent(tmp_path):
    conn = _fresh(tmp_path)
    kwargs = dict(
        pdf_stem="2055 — Grandbesökare",
        page_num=3,
        person="Olof Palme",
        raw_place="Grand",
        place_name="Biografen Grand",
        lat=59.34057,
        lon=18.06024,
        time="21:15",
        uncertainty="ca",
        nr="2055",
        sida=3,
        quote="Olof Palme sågs vid Grand omkring 21.15.",
        note="Första texten.",
        confidence="medium",
        place_match="exact",
        model="test-model",
    )

    first = record_map_observation_candidate(conn, **kwargs)
    second = record_map_observation_candidate(conn, **{**kwargs, "note": "Uppdaterad text."})

    rows = list_map_observation_candidates(conn)
    assert first == second
    assert len(rows) == 1
    assert rows[0]["note"] == "Uppdaterad text."


def test_map_observation_candidate_requires_core_fields(tmp_path):
    conn = _fresh(tmp_path)
    base = dict(
        pdf_stem="doc",
        page_num=1,
        person="Olof Palme",
        raw_place="Grand",
        place_name=None,
        lat=None,
        lon=None,
        time=None,
        uncertainty=None,
        nr="2055",
        sida=1,
        quote="Kort citat.",
        note=None,
        confidence="low",
        place_match="none",
        model="test-model",
    )
    for field in ("pdf_stem", "person", "raw_place", "nr", "quote", "model"):
        with pytest.raises(ValueError):
            record_map_observation_candidate(conn, **{**base, field: " "})
    with pytest.raises(ValueError):
        record_map_observation_candidate(conn, **{**base, "confidence": "maybe"})
    with pytest.raises(ValueError):
        record_map_observation_candidate(conn, **{**base, "place_match": "magic"})
    with pytest.raises(ValueError):
        record_map_observation_candidate(conn, **{**base, "lat": 59.0, "lon": None})


def test_update_reject_and_approve_map_observation_candidate(tmp_path):
    conn = _fresh(tmp_path)
    candidate_id = record_map_observation_candidate(
        conn,
        pdf_stem="2055 — Grandbesökare",
        page_num=3,
        person="Olof Palme",
        raw_place="Grand",
        place_name=None,
        lat=None,
        lon=None,
        time=None,
        uncertainty="ca",
        nr="2055",
        sida=3,
        quote="Olof Palme sågs vid Grand.",
        note=None,
        confidence="medium",
        place_match="none",
        model="test-model",
    )

    assert update_map_observation_candidate(
        conn,
        candidate_id,
        place_name="Biografen Grand",
        lat=59.34057,
        lon=18.06024,
        time="21:15",
        note="Godkänd rad.",
    ) is True

    observation_id = approve_map_observation_candidate(conn, candidate_id)
    approved = list_map_observation_candidates(conn, status="approved")
    observations = list_map_observations(conn, person="Olof Palme")

    assert approved[0]["map_observation_id"] == observation_id
    assert observations[0]["person"] == "Olof Palme"
    assert observations[0]["place_name"] == "Biografen Grand"
    assert observations[0]["time"] == "21:15"
    assert observations[0]["nr"] == "2055"
    assert observations[0]["sida"] == 3
    assert "Godkänd rad." in observations[0]["note"]

    reject_id = record_map_observation_candidate(
        conn,
        pdf_stem="doc",
        page_num=4,
        person="Lisbeth Palme",
        raw_place="okänd plats",
        place_name=None,
        lat=None,
        lon=None,
        time=None,
        uncertainty=None,
        nr="2055",
        sida=4,
        quote="Oklar rad.",
        note=None,
        confidence="low",
        place_match="none",
        model="test-model",
    )
    assert reject_map_observation_candidate(conn, reject_id) is True
    assert list_map_observation_candidates(conn, status="rejected")[0]["id"] == reject_id


def test_approve_candidate_requires_time_source_and_coordinates(tmp_path):
    conn = _fresh(tmp_path)
    candidate_id = record_map_observation_candidate(
        conn,
        pdf_stem="doc",
        page_num=1,
        person="Olof Palme",
        raw_place="Grand",
        place_name="Biografen Grand",
        lat=59.34057,
        lon=18.06024,
        time=None,
        uncertainty=None,
        nr="2055",
        sida=1,
        quote="Olof Palme vid Grand.",
        note=None,
        confidence="medium",
        place_match="exact",
        model="test-model",
    )

    with pytest.raises(ValueError, match="time"):
        approve_map_observation_candidate(conn, candidate_id)
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/pytest tests/test_db.py -q -k "map_observation_candidate"
```

Expected: FAIL with import errors for the new candidate functions.

- [ ] **Step 3: Add schema in `src/db.py`**

Increment `SCHEMA_VERSION` by 1 and add this table/index block inside `SCHEMA_SQL` after `map_observations`:

```sql
CREATE TABLE IF NOT EXISTS map_observation_candidates (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    pdf_stem           TEXT NOT NULL,
    page_num           INTEGER NOT NULL,
    person             TEXT NOT NULL,
    raw_place          TEXT NOT NULL,
    place_name         TEXT,
    lat                REAL,
    lon                REAL,
    time               TEXT,
    uncertainty        TEXT,
    nr                 TEXT NOT NULL,
    sida               INTEGER NOT NULL,
    quote              TEXT NOT NULL,
    note               TEXT,
    confidence         TEXT NOT NULL CHECK(confidence IN ('low', 'medium', 'high')),
    place_match        TEXT NOT NULL CHECK(place_match IN ('none', 'fuzzy', 'exact')),
    status             TEXT NOT NULL DEFAULT 'pending'
                           CHECK(status IN ('pending', 'approved', 'rejected')),
    model              TEXT NOT NULL,
    map_observation_id INTEGER REFERENCES map_observations(id) ON DELETE SET NULL,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    reviewed_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_map_observation_candidates_status
    ON map_observation_candidates(status, updated_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_map_observation_candidates_source
    ON map_observation_candidates(pdf_stem, page_num);
CREATE UNIQUE INDEX IF NOT EXISTS idx_map_observation_candidates_unique
    ON map_observation_candidates(
        pdf_stem,
        page_num,
        person COLLATE NOCASE,
        raw_place COLLATE NOCASE,
        COALESCE(time, ''),
        quote
    );
```

- [ ] **Step 4: Add DB helpers**

Add this helper section near the existing map helpers in `src/db.py`:

```python
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
```

Add the public functions:

```python
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
          AND person=?
          AND raw_place=?
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
    return int(cur.lastrowid)


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
    observation_id = record_map_observation(
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
    conn.execute(
        """
        UPDATE map_observation_candidates
        SET status='approved', map_observation_id=?, updated_at=?, reviewed_at=?
        WHERE id=?
        """,
        (observation_id, stamp, stamp, candidate_id),
    )
    conn.commit()
    return observation_id
```

- [ ] **Step 5: Run DB tests**

Run:

```bash
.venv/bin/pytest tests/test_db.py -q -k "map_observation_candidate or map_observations"
```

Expected: PASS.

---

### Task 2: Ren kartobservations-parsning och platsmatchning

**Files:**
- Create: `src/map_extract.py`
- Create: `tests/test_map_extract.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes:
  - `data/karta/platser.json` format: `{"name": str, "lat": float, "lon": float}`
- Produces:
  - `parse_map_observation_extraction(raw: str) -> list[dict]`
  - `normalize_observation_time(value: str | None) -> tuple[str | None, str | None]`
  - `build_place_index(places: list[dict]) -> list[dict]`
  - `match_place(raw_place: str, place_index: list[dict]) -> dict`
  - `candidate_payload(obs: dict, *, pdf_stem: str, page_num: int, nr: str, model: str, place_index: list[dict]) -> dict | None`

- [ ] **Step 1: Write failing pure tests**

Create `tests/test_map_extract.py`:

```python
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from map_extract import (  # noqa: E402
    build_place_index,
    candidate_payload,
    match_place,
    normalize_observation_time,
    parse_map_observation_extraction,
)


def _places():
    return build_place_index([
        {"name": "Biografen Grand", "lat": 59.34057, "lon": 18.06024},
        {"name": "Mordplatsen / Dekorima", "lat": 59.33695, "lon": 18.06324},
    ])


def test_parse_map_observation_extraction_accepts_json_object():
    raw = """
    {"observationer": [{
      "person": "Olof Palme",
      "plats": "Grand",
      "tid": "ca 21.15",
      "citat": "Olof Palme kom till Grand omkring kl 21.15.",
      "notering": "ankomst",
      "confidence": "high"
    }]}
    """

    observations = parse_map_observation_extraction(raw)

    assert observations == [{
        "person": "Olof Palme",
        "plats": "Grand",
        "tid": "ca 21.15",
        "citat": "Olof Palme kom till Grand omkring kl 21.15.",
        "notering": "ankomst",
        "confidence": "high",
    }]


def test_parse_map_observation_extraction_filters_incomplete_rows():
    raw = """
    ```json
    {"observationer": [
      {"person": "Olof Palme", "plats": "Grand", "tid": "21:15", "citat": "rad", "confidence": "medium"},
      {"person": "", "plats": "Grand", "tid": "21:15", "citat": "rad", "confidence": "medium"},
      {"person": "Olof Palme", "plats": "", "tid": "21:15", "citat": "rad", "confidence": "medium"},
      {"person": "Olof Palme", "plats": "Grand", "tid": "21:15", "citat": "", "confidence": "medium"}
    ]}
    ```
    """

    observations = parse_map_observation_extraction(raw)

    assert len(observations) == 1
    assert observations[0]["person"] == "Olof Palme"


def test_normalize_observation_time_handles_common_forms():
    assert normalize_observation_time("21:15") == ("21:15", None)
    assert normalize_observation_time("21.15") == ("21:15", None)
    assert normalize_observation_time("ca 21.15") == ("21:15", "ca")
    assert normalize_observation_time("omkring kl 23 21") == ("23:21", "omkring")
    assert normalize_observation_time("efter midnatt") == (None, "efter midnatt")
    assert normalize_observation_time("") == (None, None)


def test_match_place_exact_and_fuzzy():
    places = _places()

    assert match_place("Biografen Grand", places) == {
        "place_name": "Biografen Grand",
        "lat": 59.34057,
        "lon": 18.06024,
        "place_match": "exact",
    }
    assert match_place("Grand", places)["place_name"] == "Biografen Grand"
    assert match_place("Dekorima", places)["place_name"] == "Mordplatsen / Dekorima"
    assert match_place("okänd gränd", places) == {
        "place_name": None,
        "lat": None,
        "lon": None,
        "place_match": "none",
    }


def test_candidate_payload_maps_source_place_and_time():
    payload = candidate_payload(
        {
            "person": "Olof Palme",
            "plats": "Grand",
            "tid": "ca 21.15",
            "citat": "Olof Palme kom till Grand omkring kl 21.15.",
            "notering": "ankomst",
            "confidence": "high",
        },
        pdf_stem="2055 — Grandbesökare",
        page_num=3,
        nr="2055",
        model="test-model",
        place_index=_places(),
    )

    assert payload == {
        "pdf_stem": "2055 — Grandbesökare",
        "page_num": 3,
        "person": "Olof Palme",
        "raw_place": "Grand",
        "place_name": "Biografen Grand",
        "lat": 59.34057,
        "lon": 18.06024,
        "time": "21:15",
        "uncertainty": "ca",
        "nr": "2055",
        "sida": 3,
        "quote": "Olof Palme kom till Grand omkring kl 21.15.",
        "note": "ankomst",
        "confidence": "high",
        "place_match": "fuzzy",
        "model": "test-model",
    }
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/pytest tests/test_map_extract.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'map_extract'`.

- [ ] **Step 3: Implement `src/map_extract.py`**

Create `src/map_extract.py`:

```python
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
```

- [ ] **Step 4: Add modules to packaging**

In `pyproject.toml`, add these entries to `[tool.setuptools].py-modules`:

```toml
    "map_extract",
    "extract_map_observations",
```

- [ ] **Step 5: Run pure tests**

Run:

```bash
.venv/bin/pytest tests/test_map_extract.py tests/test_packaging.py -q
```

Expected: PASS.

---

### Task 3: LLM extractor CLI och wrapper

**Files:**
- Create: `src/extract_map_observations.py`
- Create: `extract_map_observations.sh`
- Create: `tests/test_extract_map_observations.py`

**Interfaces:**
- Consumes:
  - `map_extract.parse_map_observation_extraction`
  - `map_extract.candidate_payload`
  - `db.record_map_observation_candidate`
  - provider/config pattern from `src/graph/extract_entities.py`
- Produces:
  - `select_candidate_pages(conn, pdf_stem: str, pages: list[str]) -> list[tuple[int, str]]`
  - `extract_doc(conn, txt_path: Path, *, cfg: dict, dry_run: bool, place_index: list[dict], timeout: float = 120.0, jobs: int = 1, on_page=None) -> int`
  - CLI flags: `--text-dir`, `--limit`, `--provider`, `--model`, `--base-url`, `--api-key`, `--dry-run`, `--jobs`, `--timeout`

- [ ] **Step 1: Write failing extractor tests**

Create `tests/test_extract_map_observations.py`:

```python
from pathlib import Path
import sys
import asyncio

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import db  # noqa: E402
import extract_map_observations as emo  # noqa: E402


def _conn(tmp_path):
    conn = db.connect(tmp_path / "state.db")
    db.init_schema(conn)
    return conn


def test_select_candidate_pages_skips_short_pages_and_existing_candidates(tmp_path):
    conn = _conn(tmp_path)
    text = "Olof Palme sågs vid Grand omkring klockan 21.15 enligt vittnet. " * 4
    db.record_map_observation_candidate(
        conn,
        pdf_stem="doc",
        page_num=3,
        person="Olof Palme",
        raw_place="Grand",
        place_name="Biografen Grand",
        lat=59.34057,
        lon=18.06024,
        time="21:15",
        uncertainty="ca",
        nr="doc",
        sida=3,
        quote="Olof Palme sågs vid Grand omkring klockan 21.15.",
        note=None,
        confidence="medium",
        place_match="exact",
        model="test-model",
    )

    pages = [text, "bild", text, text]

    assert emo.select_candidate_pages(conn, "doc", pages) == [(1, text), (4, text)]


def test_extract_doc_stores_candidates(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    txt_dir = tmp_path / "text"
    txt_dir.mkdir()
    body = "Olof Palme sågs vid Grand omkring klockan 21.15 enligt vittnet. " * 4
    txt_path = txt_dir / "2055 — Grandbesökare.txt"
    txt_path.write_text(body, encoding="utf-8")

    async def fake_llm(text: str, cfg: dict) -> str:
        return """
        {"observationer": [{
          "person": "Olof Palme",
          "plats": "Grand",
          "tid": "ca 21.15",
          "citat": "Olof Palme sågs vid Grand omkring klockan 21.15.",
          "notering": "vittnesuppgift",
          "confidence": "high"
        }]}
        """

    monkeypatch.setattr(emo, "_call_llm", fake_llm)
    cfg = {"provider": "claude", "model": "test-model", "base_url": "", "api_key": ""}
    place_index = [{
        "name": "Biografen Grand",
        "lat": 59.34057,
        "lon": 18.06024,
        "aliases": ["Biografen Grand", "Grand"],
    }]

    count = asyncio.run(
        emo.extract_doc(
            conn,
            txt_path,
            cfg=cfg,
            dry_run=False,
            place_index=place_index,
            jobs=1,
        )
    )

    rows = db.list_map_observation_candidates(conn)
    assert count == 1
    assert rows[0]["person"] == "Olof Palme"
    assert rows[0]["place_name"] == "Biografen Grand"
    assert rows[0]["time"] == "21:15"
    assert rows[0]["nr"] == "2055"
    assert rows[0]["sida"] == 1


def test_extract_doc_dry_run_does_not_store(tmp_path):
    conn = _conn(tmp_path)
    txt_dir = tmp_path / "text"
    txt_dir.mkdir()
    body = "Olof Palme sågs vid Grand omkring klockan 21.15 enligt vittnet. " * 4
    txt_path = txt_dir / "2055 — Grandbesökare.txt"
    txt_path.write_text(body + "\f" + body, encoding="utf-8")
    cfg = {"provider": "claude", "model": "test-model", "base_url": "", "api_key": ""}

    count = asyncio.run(
        emo.extract_doc(
            conn,
            txt_path,
            cfg=cfg,
            dry_run=True,
            place_index=[],
            jobs=1,
        )
    )

    assert count == 2
    assert db.list_map_observation_candidates(conn) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
.venv/bin/pytest tests/test_extract_map_observations.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'extract_map_observations'`.

- [ ] **Step 3: Implement CLI module**

Create `src/extract_map_observations.py` by adapting the provider/config structure from `src/graph/extract_entities.py`. The system prompt must be:

```python
_SYSTEM = """\
Du extraherar ENDAST källbelagda observationer där en person befinner sig på
en plats vid en tidpunkt under mordkvällen 28 februari 1986.
Returnera ENBART giltig JSON på exakt denna form:
{"observationer": [{
  "person": "Förnamn Efternamn",
  "plats": "platsnamn ur texten",
  "tid": "ca 21.15",
  "citat": "kort ordagrant citat eller mycket nära textutdrag",
  "notering": "kort neutral sammanfattning",
  "confidence": "low|medium|high"
}]}
Regler:
- Ta bara med observationer där texten explicit säger eller starkt anger att
  en person var, sågs, gick, anlände, lämnade eller befann sig vid en plats.
- Gissa inte koordinater. Skriv platsnamnet som det står eller som tydligast
  framgår av sidan.
- Tid får vara exakt eller osäker, t.ex. "ca 21.15", "strax efter 23.20".
- Ta inte med allmänna biografier, adresser, arbetsplatser eller hypotetiska
  resonemang om de inte placerar personen där vid tidpunkten.
- "citat" ska vara kort och källnära så granskaren kan avgöra om kandidaten
  är rimlig.
- Om sidan inte innehåller sådana observationer: {"observationer": []}."""
```

Implement these functions:

```python
MIN_PAGE_ALNUM = 100


def select_candidate_pages(conn, pdf_stem: str, pages: list[str]) -> list[tuple[int, str]]:
    """Välj sidor som kan innehålla kartobservationer."""
    out: list[tuple[int, str]] = []
    for idx, text in enumerate(pages, start=1):
        if sum(1 for c in text if c.isalnum()) < MIN_PAGE_ALNUM:
            continue
        existing = conn.execute(
            """
            SELECT 1 FROM map_observation_candidates
            WHERE pdf_stem=? AND page_num=?
            LIMIT 1
            """,
            (pdf_stem, idx),
        ).fetchone()
        if existing:
            continue
        out.append((idx, text))
    return out
```

Use `src/graph/extract_entities.py` as the exact model for `resolve_provider_cfg`, `_claude_call`, `_openai_call`, `_call_llm`, `fmt_eta`, CLI argument parsing, dry-run behavior, progress output and error logging. In `extract_doc`, after each LLM response:

```python
raw = await asyncio.wait_for(_call_llm(text[:6000], cfg), timeout=timeout)
for obs in parse_map_observation_extraction(raw):
    payload = candidate_payload(
        obs,
        pdf_stem=stem,
        page_num=page_num,
        nr=stem.split(" — ")[0].strip(),
        model=cfg["model"],
        place_index=place_index,
    )
    if payload:
        state_db.record_map_observation_candidate(conn, **payload)
```

Load the place index in `main()` from `data/karta/platser.json`:

```python
places_path = ROOT / "data" / "karta" / "platser.json"
places = json.loads(places_path.read_text(encoding="utf-8")) if places_path.exists() else []
place_index = build_place_index(places)
```

- [ ] **Step 4: Create shell wrapper**

Create `extract_map_observations.sh`:

```bash
#!/bin/bash
# Extrahera granskningsbara kartobservations-kandidater ur OCR-texten.
# Flaggor vidarebefordras till src/extract_map_observations.py.
set -eu
ROOT=$(cd "$(dirname "$0")" && pwd)
cd "$ROOT"
source .venv/bin/activate
exec python -m extract_map_observations "$@"
```

Run:

```bash
chmod +x extract_map_observations.sh
```

- [ ] **Step 5: Run extractor tests and dry-run smoke**

Run:

```bash
.venv/bin/pytest tests/test_extract_map_observations.py tests/test_map_extract.py -q
./extract_map_observations.sh --dry-run --limit 3
```

Expected:
- Tests PASS.
- Dry-run prints a line beginning with `Hittade ` and exits 0 without writing candidates.

---

### Task 4: Review-yta i Karta-fliken

**Files:**
- Modify: `src/pages/7_Karta.py`
- Modify: `src/karta.py`
- Modify: `tests/test_karta.py`

**Interfaces:**
- Consumes:
  - `list_map_observation_candidates(conn, status="pending")`
  - `update_map_observation_candidate(conn, candidate_id, **fields)`
  - `approve_map_observation_candidate(conn, candidate_id)`
  - `reject_map_observation_candidate(conn, candidate_id)`
  - `_casebook_ui.render_source_cards(ROOT, sources, conn, key_prefix: str)`
- Produces:
  - UI section `Granska extraherade kartförslag`
  - Pure helper `candidate_source_payload(candidate: dict, *, source_stem: str | None = None) -> dict | None`

- [ ] **Step 1: Write pure tests for candidate source payload**

Append to `tests/test_karta.py`:

```python
from karta import candidate_source_payload


def test_candidate_source_payload_uses_exact_source_stem():
    candidate = {
        "pdf_stem": "2055 — Grandbesökare",
        "nr": "2055",
        "sida": 3,
        "person": "Olof Palme",
        "raw_place": "Grand",
    }

    assert candidate_source_payload(candidate, source_stem="2055 — Grandbesökare") == {
        "source": "2055 — Grandbesökare.txt",
        "page": 3,
        "nr": "2055",
        "title": "Olof Palme vid Grand",
    }


def test_candidate_source_payload_requires_source_and_page():
    assert candidate_source_payload({"nr": "2055", "sida": 1}, source_stem=None) is None
    assert candidate_source_payload({"nr": "2055", "sida": None}, source_stem="2055 — X") is None
```

- [ ] **Step 2: Implement pure helper in `src/karta.py`**

Add:

```python
def candidate_source_payload(candidate: dict, *, source_stem: str | None = None) -> dict | None:
    """Bygg källkortspayload för en extraherad kartkandidat."""
    nr = str(candidate.get("nr") or "").strip()
    sida = candidate.get("sida")
    stem = str(source_stem or "").strip()
    if not nr or sida in (None, "") or not stem:
        return None
    source = stem if stem.endswith(".txt") else f"{stem}.txt"
    person = str(candidate.get("person") or "").strip()
    place = str(candidate.get("raw_place") or candidate.get("place_name") or "").strip()
    title = f"{person} vid {place}".strip() if person and place else person or place or nr
    return {"source": source, "page": sida, "nr": nr, "title": title}
```

- [ ] **Step 3: Run helper tests**

Run:

```bash
.venv/bin/pytest tests/test_karta.py -q -k candidate_source_payload
```

Expected: PASS.

- [ ] **Step 4: Add review section to `src/pages/7_Karta.py`**

Keep using the existing `_state_db` namespace import; call the new DB helpers as
`_state_db.list_map_observation_candidates`, `_state_db.update_map_observation_candidate`,
`_state_db.approve_map_observation_candidate` and `_state_db.reject_map_observation_candidate`.

Add this helper near `_sources_for_observation`:

```python
def _sources_for_candidate(candidate: dict) -> list[dict]:
    nr = str(candidate.get("nr") or "").strip()
    sida = candidate.get("sida")
    if not nr or sida in (None, ""):
        return []
    mapping = _nr_to_pdf_mapping(str(ROOT))
    stems = [pdf.stem for pdf in _citations.resolve_nr_all(nr, mapping)]
    payloads = []
    for stem in stems:
        payload = _karta.candidate_source_payload(candidate, source_stem=stem)
        if payload:
            payloads.append(payload)
    return payloads
```

Add this section after the existing observation form and selected-observation source block:

```python
st.subheader("Granska extraherade kartförslag")
pending_candidates = _state_db.list_map_observation_candidates(conn, status="pending", limit=50)
if not pending_candidates:
    st.info("Inga kartförslag väntar på granskning.")
else:
    st.caption(f"{len(pending_candidates)} förslag väntar på granskning")

for candidate in pending_candidates:
    label_time = candidate.get("time") or "utan tid"
    label_place = candidate.get("place_name") or candidate.get("raw_place") or "okänd plats"
    with st.expander(
        f"{candidate['person']} · {label_time} · {label_place} · "
        f"Nr {candidate['nr']}, sida {candidate['sida']}"
    ):
        st.write(candidate.get("quote") or "")
        st.caption(
            f"Matchning: {candidate['place_match']} · "
            f"confidence: {candidate['confidence']} · modell: {candidate['model']}"
        )
        sources = _sources_for_candidate(candidate)
        if sources:
            _casebook_ui.render_source_cards(
                ROOT,
                sources,
                conn,
                key_prefix=f"karta_candidate_source_{candidate['id']}",
            )
        else:
            st.caption("Ingen lokal arkivfil matchade kandidatens nr.")

        with st.form(f"karta_candidate_form_{candidate['id']}"):
            cols = st.columns(3)
            with cols[0]:
                person = st.text_input("Person", value=candidate["person"])
                place_name = st.text_input(
                    "Platsnamn",
                    value=candidate.get("place_name") or candidate.get("raw_place") or "",
                )
            with cols[1]:
                lat = st.number_input(
                    "Lat",
                    value=float(candidate["lat"]) if candidate["lat"] is not None else 59.33695,
                    format="%.6f",
                )
                lon = st.number_input(
                    "Lon",
                    value=float(candidate["lon"]) if candidate["lon"] is not None else 18.06324,
                    format="%.6f",
                )
            with cols[2]:
                time = st.text_input("Tid (HH:MM)", value=candidate.get("time") or "")
                uncertainty = st.text_input("Osäkerhet", value=candidate.get("uncertainty") or "")
            note = st.text_area(
                "Notering",
                value=candidate.get("note") or candidate.get("quote") or "",
            )
            save, approve, reject = st.columns(3)
            save_clicked = save.form_submit_button("Spara ändringar")
            approve_clicked = approve.form_submit_button("Godkänn till kartan")
            reject_clicked = reject.form_submit_button("Avvisa")

        candidate_updates = _karta.db_observation_payload({
            "person": person,
            "place_name": place_name,
            "lat": lat,
            "lon": lon,
            "time": time,
            "uncertainty": uncertainty,
            "note": note,
        })
        if save_clicked:
            _state_db.update_map_observation_candidate(conn, candidate["id"], **candidate_updates)
            st.toast("Kartförslaget uppdaterat")
            st.rerun()
        if approve_clicked:
            _state_db.update_map_observation_candidate(conn, candidate["id"], **candidate_updates)
            try:
                _state_db.approve_map_observation_candidate(conn, candidate["id"])
            except ValueError as exc:
                st.error(f"Kan inte godkänna: {exc}")
            else:
                st.toast("Kartförslaget godkänt")
                st.rerun()
        if reject_clicked:
            _state_db.reject_map_observation_candidate(conn, candidate["id"])
            st.toast("Kartförslaget avvisat")
            st.rerun()
```

- [ ] **Step 5: Run UI-adjacent tests**

Run:

```bash
.venv/bin/pytest tests/test_karta.py tests/test_db.py -q -k "candidate or map_observation"
python3 -m compileall src
```

Expected: PASS and compileall exit 0.

---

### Task 5: Documentation and verification

**Files:**
- Modify: `README.md`
- Modify: `docs/kom-igang.md`
- Modify: `docs/teknisk-referens.md`
- Modify: `AGENTS.md`
- Modify: `CLAUDE.md`
- Modify: `data/karta/README.md`

**Interfaces:**
- Consumes:
  - New command `./extract_map_observations.sh`
  - New DB table `map_observation_candidates`
  - Karta review UI from Task 4
- Produces:
  - User-facing docs for dry-run, candidate extraction and manual review.
  - Agent docs for future sessions.

- [ ] **Step 1: Update README**

In the `### Karta` section, replace the paragraph with:

```markdown
**Karta** visar källhänvisade observationer på en tidsanimerad karta över
mordkvällen. Platskatalogen seedas och används för snabbval i formuläret, medan
observationerna kan redigeras i appen; rörelser visas bara när de har tid,
koordinater och källa. Kartförslag kan dessutom extraheras som en separat
granskningskö med `./extract_map_observations.sh` och godkänns manuellt innan
de syns på kartan.
```

- [ ] **Step 2: Update quickstart**

In `docs/kom-igang.md`, extend the Karta section with:

````markdown
För att leta fram kandidater ur OCR-texten utan att publicera dem direkt:

```bash
./extract_map_observations.sh --dry-run --limit 20
./extract_map_observations.sh --limit 20
```

Öppna sedan Karta-fliken och granska förslagen under **Granska extraherade
kartförslag**. En kandidat måste ha person, koordinat, tid och källa innan den
kan godkännas till tidslinjen.
````

- [ ] **Step 3: Update technical reference**

Add to the state-db section:

```markdown
- `map_observation_candidates` — granskningskö för LLM-extraherade
  kartförslag. Raderna har `status` (`pending`, `approved`, `rejected`) och
  blir inte publicerade kartobservationer förrän `approve_map_observation_candidate`
  skapar en rad i `map_observations`.
```

Add to the Karta section:

```markdown
`extract_map_observations.sh` läser `generated/text/*.txt` sida för sida och
skapar kandidater i `map_observation_candidates`. Extraktorn använder samma
LLM-konfigurationsmönster som kunskapsgrafens `extract_entities.sh`, men har en
snäv prompt: bara person + plats + tid + citatutdrag. Platsnamn matchas mot
`data/karta/platser.json`; okända platser sparas utan koordinater och måste
kompletteras i review-UI:t innan de kan godkännas.
```

Add to the commands table:

```bash
./extract_map_observations.sh --dry-run [--limit N]
./extract_map_observations.sh [--limit N] [--provider openai --model gpt-4o-mini]
```

- [ ] **Step 4: Update AGENTS and CLAUDE**

In both files, add to directory/command sections:

```markdown
  map_extract.py              # Ren parsing/platsmatchning för kartobservations-kandidater
  extract_map_observations.py # LLM-extraktion av person-position-tid-kandidater
```

and:

```bash
./extract_map_observations.sh [--dry-run] [--limit N]
```

Add a non-obvious design note:

```markdown
**Kartobservations-extraktion**: LLM-extraktorn skriver bara till
`map_observation_candidates`. Kandidater är granskningsmaterial, inte fakta på
kartan. Först när användaren godkänner en kandidat i Karta-fliken skapas en rad
i `map_observations`.
```

- [ ] **Step 5: Update `data/karta/README.md`**

Append:

```markdown
## Extraherade kandidater

`./extract_map_observations.sh` skapar kandidater i state-db, inte i
`rorelser.json`. Granska alltid kandidatens citat och källkort i Karta-fliken
innan den godkänns. Kandidater utan koordinater behöver matchas mot en plats
eller få lat/lon manuellt.
```

- [ ] **Step 6: Run final verification**

Run:

```bash
.venv/bin/pytest tests/ -q
python3 -m compileall src
python3 -m json.tool data/karta/platser.json >/tmp/platser.json
python3 -m json.tool data/karta/rorelser.json >/tmp/rorelser.json
./extract_map_observations.sh --dry-run --limit 3
git status --short --untracked-files=all
```

Expected:
- Pytest: all tests pass.
- Compileall exits 0.
- Both JSON files validate.
- Dry-run exits 0 and does not create approved map observations.
- Git status shows only intentional source, tests and docs changes.

---

## Manual QA Checklist

- Start web UI: `./web.sh -- --server.headless true --server.port 8503`.
- Open `http://localhost:8503` and go to **Karta**.
- Confirm existing manually-created observations still render.
- Run `./extract_map_observations.sh --limit 1` on a test corpus or real first document.
- Confirm pending candidates appear under **Granska extraherade kartförslag**.
- Open a candidate's source card and verify PDF/text buttons resolve.
- Edit time/coordinates on one candidate and save.
- Approve it; confirm it disappears from pending and appears on the map/timeline.
- Reject another candidate; confirm it disappears from pending and remains queryable as `status='rejected'`.

## Self-Review Notes

- Spec coverage: candidate-only extraction, source-backed review, place matching, UI approval and documentation are covered.
- No direct auto-publication path exists in the plan; `approve_map_observation_candidate` is the only bridge into `map_observations`.
- Types and field names match the existing Karta schema where possible: `person`, `place_name`, `lat`, `lon`, `time`, `uncertainty`, `nr`, `sida`, `note`.
- The plan intentionally avoids full geocoding. It only matches against the existing curated place catalog and requires manual coordinates for unknown places.
