# Kartmodul — designspec

**Datum:** 2026-06-20
**Status:** Godkänd design, redo för implementationsplan
**Flik:** Karta (ny Streamlit-sida)

## Mål

En kartmodul som visar var de centrala aktörerna befann sig vid olika tidpunkter
kvällen för Palmemordet — **28 februari 1986, ca 20:30–24:00** — och hur de rörde
sig i området runt mordplatsen (Sveavägen/Tunnelgatan, Grand, Dekorima,
Skandiahuset m.fl.). Positionerna ska kunna **justeras (plats och tid) och
redigeras fritt** i appen så att utredaren kan pröva hypoteser om rörelsemönster.

Varje observation är källhänvisad till ett arkivdokument (Nr/sida) — inget
hamnar på kartan utan en referens som kan verifieras, i linje med projektets
övriga delar.

## Avgränsning

**Ingår (v1):**
- En kurerad uppsättning aktörer den aktuella kvällen (t.ex. Palme-paret, Stig
  Engström, centrala vittnen, gärningsmannen enligt vittnesuppgifter).
- Animerad uppspelning av rörelser över kvällen, med play/paus och tidslinje.
- Full redigering (CRUD) av observationer och en platskatalog.

**Ingår inte (medvetet bortvalt):**
- Hela arkivet / godtycklig person och tidsperiod.
- LLM-extraktion av rörelser ur dokumenten.
- Historisk/perioddtrogen baskarta (OSM-rutor används).

Datamodellen är dock generell nog att senare utökas till fler händelser/tider.

## Datamodell

Datan lever i **state.db** (`generated/db/state.db`), seedad från incheckade
JSON-filer. All CRUD går via `src/db.py` — konsumenter skriver aldrig egen SQL
(samma princip som bokmärken/anteckningar).

### Tabeller

```sql
CREATE TABLE IF NOT EXISTS map_places (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    lat        REAL NOT NULL,
    lon        REAL NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS map_observations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    person      TEXT NOT NULL,
    place_name  TEXT,            -- valfri etikett (t.ex. "biografen Grand")
    lat         REAL NOT NULL,   -- egna koordinater per observation
    lon         REAL NOT NULL,
    time        TEXT,            -- "HH:MM" under mordkvällen
    uncertainty TEXT,            -- t.ex. "ca", "±10 min"
    nr          TEXT,            -- källans arkivnummer
    sida        INTEGER,         -- källans sida
    note        TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
```

**Designval:** `map_places` är en *katalog* för snabbval; `map_observations`
bär **egna koordinater** så att en persons position kan justeras fritt utan att
påverka andra observationer eller katalogen. Personfärg lagras inte — den
härleds deterministiskt ur personnamnet i `karta.py` (stabil palett), så att
lägga till/ta bort en person bara är att lägga till/ta bort observationer.

### Seed

- `data/karta/platser.json` — `[{name, lat, lon}, …]` för de ~20–30 relevanta
  platserna.
- `data/karta/rorelser.json` — `[{person, place_name, lat, lon, time,
  uncertainty?, nr?, sida?, note?}, …]`.
- `db.seed_map_data_if_empty(conn, places, observations)` importerar dessa
  **endast om tabellerna är tomma** (idempotent). Seedfilerna är incheckade och
  granskningsbara; varje rörelse-rad bör ha en källa.

> **Obs om datan:** rörelserna är handkurerade och påstår var personer var.
> Seed-setet ska tas fram ur arkivet med källhänvisningar och granskas av
> användaren innan det litas på.

## Arkitektur

Projektets mönster: ren, testbar logik i en modul + en tunn Streamlit-sida.

### `src/db.py` (utökas)

```python
def seed_map_data_if_empty(conn, places: list[dict], observations: list[dict]) -> int
def list_map_places(conn) -> list[dict]
def record_map_place(conn, *, name, lat, lon) -> int
def delete_map_place(conn, place_id: int) -> bool
def list_map_observations(conn, *, person: str | None = None) -> list[dict]
def record_map_observation(conn, *, person, place_name, lat, lon,
                           time=None, uncertainty=None, nr=None,
                           sida=None, note=None) -> int
def update_map_observation(conn, obs_id: int, **fields) -> bool
def delete_map_observation(conn, obs_id: int) -> bool
```

### `src/karta.py` (ny, Streamlit-fri)

```python
PERSON_PALETTE: list[str]                       # stabil färgpalett
def person_color(name: str) -> str              # deterministisk färg ur namn
def validate_observation(obs: dict) -> list[str]   # fellista (tom = ok)
def observations_by_person(obs: list[dict]) -> dict[str, list[dict]]  # sorterat på tid
def popup_html(obs: dict) -> str                # innehåller "[Nr X, sida Y]"
def build_timestamped_geojson(obs: list[dict], *,
                              base_date: str = "1986-02-28") -> dict
```

`build_timestamped_geojson` returnerar en GeoJSON `FeatureCollection`:
- en **Point-feature per observation** med `properties.times = [ISO-tid]`,
  `icon`/färg per person och `popup`-HTML med källcitat,
- en **LineString-feature per person** (spåret), med `times` för varje punkt,
  så markören rör sig längs linjen i animeringen.

Tid lagras som `"HH:MM"` och kombineras med `base_date` till ISO
(`1986-02-28THH:MM:00`) vid bygget. Observationer **utan** `time` tas inte med i
den animerade tidslinjen (de saknar tidpunkt) men listas och kan redigeras i
redigeraren — sätt en tid för att få med dem i uppspelningen.

### `src/pages/7_Karta.py` (ny sida, Layout A)

- Laddar observationer/platser ur state.db (seedar vid behov).
- Bygger `folium.Map` centrerad på mordplatsen (OSM-rutor) + `TimestampedGeoJson`
  och renderar via `st_folium`.

## UI-flöde (Layout A)

```
┌───────────────────────────────────────────────┐
│  Karta — mordkvällen 28 feb 1986               │
├──────────────┬────────────────────────────────┤
│ Personer     │   [folium-karta]                │
│ ☑ 🟥 Palme   │   markörer + färgade spår       │
│ ☑ 🟧 Engström│   popup: Person — Plats,        │
│ ☑ 🟩 Vittne A│   kl HH:MM [Nr X, sida Y]       │
│              │   ⏮ ▶ ⏯ ──●──── 23:21 (tidslinje)│
│ Tid: 23:21   │                                 │
├──────────────┴────────────────────────────────┤
│  Redigera observationer                        │
│  [tabell, filtrerbar på person]                │
│  Formulär: person · plats (katalog/fritt) ·    │
│  tid · osäkerhet · källa (Nr/sida) · notering  │
│  [Använd senaste kartklick] [Spara][Ny][Ta bort]│
│  Källkort för vald observation (Öppna PDF/bokmärk)│
└────────────────────────────────────────────────┘
```

- **Vänsterpanel:** färgkodade personer (kryssrutor för visa/dölj) + aktuell tid.
- **Karta:** folium + `TimestampedGeoJson` (play/paus, slider), färgade spår +
  markörer, popup med källcitat per position.
- **Redigeringssektion (under kartan):**
  - Tabell över observationer (filtrerbar på person).
  - Formulär för vald/ny observation: *person, plats-namn, plats ur katalog
    (autofyller namn + koordinat), tid (HH:MM), osäkerhet, källa (Nr/sida),
    notering*.
  - **"Använd senaste kartklick som koordinat"** — läser `st_folium`
    `last_clicked` så plats kan sättas genom att klicka på kartan.
  - Knappar: **Spara**, **Lägg till ny**, **Ta bort** (full CRUD via db.py).
  - **Källkort** via `casebook_ui.render_source_cards` för den valda
    observationens källa → Öppna PDF/text, bokmärk, anteckna (återanvänder
    befintliga komponenter). Popupen ger citatet; källkortet ger de fungerande
    PDF-knapparna.

## Beroenden

- Nya: **`folium`**, **`streamlit-folium`** — läggs i `[web]`-extran i
  `pyproject.toml`, installeras via `install.sh` (`pip install -e .[web]`).
  Fristående från de pinnade ML-paketen (`sentence-transformers<5` m.fl.) → låg
  krockrisk.
- OSM-kartrutor kräver internet.

## Felhantering

- `karta.py` **validerar** varje observation (giltiga koordinater, tid,
  källa) och hoppar över ogiltiga med en UI-varning i stället för att krascha.
- Tom datamängd → informativt meddelande + möjlighet att seeda/lägga till.
- Offline: markörer/spår ritas ändå (blank bakgrund); en caption noterar att
  kartrutorna kräver internet.
- Redigering: bekräftelse vid borttagning; ogiltigt formulär (saknad
  person/koordinat) avvisas med felmeddelande.

## Tester

- **`tests/test_db.py`:** CRUD för `map_places` och `map_observations`
  (lägg till, ändra plats+tid, ta bort, lista, filtrera på person) samt
  idempotent `seed_map_data_if_empty` (seedar tom db, gör inget vid icke-tom).
- **`tests/test_karta.py`:** `person_color` stabil/distinkt; `validate_observation`
  fångar saknad koordinat/källa; `observations_by_person` sorterar på tid;
  `popup_html` innehåller `[Nr X, sida Y]`; `build_timestamped_geojson` ger rätt
  antal point-/line-features med `times`-fält och ISO-tider ur `base_date`.
- Sidan `7_Karta.py` enhetstestas inte (Streamlit) — bara `compileall`, enligt
  projektets konvention.

## Dokumentation (uppdateras i samma ändring)

- `README.md` — ny flik **Karta**.
- `docs/kom-igang.md` — kort beskrivning.
- `docs/teknisk-referens.md` — datamodell (tabeller + seedfiler), animationsteknik
  (TimestampedGeoJson), redigeringsflöde, nya beroenden, filöversikt.
- `CLAUDE.md` + `AGENTS.md` — katalogstruktur, designnotis, nya beroenden i
  gotchas, `state.db`-tabellraden.
- Kort format-README för `data/karta/*.json`.

## Filöversikt (ny/ändrad)

| Fil | Roll |
|-----|------|
| `src/karta.py` | Ren logik: validering, färg, GeoJSON-bygge för TimestampedGeoJson |
| `src/pages/7_Karta.py` | Streamlit-sida (Layout A): karta + animation + redigering |
| `src/db.py` | + `map_places`/`map_observations` schema, CRUD och seed |
| `data/karta/platser.json` | Seed: platskatalog |
| `data/karta/rorelser.json` | Seed: källhänvisade observationer |
| `tests/test_karta.py` | Tester för `karta.py` |
| `tests/test_db.py` | + tester för kart-CRUD och seed |
