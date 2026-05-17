# SQLite-baserad state för pipeline-tracking

**Datum:** 2026-05-17
**Status:** Godkänd för planering

## Bakgrund

Pipelinen har idag operativ state utspridd i många filtyper:

- Stamp-filer: `generated/text/.normalize_stamp`, `generated/.quality_stamp`
- Markörfiler: `generated/text/<stem>.redact`, `generated/text_pages/<stem>/page-NNN.json`
- Tabulär state: `downloaded/files/manifest.csv`, `downloaded/wpu_files/manifest.csv`, `generated/quality.csv`, `generated/quality_pages.jsonl`
- mtime-kolumn i LanceDB-tabellen (för re-ingest-beslut)

Detta gör inkrementell logik spröd (en raderad stamp tvingar omkörning av allt), försvårar query över state ("vilka filer saknar quality?"), och sprider definitionen av "vad är gjort?" över många moduler.

## Mål

Samla all operativ pipeline-state i en SQLite-databas, behåll filsystemet enbart för faktiskt innehåll (PDF:er, OCR-text, bilder, vektorer, loggar, konfig).

## Icke-mål

Utanför scope (behålls som filer):

- `generated/errors.log` — append-only fellogg
- `generated/unusable.txt` + `unusable_mtimes.json` — manuellt kuraterad lista
- `generated/llm_config.json` — användarkonfig från webui
- `generated/lancedb/` — vektorindex (LanceDB är sin egen store)
- `generated/text/`, `generated/text_pages/`, `generated/ocr/` — faktiskt OCR-innehåll
- `downloaded/files/*.pdf`, `downloaded/wpu_files/*.pdf` — råfiler

## Designval

### Val av databas: SQLite

- Embedded, ingen server — passar lokal singel-användarpipeline
- Standardbibliotek i Python (`sqlite3`) — inga nya beroenden
- WAL-mode hanterar pipelinens 4 parallella OCR-jobb utan låsningskonflikter
- Lätt att backa upp (en fil) och inspektera (`sqlite3` CLI)

Övervägda alternativ: DuckDB (overkill — analytiska aggregeringar är inte huvudbehovet), Postgres (kräver server, för tungt för en lokal pipeline).

### Placering

`generated/state.db` — gitignored som resten av `generated/`.

## Schema

### `schema_version`

| Kolumn | Typ | Not |
|---|---|---|
| `version` | INTEGER PRIMARY KEY | Aktuell schemaversion |
| `applied_at` | TEXT | ISO-timestamp |

Trivial migrationsmekanism: `CREATE TABLE IF NOT EXISTS` + version-bump vid breaking changes.

### `downloads`

Ersätter `manifest.csv` i både `downloaded/files/` och `downloaded/wpu_files/`.

| Kolumn | Typ | Not |
|---|---|---|
| `source` | TEXT | `files` eller `wpu` |
| `drive_id` | TEXT | Google Drive file-id (NULL för wpu) |
| `url` | TEXT | Käll-URL (NULL för Drive) |
| `filename` | TEXT NOT NULL | Filnamn i mål-mappen |
| `sha1` | TEXT | SHA1 av nedladdad fil |
| `bytes` | INTEGER | Filstorlek |
| `downloaded_at` | TEXT NOT NULL | ISO-timestamp |
| `note` | TEXT | T.ex. "dup-of:<filename>" |

PRIMARY KEY: `(source, COALESCE(drive_id, url))`
Index på `sha1` (för dup-detektering).

### `pdf_files`

En rad per PDF — central status-tabell.

| Kolumn | Typ | Not |
|---|---|---|
| `pdf_stem` | TEXT PRIMARY KEY | T.ex. `00001-0001` |
| `source` | TEXT NOT NULL | `files` eller `wpu` |
| `pdf_path` | TEXT NOT NULL | Relativ sökväg till PDF |
| `redaction_checked_at` | TEXT | NULL = ej körd |
| `has_redactions` | INTEGER | 0/1, NULL om ej körd |
| `merged_at` | TEXT | När `text/<stem>.txt` byggdes från sidor |
| `normalized_at` | TEXT | Ersätter `.normalize_stamp` (per fil) |
| `text_mtime` | REAL | mtime på `text/<stem>.txt` vid normalisering |

Ersätter: `.redact`-markörfiler, `.normalize_stamp` (nu per-fil i stället för global).

### `pdf_pages`

Ersätter `page-NNN.json` per sida.

| Kolumn | Typ | Not |
|---|---|---|
| `pdf_stem` | TEXT NOT NULL | FK till `pdf_files` |
| `page_num` | INTEGER NOT NULL | 1-indexerad |
| `engine` | TEXT NOT NULL | `tesseract` eller `surya` |
| `text` | TEXT | OCR-text för sidan |
| `score` | REAL | Per-sida-kvalitetspoäng |
| `processed_at` | TEXT NOT NULL | ISO-timestamp |

PRIMARY KEY: `(pdf_stem, page_num)`.

Funktionellt: tjänar som idempotens-markör (om rad finns → sidan är OCR:ad) och tar bort behovet av `page-NNN.json`-filer.

### `quality`

Ersätter `generated/quality.csv`.

| Kolumn | Typ | Not |
|---|---|---|
| `pdf_stem` | TEXT PRIMARY KEY | FK till `pdf_files` |
| `score` | REAL | 0–100 |
| `chars` | INTEGER | |
| `non_ascii_ratio` | REAL | |
| `dict_hit_ratio` | REAL | |
| `…` | | Övriga heuristik-fält som idag finns i `quality.csv` |
| `text_mtime` | REAL | mtime på text-filen som bedömdes |
| `scored_at` | TEXT NOT NULL | ISO-timestamp |

Inkrementell logik: "ge filer där `pdf_files.text_mtime > quality.text_mtime` eller `quality` saknas". Ersätter `.quality_stamp`.

### `quality_pages`

Ersätter `generated/quality_pages.jsonl`.

| Kolumn | Typ | Not |
|---|---|---|
| `pdf_stem` | TEXT NOT NULL | |
| `page_num` | INTEGER NOT NULL | |
| `score` | REAL | |
| `…` | | Övriga per-sida-fält |
| `scored_at` | TEXT NOT NULL | |

PRIMARY KEY: `(pdf_stem, page_num)`.

### `ingest`

Ersätter `mtime`-kolumnen i LanceDB-tabellen (eller kompletterar den — se beslut nedan).

| Kolumn | Typ | Not |
|---|---|---|
| `pdf_stem` | TEXT PRIMARY KEY | |
| `text_mtime` | REAL NOT NULL | mtime som indexerades |
| `chunks` | INTEGER | Antal chunks som lades in |
| `indexed_at` | TEXT NOT NULL | |

**Beslut:** Behåll mtime-kolumnen i LanceDB tills vidare (det är den som queryas vid sökning). `ingest`-tabellen är auktoritativ för "vad har indexerats sedan när"-beslut. Vid re-build av LanceDB kan vi rekonstruera från `ingest` + `text/`.

## Åtkomstskikt: `src/db.py`

Ny modul med:

- `connect(path: Path = DEFAULT_DB) -> sqlite3.Connection` — öppnar med `PRAGMA journal_mode=WAL`, `PRAGMA foreign_keys=ON`, returnerar connection med `row_factory=sqlite3.Row`.
- `init_schema(conn)` — idempotent `CREATE TABLE IF NOT EXISTS …`, bumpar `schema_version` vid behov.
- Domänfunktioner per tabell, t.ex.:
  - `record_download(conn, source, drive_id, filename, sha1, bytes, …)`
  - `is_downloaded(conn, source, drive_id) -> bool`
  - `record_page(conn, pdf_stem, page_num, engine, text, score)`
  - `page_exists(conn, pdf_stem, page_num) -> bool`
  - `mark_redaction_checked(conn, pdf_stem, has_redactions)`
  - `redaction_checked(conn, pdf_stem) -> bool`
  - `files_needing_quality(conn) -> list[Path]` — text_mtime > quality.text_mtime eller saknas
  - `files_needing_normalize(conn) -> list[Path]`
  - `files_needing_ingest(conn) -> list[Path]`
  - `record_quality(conn, pdf_stem, score, …, text_mtime)`
  - `record_ingest(conn, pdf_stem, text_mtime, chunks)`

Princip: konsumenter (`download.py`, `ocr_pages.py`, `normalize_text.py`, `quality.py`, `merge_pages.py`, `ingest.py`) ska inte själva skriva SQL — de anropar `db.X()`.

## Migrering

Engångsskript `src/migrate_to_db.py`:

1. Skapar/öppnar `generated/state.db`, kör `init_schema`.
2. Läser `downloaded/files/manifest.csv` + `downloaded/wpu_files/manifest.csv` → `downloads`.
3. Skannar `downloaded/files/*.pdf` + `downloaded/wpu_files/*.pdf` → `pdf_files` (med `source`, `pdf_path`).
4. Läser `generated/text/<stem>.redact` → `pdf_files.has_redactions = 1`, `redaction_checked_at = mtime`.
5. Skannar `generated/text_pages/<stem>/page-*.json` → `pdf_pages`.
6. Sätter `pdf_files.normalized_at` = mtime på `.normalize_stamp` om `text/<stem>.txt.mtime <= stamp.mtime` (best effort — gamla filer markeras normaliserade).
7. Sätter `pdf_files.merged_at` = mtime på `text/<stem>.txt`.
8. Läser `quality.csv` → `quality`.
9. Läser `quality_pages.jsonl` → `quality_pages`.
10. Läser LanceDB-tabellen (`mtime`-kolumnen, distinct per `pdf_stem`) → `ingest`.

Idempotent (UPSERT). Filerna lämnas kvar tills migreringen verifierats; en separat städ-kommando tar bort dem efteråt.

## Inkrementell logik — exempel

**Normalisering (ersätter `.normalize_stamp`):**
```sql
SELECT pdf_stem FROM pdf_files
WHERE normalized_at IS NULL
   OR text_mtime > strftime('%s', normalized_at);
```

**Quality (ersätter `.quality_stamp`):**
```sql
SELECT pf.pdf_stem FROM pdf_files pf
LEFT JOIN quality q USING (pdf_stem)
WHERE q.pdf_stem IS NULL
   OR pf.text_mtime > q.text_mtime;
```

**Ingest:**
```sql
SELECT pf.pdf_stem FROM pdf_files pf
LEFT JOIN ingest i USING (pdf_stem)
WHERE i.pdf_stem IS NULL
   OR pf.text_mtime > i.text_mtime;
```

## Concurrency

- `PRAGMA journal_mode=WAL` — flera läsare + en skrivare parallellt utan låsning.
- Pipelinen kör typiskt 4 parallella OCR-jobb. Varje jobb öppnar egen connection, skriver `pdf_pages` för sin sida. Skrivkollisioner extremt osannolika (olika `(pdf_stem, page_num)`-rader); vid kollision retry-loop med backoff i `db.py`.
- Långa transaktioner undviks — en commit per sida/fil.

## Påverkade moduler

| Modul | Förändring |
|---|---|
| `src/download.py` | Läser/skriver `downloads` via `db.py` i stället för `manifest.csv` |
| `src/ocr_pages.py` | Skriver `pdf_pages`-rader i stället för `page-NNN.json`. Markörer för redaktion → `pdf_files.has_redactions` |
| `src/merge_pages.py` | Läser sidor från `pdf_pages` i stället för disk, skriver `pdf_files.merged_at` |
| `src/normalize_text.py` | Tar bort stamp-filslogik, frågar `files_needing_normalize()`, skriver `pdf_files.normalized_at`+`text_mtime` |
| `src/quality.py` | Tar bort stamp-filslogik och CSV/JSONL-skrivning. `files_needing_quality()`, `record_quality()` |
| `src/rag/ingest.py` | mtime-läsning från `ingest`-tabellen i stället för LanceDB-kolumnen för delta-beslut |
| `src/llm_correct.py` | Läser dåliga sidor från `quality_pages` i stället för jsonl |
| Shell-wrappers (`*.sh`) | Bara hjälptexter ändras (inga fler `--rebuild`-flaggor som rör stampar) |
| `detect_redactions.sh` | Filtrerar redan-checkade filer via SQL i stället för `.redact`-glob |

## Testning

- Befintliga tester (`test_normalize_text`, `test_quality`, `test_download`, `test_detect_redactions`, `test_reingest`, `test_merge_pages`) anpassas — använder temporär DB i fixture.
- Ny `test_db.py` täcker `db.py`-funktioner direkt (init_schema, idempotens, concurrency med `threading`).
- Ny `test_migrate.py` täcker engångsmigreringen (fixture med en fejk-`generated/`).

## Verifiering

1. Kör `migrate_to_db.py` i en kopia av `generated/`.
2. Diff: SELECT-count per tabell mot källfilernas radantal.
3. Kör pipelinen "noop" (alla filer redan processade) — inget jobb ska triggas.
4. Touch:a en `text/*.txt`, kör pipeline — bara den filen ska re-processas i `quality`/`ingest`.

## Rollback

- `generated/state.db` är inte källan till sanning för innehåll (text-filerna är) — kan raderas och återskapas från `migrate_to_db.py`.
- Gamla CSV/JSONL/markörfiler tas inte bort förrän vi verifierat att SQLite-pipelinen fungerar i skarpt läge.

## Öppna frågor

Inga vid godkännande 2026-05-17.
