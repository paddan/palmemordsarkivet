# SQLite-state Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ersätt filbaserad pipeline-state (`.normalize_stamp`, `.quality_stamp`, `.redact`, `page-NNN.json`, `manifest.csv`, `quality.csv`, `quality_pages.jsonl`, mtime-kolumn i LanceDB) med en SQLite-databas (`generated/state.db`).

**Architecture:** Ny modul `src/db.py` exponerar `connect()` + domänfunktioner (en per tabell). Konsumentmoduler (`download.py`, `ocr_pages.py`, `merge_pages.py`, `normalize_text.py`, `quality.py`, `llm_correct.py`, `rag/ingest.py`) ersätter sin filbaserade idempotens-logik med anrop till `db`. Engångsskript `src/migrate_to_db.py` fyller databasen från befintliga filer. Filerna lämnas kvar tills verifiering är klar.

**Tech Stack:** Python 3, `sqlite3` (stdlib), pytest. Inga nya beroenden.

**Spec:** `docs/superpowers/specs/2026-05-17-sqlite-state-design.md`

---

## File Structure

**Nya filer:**
- `src/db.py` — schema + connection helper + domänfunktioner
- `src/migrate_to_db.py` — engångsskript som läser befintliga filer och fyller databasen
- `migrate_to_db.sh` — shell-wrapper
- `tests/test_db.py` — enhetstester för `db.py` (init_schema, CRUD per tabell, concurrency)
- `tests/test_migrate_to_db.py` — verifierar migreringen mot en fixture-`generated/`

**Modifierade filer:**
- `src/download.py` — ersätter `load_manifest`/`append_manifest` med db-anrop
- `src/download_wpu.py` — samma (om manifestlogik finns där också)
- `src/ocr_pages.py` — `page-NNN.json` ersätts av `db.record_page` + `db.page_exists`; `.redact`-markör ersätts av `db.mark_redaction_checked`
- `src/merge_pages.py` — `find_updates` läser sidor från `pdf_pages`-tabellen via `db`, raderar inte `.json` (de finns inte längre)
- `src/normalize_text.py` — tar bort stamp-fil, frågar `db.files_needing_normalize()` och skriver `db.record_normalized()`
- `src/quality.py` — tar bort stamp-fil + CSV/JSONL-skrivning, läser/skriver via `db`
- `src/llm_correct.py` — läser dåliga sidor från `db.quality_pages` i stället för jsonl
- `src/rag/ingest.py` — `already`-dict byggs från `db.ingest`-tabellen i stället för LanceDB-mtime-kolumn
- `detect_redactions.sh` — filtrerar redan-kontrollerade filer via `sqlite3` i stället för `.redact`-glob
- `CLAUDE.md` — uppdatera "Non-obvious Design Decisions" och "Common Gotchas"
- `README.md` — användarvänd dokumentation om `state.db`

**Bestående tester** som kan behöva uppdateras:
- `tests/test_quality.py`, `tests/test_normalize_text.py`, `tests/test_download.py`,
  `tests/test_detect_redactions.py`, `tests/test_reingest.py`, `tests/test_merge_pages.py`

---

## Task 1: `src/db.py` — schema + connection

**Files:**
- Create: `src/db.py`
- Test: `tests/test_db.py`

- [ ] **Step 1: Skriv test för `connect()` + `init_schema()`**

Skapa `tests/test_db.py`:

```python
import sqlite3
from pathlib import Path

import pytest

from db import connect, init_schema, SCHEMA_VERSION


def test_connect_creates_file_with_wal(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect(db_path)
    try:
        cur = conn.execute("PRAGMA journal_mode")
        assert cur.fetchone()[0] == "wal"
        cur = conn.execute("PRAGMA foreign_keys")
        assert cur.fetchone()[0] == 1
    finally:
        conn.close()


def test_init_schema_is_idempotent(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect(db_path)
    try:
        init_schema(conn)
        init_schema(conn)  # andra körningen ska inte krascha
        version = conn.execute(
            "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
        ).fetchone()["version"]
        assert version == SCHEMA_VERSION
        tables = {
            r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"downloads", "pdf_files", "pdf_pages",
                "quality", "quality_pages", "ingest",
                "schema_version"}.issubset(tables)
    finally:
        conn.close()
```

- [ ] **Step 2: Verifiera att testen failar**

Run: `.venv/bin/pytest tests/test_db.py -v`
Expected: ImportError (modulen finns inte).

- [ ] **Step 3: Skapa `src/db.py` med connect + schema**

```python
"""SQLite-baserad state för pipeline-tracking.

Ersätter filbaserade markörer (.normalize_stamp, .quality_stamp, .redact,
page-NNN.json, manifest.csv, quality.csv, quality_pages.jsonl, LanceDB-mtime).

Schema, åtkomstskikt och inkrementella frågor samlas här — konsumentmoduler
ska aldrig skriva egen SQL.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = Path(
    os.environ.get("STATE_DB") or (ROOT / "generated" / "state.db")
)

SCHEMA_VERSION = 1


def now() -> str:
    """ISO-timestamp i UTC — används som default för *_at-kolumner."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path = DEFAULT_DB) -> sqlite3.Connection:
    """Öppna SQLite med WAL, foreign keys och Row-factory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS downloads (
    source        TEXT NOT NULL,
    drive_id      TEXT,
    url           TEXT,
    filename      TEXT NOT NULL,
    sha1          TEXT,
    bytes         INTEGER,
    downloaded_at TEXT NOT NULL,
    note          TEXT,
    PRIMARY KEY (source, COALESCE(drive_id, url))
);
CREATE INDEX IF NOT EXISTS idx_downloads_sha1 ON downloads(sha1);

CREATE TABLE IF NOT EXISTS pdf_files (
    pdf_stem             TEXT PRIMARY KEY,
    source               TEXT NOT NULL,
    pdf_path             TEXT NOT NULL,
    redaction_checked_at TEXT,
    has_redactions       INTEGER,
    merged_at            TEXT,
    normalized_at        TEXT,
    text_mtime           REAL
);

CREATE TABLE IF NOT EXISTS pdf_pages (
    pdf_stem     TEXT NOT NULL,
    page_num     INTEGER NOT NULL,
    engine       TEXT NOT NULL,
    text         TEXT,
    score        REAL,
    processed_at TEXT NOT NULL,
    PRIMARY KEY (pdf_stem, page_num)
);

CREATE TABLE IF NOT EXISTS quality (
    pdf_stem            TEXT PRIMARY KEY,
    score               REAL,
    chars               INTEGER,
    pct_swe             REAL,
    junk_ratio          REAL,
    short_word_ratio    REAL,
    long_word_ratio     REAL,
    digit_in_word_ratio REAL,
    avg_word_len        REAL,
    vowel_ratio         REAL,
    source_type         TEXT,
    text_mtime          REAL,
    scored_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quality_pages (
    pdf_stem    TEXT NOT NULL,
    page_num    INTEGER NOT NULL,
    score       REAL,
    chars       INTEGER,
    image_page  INTEGER,
    payload     TEXT,
    scored_at   TEXT NOT NULL,
    PRIMARY KEY (pdf_stem, page_num)
);

CREATE TABLE IF NOT EXISTS ingest (
    pdf_stem    TEXT PRIMARY KEY,
    text_mtime  REAL NOT NULL,
    chunks      INTEGER,
    indexed_at  TEXT NOT NULL
);
"""


def init_schema(conn: sqlite3.Connection) -> None:
    """Skapa tabeller om de saknas och registrera schemaversion."""
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (?, ?)",
        (SCHEMA_VERSION, now()),
    )
    conn.commit()
```

- [ ] **Step 4: Kör testen**

Run: `.venv/bin/pytest tests/test_db.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/db.py tests/test_db.py
git commit -m "feat(db): grundläggande SQLite-state med schema + init"
```

---

## Task 2: `db.py` — downloads + pdf_files-domänfunktioner

**Files:**
- Modify: `src/db.py`
- Test: `tests/test_db.py`

- [ ] **Step 1: Skriv tester för downloads + pdf_files**

Lägg till i `tests/test_db.py`:

```python
from db import (
    record_download, is_downloaded, find_download_by_sha1,
    upsert_pdf_file, get_pdf_file, mark_redaction_checked,
    mark_merged, mark_normalized,
)


def _fresh(tmp_path):
    conn = connect(tmp_path / "state.db")
    init_schema(conn)
    return conn


def test_downloads_roundtrip(tmp_path):
    conn = _fresh(tmp_path)
    record_download(conn, source="files", drive_id="abc",
                    filename="00001-0001.pdf", sha1="deadbeef",
                    bytes_=1234)
    assert is_downloaded(conn, source="files", drive_id="abc")
    assert not is_downloaded(conn, source="files", drive_id="xyz")
    hit = find_download_by_sha1(conn, "deadbeef")
    assert hit["filename"] == "00001-0001.pdf"


def test_record_download_is_upsert(tmp_path):
    conn = _fresh(tmp_path)
    record_download(conn, source="files", drive_id="abc",
                    filename="a.pdf", sha1="x", bytes_=1)
    record_download(conn, source="files", drive_id="abc",
                    filename="a.pdf", sha1="x", bytes_=1, note="updated")
    rows = list(conn.execute("SELECT note FROM downloads"))
    assert len(rows) == 1
    assert rows[0]["note"] == "updated"


def test_pdf_file_status_transitions(tmp_path):
    conn = _fresh(tmp_path)
    upsert_pdf_file(conn, pdf_stem="00001-0001", source="files",
                    pdf_path="downloaded/files/00001-0001.pdf")
    row = get_pdf_file(conn, "00001-0001")
    assert row["redaction_checked_at"] is None
    assert row["merged_at"] is None
    mark_redaction_checked(conn, "00001-0001", has_redactions=True)
    mark_merged(conn, "00001-0001", text_mtime=123.0)
    mark_normalized(conn, "00001-0001", text_mtime=124.0)
    row = get_pdf_file(conn, "00001-0001")
    assert row["has_redactions"] == 1
    assert row["merged_at"] is not None
    assert row["normalized_at"] is not None
    assert row["text_mtime"] == 124.0
```

- [ ] **Step 2: Verifiera fail**

Run: `.venv/bin/pytest tests/test_db.py -v`
Expected: ImportError för nya namnen.

- [ ] **Step 3: Lägg till funktionerna i `src/db.py`**

```python
# --- downloads --------------------------------------------------------

def record_download(
    conn: sqlite3.Connection,
    *,
    source: str,
    filename: str,
    drive_id: str | None = None,
    url: str | None = None,
    sha1: str | None = None,
    bytes_: int | None = None,
    note: str | None = None,
) -> None:
    """Skriv eller uppdatera en download-rad (UPSERT på source+drive_id/url)."""
    if drive_id is None and url is None:
        raise ValueError("record_download kräver antingen drive_id eller url")
    conn.execute(
        """
        INSERT INTO downloads(source, drive_id, url, filename, sha1, bytes,
                              downloaded_at, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source, COALESCE(drive_id, url)) DO UPDATE SET
            filename      = excluded.filename,
            sha1          = excluded.sha1,
            bytes         = excluded.bytes,
            downloaded_at = excluded.downloaded_at,
            note          = excluded.note
        """,
        (source, drive_id, url, filename, sha1, bytes_, now(), note),
    )
    conn.commit()


def is_downloaded(
    conn: sqlite3.Connection,
    *,
    source: str,
    drive_id: str | None = None,
    url: str | None = None,
) -> bool:
    if drive_id is not None:
        row = conn.execute(
            "SELECT 1 FROM downloads WHERE source=? AND drive_id=?",
            (source, drive_id),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT 1 FROM downloads WHERE source=? AND url=?",
            (source, url),
        ).fetchone()
    return row is not None


def find_download_by_sha1(
    conn: sqlite3.Connection, sha1: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM downloads WHERE sha1=? LIMIT 1", (sha1,)
    ).fetchone()


# --- pdf_files --------------------------------------------------------

def upsert_pdf_file(
    conn: sqlite3.Connection,
    *,
    pdf_stem: str,
    source: str,
    pdf_path: str,
) -> None:
    conn.execute(
        """
        INSERT INTO pdf_files(pdf_stem, source, pdf_path)
        VALUES (?, ?, ?)
        ON CONFLICT(pdf_stem) DO UPDATE SET
            source   = excluded.source,
            pdf_path = excluded.pdf_path
        """,
        (pdf_stem, source, pdf_path),
    )
    conn.commit()


def get_pdf_file(
    conn: sqlite3.Connection, pdf_stem: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM pdf_files WHERE pdf_stem=?", (pdf_stem,)
    ).fetchone()


def mark_redaction_checked(
    conn: sqlite3.Connection, pdf_stem: str, *, has_redactions: bool
) -> None:
    conn.execute(
        """UPDATE pdf_files
           SET redaction_checked_at=?, has_redactions=?
           WHERE pdf_stem=?""",
        (now(), 1 if has_redactions else 0, pdf_stem),
    )
    conn.commit()


def redaction_checked(conn: sqlite3.Connection, pdf_stem: str) -> bool:
    row = conn.execute(
        "SELECT redaction_checked_at FROM pdf_files WHERE pdf_stem=?",
        (pdf_stem,),
    ).fetchone()
    return bool(row and row["redaction_checked_at"])


def mark_merged(
    conn: sqlite3.Connection, pdf_stem: str, *, text_mtime: float
) -> None:
    conn.execute(
        "UPDATE pdf_files SET merged_at=?, text_mtime=? WHERE pdf_stem=?",
        (now(), text_mtime, pdf_stem),
    )
    conn.commit()


def mark_normalized(
    conn: sqlite3.Connection, pdf_stem: str, *, text_mtime: float
) -> None:
    conn.execute(
        "UPDATE pdf_files SET normalized_at=?, text_mtime=? WHERE pdf_stem=?",
        (now(), text_mtime, pdf_stem),
    )
    conn.commit()
```

- [ ] **Step 4: Kör tester**

Run: `.venv/bin/pytest tests/test_db.py -v`
Expected: ALLA PASS.

- [ ] **Step 5: Commit**

```bash
git add src/db.py tests/test_db.py
git commit -m "feat(db): downloads + pdf_files-domänfunktioner"
```

---

## Task 3: `db.py` — pdf_pages + quality + ingest + delta-queries

**Files:**
- Modify: `src/db.py`
- Test: `tests/test_db.py`

- [ ] **Step 1: Skriv tester**

Lägg till:

```python
from db import (
    record_page, page_exists, get_pages_for_stem,
    record_quality, record_quality_page, get_bad_pages,
    record_ingest, get_ingested_mtime,
    files_needing_normalize, files_needing_quality, files_needing_ingest,
)


def test_pages_roundtrip(tmp_path):
    conn = _fresh(tmp_path)
    upsert_pdf_file(conn, pdf_stem="s1", source="files",
                    pdf_path="downloaded/files/s1.pdf")
    record_page(conn, pdf_stem="s1", page_num=1, engine="tesseract",
                text="hej", score=80.0)
    assert page_exists(conn, "s1", 1)
    assert not page_exists(conn, "s1", 2)
    pages = get_pages_for_stem(conn, "s1")
    assert [p["page_num"] for p in pages] == [1]


def test_record_page_is_upsert(tmp_path):
    conn = _fresh(tmp_path)
    upsert_pdf_file(conn, pdf_stem="s1", source="files", pdf_path="x")
    record_page(conn, pdf_stem="s1", page_num=1, engine="tesseract",
                text="a", score=50.0)
    record_page(conn, pdf_stem="s1", page_num=1, engine="surya",
                text="b", score=90.0)
    pages = get_pages_for_stem(conn, "s1")
    assert pages[0]["engine"] == "surya"
    assert pages[0]["text"] == "b"


def test_quality_and_delta(tmp_path):
    conn = _fresh(tmp_path)
    upsert_pdf_file(conn, pdf_stem="s1", source="files", pdf_path="x")
    mark_normalized(conn, "s1", text_mtime=100.0)
    assert "s1" in files_needing_quality(conn)
    record_quality(conn, pdf_stem="s1", score=70.0, chars=1000,
                   text_mtime=100.0, extras={"pct_swe": 0.9})
    assert "s1" not in files_needing_quality(conn)
    mark_normalized(conn, "s1", text_mtime=200.0)
    assert "s1" in files_needing_quality(conn)


def test_ingest_delta(tmp_path):
    conn = _fresh(tmp_path)
    upsert_pdf_file(conn, pdf_stem="s1", source="files", pdf_path="x")
    mark_normalized(conn, "s1", text_mtime=100.0)
    assert "s1" in files_needing_ingest(conn)
    record_ingest(conn, pdf_stem="s1", text_mtime=100.0, chunks=5)
    assert "s1" not in files_needing_ingest(conn)
    assert get_ingested_mtime(conn, "s1") == 100.0


def test_bad_pages(tmp_path):
    conn = _fresh(tmp_path)
    upsert_pdf_file(conn, pdf_stem="s1", source="files", pdf_path="x")
    record_quality_page(conn, pdf_stem="s1", page_num=1, score=20.0)
    record_quality_page(conn, pdf_stem="s1", page_num=2, score=80.0)
    bad = get_bad_pages(conn, threshold=50.0)
    assert [(b["pdf_stem"], b["page_num"]) for b in bad] == [("s1", 1)]
```

- [ ] **Step 2: Kör (fail)**

Run: `.venv/bin/pytest tests/test_db.py -v`

- [ ] **Step 3: Implementera funktionerna**

Lägg till i `src/db.py`:

```python
import json as _json

# --- pdf_pages --------------------------------------------------------

def record_page(
    conn: sqlite3.Connection, *,
    pdf_stem: str, page_num: int, engine: str,
    text: str | None, score: float | None,
) -> None:
    conn.execute(
        """
        INSERT INTO pdf_pages(pdf_stem, page_num, engine, text, score, processed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(pdf_stem, page_num) DO UPDATE SET
            engine       = excluded.engine,
            text         = excluded.text,
            score        = excluded.score,
            processed_at = excluded.processed_at
        """,
        (pdf_stem, page_num, engine, text, score, now()),
    )
    conn.commit()


def page_exists(conn: sqlite3.Connection, pdf_stem: str, page_num: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM pdf_pages WHERE pdf_stem=? AND page_num=?",
        (pdf_stem, page_num),
    ).fetchone() is not None


def get_pages_for_stem(
    conn: sqlite3.Connection, pdf_stem: str
) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM pdf_pages WHERE pdf_stem=? ORDER BY page_num",
        (pdf_stem,),
    ))


# --- quality ----------------------------------------------------------

_QUALITY_COLS = (
    "pct_swe", "junk_ratio", "short_word_ratio", "long_word_ratio",
    "digit_in_word_ratio", "avg_word_len", "vowel_ratio", "source_type",
)


def record_quality(
    conn: sqlite3.Connection, *,
    pdf_stem: str, score: float, chars: int,
    text_mtime: float, extras: dict | None = None,
) -> None:
    extras = extras or {}
    cols = ["pdf_stem", "score", "chars", "text_mtime", "scored_at"]
    vals = [pdf_stem, score, chars, text_mtime, now()]
    for c in _QUALITY_COLS:
        cols.append(c)
        vals.append(extras.get(c))
    placeholders = ",".join("?" * len(vals))
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "pdf_stem")
    conn.execute(
        f"""INSERT INTO quality({','.join(cols)}) VALUES ({placeholders})
            ON CONFLICT(pdf_stem) DO UPDATE SET {updates}""",
        vals,
    )
    conn.commit()


def record_quality_page(
    conn: sqlite3.Connection, *,
    pdf_stem: str, page_num: int, score: float,
    chars: int | None = None, image_page: bool = False,
    payload: dict | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO quality_pages(pdf_stem, page_num, score, chars,
                                  image_page, payload, scored_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(pdf_stem, page_num) DO UPDATE SET
            score=excluded.score, chars=excluded.chars,
            image_page=excluded.image_page,
            payload=excluded.payload, scored_at=excluded.scored_at
        """,
        (pdf_stem, page_num, score, chars,
         1 if image_page else 0,
         _json.dumps(payload) if payload else None,
         now()),
    )
    conn.commit()


def get_bad_pages(
    conn: sqlite3.Connection, *, threshold: float
) -> list[sqlite3.Row]:
    return list(conn.execute(
        """SELECT pdf_stem, page_num, score, payload
           FROM quality_pages
           WHERE score < ? AND COALESCE(image_page, 0) = 0
           ORDER BY score ASC""",
        (threshold,),
    ))


# --- ingest -----------------------------------------------------------

def record_ingest(
    conn: sqlite3.Connection, *,
    pdf_stem: str, text_mtime: float, chunks: int,
) -> None:
    conn.execute(
        """
        INSERT INTO ingest(pdf_stem, text_mtime, chunks, indexed_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(pdf_stem) DO UPDATE SET
            text_mtime=excluded.text_mtime,
            chunks=excluded.chunks,
            indexed_at=excluded.indexed_at
        """,
        (pdf_stem, text_mtime, chunks, now()),
    )
    conn.commit()


def get_ingested_mtime(
    conn: sqlite3.Connection, pdf_stem: str
) -> float | None:
    row = conn.execute(
        "SELECT text_mtime FROM ingest WHERE pdf_stem=?", (pdf_stem,)
    ).fetchone()
    return row["text_mtime"] if row else None


# --- delta-queries (inkrementell logik) -------------------------------

def files_needing_normalize(conn: sqlite3.Connection) -> list[str]:
    """pdf_stems vars text_mtime > normalized_at (eller aldrig normaliserade)."""
    rows = conn.execute(
        """SELECT pdf_stem FROM pdf_files
           WHERE text_mtime IS NOT NULL
             AND (normalized_at IS NULL
                  OR text_mtime > strftime('%s', normalized_at))"""
    )
    return [r["pdf_stem"] for r in rows]


def files_needing_quality(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """SELECT pf.pdf_stem FROM pdf_files pf
           LEFT JOIN quality q USING (pdf_stem)
           WHERE pf.text_mtime IS NOT NULL
             AND (q.pdf_stem IS NULL OR pf.text_mtime > q.text_mtime)"""
    )
    return [r["pdf_stem"] for r in rows]


def files_needing_ingest(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """SELECT pf.pdf_stem FROM pdf_files pf
           LEFT JOIN ingest i USING (pdf_stem)
           WHERE pf.text_mtime IS NOT NULL
             AND (i.pdf_stem IS NULL OR pf.text_mtime > i.text_mtime)"""
    )
    return [r["pdf_stem"] for r in rows]
```

- [ ] **Step 4: Kör tester**

Run: `.venv/bin/pytest tests/test_db.py -v`
Expected: ALLA PASS.

- [ ] **Step 5: Concurrency-test**

Lägg till i `tests/test_db.py`:

```python
import threading


def test_parallel_page_writes(tmp_path):
    """4 trådar skriver olika sidor — ska inte krascha eller tappa data."""
    db_path = tmp_path / "state.db"
    init_conn = connect(db_path)
    init_schema(init_conn)
    upsert_pdf_file(init_conn, pdf_stem="s1", source="files", pdf_path="x")
    init_conn.close()

    errors = []

    def worker(page_range):
        try:
            c = connect(db_path)
            for n in page_range:
                record_page(c, pdf_stem="s1", page_num=n,
                            engine="tesseract", text=f"p{n}", score=80.0)
            c.close()
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(range(i*25+1, i*25+26),))
               for i in range(4)]
    for t in threads: t.start()
    for t in threads: t.join()

    assert not errors
    c = connect(db_path)
    n = c.execute("SELECT COUNT(*) FROM pdf_pages").fetchone()[0]
    assert n == 100
```

Run: `.venv/bin/pytest tests/test_db.py -v`
Expected: ALLA PASS.

- [ ] **Step 6: Commit**

```bash
git add src/db.py tests/test_db.py
git commit -m "feat(db): pages, quality, ingest + delta-queries + concurrency"
```

---

## Task 4: `src/migrate_to_db.py` — engångsskript

**Files:**
- Create: `src/migrate_to_db.py`, `migrate_to_db.sh`
- Test: `tests/test_migrate_to_db.py`

- [ ] **Step 1: Skriv test med fixture-`generated/`**

Skapa `tests/test_migrate_to_db.py`:

```python
import csv
import json
from pathlib import Path

from db import connect, init_schema
from migrate_to_db import migrate


def _make_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def test_migrate_full_fixture(tmp_path):
    root = tmp_path
    # downloads
    _make_csv(root / "downloaded/files/manifest.csv",
              [{"drive_id": "a", "filename": "00001-0001.pdf",
                "sha1": "h1", "downloaded_at": "2026-01-01T00:00:00",
                "bytes": "1000"}],
              ["drive_id", "filename", "sha1", "downloaded_at", "bytes"])
    (root / "downloaded/files/00001-0001.pdf").write_bytes(b"%PDF-fake")

    # OCR-text + redaktionsmarkör
    (root / "generated/text").mkdir(parents=True)
    txt = root / "generated/text/00001-0001.txt"
    txt.write_text("hej världen", encoding="utf-8")
    (root / "generated/text/00001-0001.redact").write_text("1")
    stamp = root / "generated/text/.normalize_stamp"
    stamp.write_text("")  # mtime = now

    # quality
    _make_csv(root / "generated/quality.csv",
              [{"file": "00001-0001.txt", "source": "ocr", "score": "75.5",
                "chars": "11", "pct_swe": "0.8", "junk_ratio": "0.1",
                "short_word_ratio": "0.1", "long_word_ratio": "0.0",
                "digit_in_word_ratio": "0.0", "avg_word_len": "4.5",
                "vowel_ratio": "0.4"}],
              ["file", "source", "score", "chars", "pct_swe",
               "junk_ratio", "short_word_ratio", "long_word_ratio",
               "digit_in_word_ratio", "avg_word_len", "vowel_ratio"])
    (root / "generated/quality_pages.jsonl").write_text(
        json.dumps({"file": "00001-0001.txt", "page": 1, "score": 60.0,
                    "chars": 11}) + "\n",
        encoding="utf-8")

    # per-sida-markörer
    sd = root / "generated/text_pages/00001-0001"
    sd.mkdir(parents=True)
    (sd / "page-001.json").write_text(json.dumps(
        {"file": "00001-0001.pdf", "page": 1, "engine": "tesseract",
         "score": 70.0, "chars": 11}))

    db_path = root / "generated/state.db"
    conn = connect(db_path)
    init_schema(conn)

    stats = migrate(conn, root)
    assert stats["downloads"] == 1
    assert stats["pdf_files"] == 1
    assert stats["pdf_pages"] == 1
    assert stats["quality"] == 1
    assert stats["quality_pages"] == 1

    row = conn.execute(
        "SELECT * FROM pdf_files WHERE pdf_stem='00001-0001'"
    ).fetchone()
    assert row["has_redactions"] == 1
    assert row["normalized_at"] is not None
    assert row["text_mtime"] is not None
```

- [ ] **Step 2: Implementera `src/migrate_to_db.py`**

```python
"""Engångsmigrering: läs befintliga filer i downloaded/ och generated/ och
fyll state.db. Idempotent (UPSERT) — kan köras flera gånger."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import db

ROOT = Path(os.environ.get("ROOT") or Path(__file__).resolve().parents[1])


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def _migrate_downloads(conn, root: Path) -> int:
    n = 0
    for source, subdir in (("files", "files"), ("wpu", "wpu_files")):
        manifest = root / "downloaded" / subdir / "manifest.csv"
        if not manifest.exists():
            continue
        with manifest.open(encoding="utf-8", newline="") as fp:
            for row in csv.DictReader(fp):
                drive_id = row.get("drive_id") or None
                url = row.get("url") or None
                if not drive_id and not url:
                    continue
                db.record_download(
                    conn, source=source, drive_id=drive_id, url=url,
                    filename=row.get("filename", ""),
                    sha1=row.get("sha1") or None,
                    bytes_=int(row["bytes"]) if row.get("bytes") else None,
                )
                n += 1
    return n


def _migrate_pdf_files(conn, root: Path) -> int:
    n = 0
    for source, subdir in (("files", "files"), ("wpu", "wpu_files")):
        pdf_dir = root / "downloaded" / subdir
        if not pdf_dir.exists():
            continue
        for pdf in sorted(pdf_dir.glob("*.pdf")):
            db.upsert_pdf_file(
                conn, pdf_stem=pdf.stem, source=source,
                pdf_path=str(pdf.relative_to(root)),
            )
            n += 1
    return n


def _migrate_redactions_and_text(conn, root: Path) -> None:
    """text/<stem>.redact → has_redactions. text/<stem>.txt → text_mtime + merged_at.
    .normalize_stamp → normalized_at för filer med mtime ≤ stamp."""
    text_dir = root / "generated" / "text"
    if not text_dir.exists():
        return
    stamp = text_dir / ".normalize_stamp"
    stamp_mtime = stamp.stat().st_mtime if stamp.exists() else None

    for txt in text_dir.glob("*.txt"):
        stem = txt.stem
        if db.get_pdf_file(conn, stem) is None:
            continue
        mtime = txt.stat().st_mtime
        db.mark_merged(conn, stem, text_mtime=mtime)
        if stamp_mtime is not None and mtime <= stamp_mtime:
            db.mark_normalized(conn, stem, text_mtime=mtime)

    for r in text_dir.glob("*.redact"):
        stem = r.stem
        if db.get_pdf_file(conn, stem) is not None:
            db.mark_redaction_checked(conn, stem, has_redactions=True)


def _migrate_pages(conn, root: Path) -> int:
    pages_root = root / "generated" / "text_pages"
    if not pages_root.exists():
        return 0
    n = 0
    for stem_dir in sorted(pages_root.iterdir()):
        if not stem_dir.is_dir():
            continue
        stem = stem_dir.name
        if db.get_pdf_file(conn, stem) is None:
            continue
        for j in sorted(stem_dir.glob("page-*.json")):
            try:
                meta = json.loads(j.read_text(encoding="utf-8"))
            except Exception:
                continue
            page_num = int(meta.get("page", 0))
            if page_num < 1:
                continue
            txt_path = j.with_suffix(".txt")
            text = txt_path.read_text(encoding="utf-8", errors="replace") \
                if txt_path.exists() else None
            db.record_page(
                conn, pdf_stem=stem, page_num=page_num,
                engine=meta.get("engine", "tesseract"),
                text=text, score=meta.get("score"),
            )
            n += 1
    return n


def _migrate_quality(conn, root: Path) -> int:
    csv_path = root / "generated" / "quality.csv"
    if not csv_path.exists():
        return 0
    text_dir = root / "generated" / "text"
    n = 0
    with csv_path.open(encoding="utf-8", newline="") as fp:
        for row in csv.DictReader(fp):
            fname = row.get("file", "")
            stem = Path(fname).stem
            if db.get_pdf_file(conn, stem) is None:
                continue
            txt = text_dir / fname
            text_mtime = txt.stat().st_mtime if txt.exists() else 0.0
            extras = {k: (float(row[k]) if row.get(k) else None)
                      for k in ("pct_swe", "junk_ratio", "short_word_ratio",
                                "long_word_ratio", "digit_in_word_ratio",
                                "avg_word_len", "vowel_ratio")}
            extras["source_type"] = row.get("source")
            db.record_quality(
                conn, pdf_stem=stem,
                score=float(row.get("score") or 0),
                chars=int(row.get("chars") or 0),
                text_mtime=text_mtime,
                extras=extras,
            )
            n += 1
    return n


def _migrate_quality_pages(conn, root: Path) -> int:
    jsonl = root / "generated" / "quality_pages.jsonl"
    if not jsonl.exists():
        return 0
    n = 0
    with jsonl.open(encoding="utf-8") as fp:
        for line in fp:
            try:
                d = json.loads(line)
            except Exception:
                continue
            stem = Path(d.get("file", "")).stem
            page = int(d.get("page", 0))
            if not stem or page < 1:
                continue
            if db.get_pdf_file(conn, stem) is None:
                continue
            db.record_quality_page(
                conn, pdf_stem=stem, page_num=page,
                score=float(d.get("score") or 0),
                chars=int(d.get("chars") or 0) if d.get("chars") else None,
                image_page=bool(d.get("image_page")),
                payload=d,
            )
            n += 1
    return n


def _migrate_ingest(conn, root: Path) -> int:
    """Läs (source, mtime) från LanceDB-tabellen om den finns."""
    db_dir = root / "generated" / "lancedb"
    if not db_dir.exists():
        return 0
    try:
        import lancedb  # type: ignore
    except ImportError:
        return 0
    try:
        ldb = lancedb.connect(str(db_dir))
        if "chunks" not in [t for t in (ldb.list_tables().tables
                                        if hasattr(ldb.list_tables(), "tables")
                                        else ldb.list_tables())]:
            return 0
        tbl = ldb.open_table("chunks")
        arrow = tbl.to_lance().to_table(columns=["source", "mtime"])
        sources = arrow.column("source").to_pylist()
        mtimes = arrow.column("mtime").to_pylist()
    except Exception:
        return 0

    per_stem: dict[str, tuple[float, int]] = {}
    for s, m in zip(sources, mtimes):
        stem = Path(s).stem
        prev = per_stem.get(stem)
        if not prev or m > prev[0]:
            per_stem[stem] = (float(m), (prev[1] if prev else 0) + 1)
        else:
            per_stem[stem] = (prev[0], prev[1] + 1)

    n = 0
    for stem, (m, chunks) in per_stem.items():
        if db.get_pdf_file(conn, stem) is None:
            continue
        if m <= 0:
            continue
        db.record_ingest(conn, pdf_stem=stem, text_mtime=m, chunks=chunks)
        n += 1
    return n


def migrate(conn, root: Path) -> dict[str, int]:
    return {
        "downloads": _migrate_downloads(conn, root),
        "pdf_files": _migrate_pdf_files(conn, root),
        "pdf_pages": _migrate_pages(conn, root),
        "quality": _migrate_quality(conn, root),
        "quality_pages": _migrate_quality_pages(conn, root),
        "ingest": _migrate_ingest(conn, root),
        # _migrate_redactions_and_text körs som biverkning (mark_merged/normalized)
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=str(ROOT), help="projektrot")
    ap.add_argument("--db", default=None,
                    help="path till state.db (default: <root>/generated/state.db)")
    args = ap.parse_args()

    root = Path(args.root)
    db_path = Path(args.db) if args.db else root / "generated" / "state.db"
    conn = db.connect(db_path)
    db.init_schema(conn)
    _migrate_redactions_and_text(conn, root)  # innan _migrate_quality
    stats = migrate(conn, root)
    print(f"Migrerade till {db_path}:")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Anpassa testen så att `_migrate_redactions_and_text` körs före `migrate`:
ersätt `migrate(conn, root)` i testet med:
```python
from migrate_to_db import _migrate_redactions_and_text
_migrate_redactions_and_text(conn, root)
stats = migrate(conn, root)
```

- [ ] **Step 3: Kör test**

Run: `.venv/bin/pytest tests/test_migrate_to_db.py -v`
Expected: PASS.

- [ ] **Step 4: Skapa `migrate_to_db.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
source .venv/bin/activate
exec python src/migrate_to_db.py "$@"
```

Run: `chmod +x migrate_to_db.sh`

- [ ] **Step 5: Commit**

```bash
git add src/migrate_to_db.py tests/test_migrate_to_db.py migrate_to_db.sh
git commit -m "feat(migrate): engångsmigrering av filstate → state.db"
```

---

## Task 5: `download.py` → använda `db.downloads`

**Files:**
- Modify: `src/download.py`
- Modify: `tests/test_download.py`

- [ ] **Step 1: Läs befintliga `test_download.py` för att förstå fixture**

Run: `cat tests/test_download.py | head -50`

- [ ] **Step 2: Lägg till parameteriserbar `conn` i download.py**

Modifiera `src/download.py:200-230`: ta bort `load_manifest`/`append_manifest`-funktionerna och importera `db`:

```python
import db as state_db
```

Modifiera `main()` (`src/download.py:229-397`):
- Ersätt `manifest_path = out_dir / MANIFEST_NAME` med:
  ```python
  conn = state_db.connect()
  state_db.init_schema(conn)
  source = "wpu" if out_dir.name == "wpu_files" else "files"
  ```
- Ersätt `manifest = load_manifest(...)`, `manifest_ids = ...`, `manifest_sha1s = ...` med:
  ```python
  manifest_ids = {
      r["drive_id"] for r in conn.execute(
          "SELECT drive_id FROM downloads WHERE source=? AND drive_id IS NOT NULL",
          (source,),
      )
  }
  manifest_sha1s = {
      r["sha1"]: r["filename"] for r in conn.execute(
          "SELECT sha1, filename FROM downloads WHERE source=? AND sha1 IS NOT NULL",
          (source,),
      )
  }
  ```
- Ersätt `append_manifest(manifest_path, {...})` med:
  ```python
  state_db.record_download(
      conn, source=source, drive_id=file_id,
      filename=dst.name, sha1=sha1, bytes_=size,
  )
  ```
  och motsvarande för dubblett-grenen (med `filename=other` + `note=f"dup-of:{other}"`).

- [ ] **Step 3: Behåll manifest.csv-skrivningen tills migreringen verifierats**

Inte denna iteration — efter migrate-cleanup-task tas konstanter `MANIFEST_NAME` / `MANIFEST_FIELDS` bort.

- [ ] **Step 4: Uppdatera `tests/test_download.py`**

Använd `monkeypatch` på `STATE_DB`-env-var så testen pekar på `tmp_path/state.db`:
```python
def test_download(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    # ... resten
```
Verifiera mot `db`-tabellen i stället för `manifest.csv`.

- [ ] **Step 5: Kör test**

Run: `.venv/bin/pytest tests/test_download.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/download.py tests/test_download.py
git commit -m "refactor(download): använd state.db i stället för manifest.csv"
```

---

## Task 6: `ocr_pages.py` → `db.pdf_pages` + `db.mark_redaction_checked`

**Files:**
- Modify: `src/ocr_pages.py`
- Test: `tests/test_detect_redactions.py`

- [ ] **Step 1: Importera db**

I `src/ocr_pages.py` toppen:
```python
import db as state_db
```

- [ ] **Step 2: Ersätt idempotens-markör `page-NNN.json` med db-lookup**

Lokalisera `src/ocr_pages.py:450-460` (där `json_path.exists()` används som markör). Skapa connection en gång före loopen:

```python
conn = state_db.connect()
state_db.init_schema(conn)
state_db.upsert_pdf_file(conn, pdf_stem=pdf.stem,
                         source=("wpu" if "wpu" in str(pdf) else "files"),
                         pdf_path=str(pdf))
```

Ersätt `if json_path.exists():` med `if state_db.page_exists(conn, pdf.stem, page_num):`.

Ersätt slutet av sid-loopen (`txt_path.write_text(...)` + `json_path.write_text(...)`) med:
```python
state_db.record_page(
    conn, pdf_stem=pdf.stem, page_num=page_num,
    engine=args.engine, text=text, score=scored.get("score"),
)
```

Behåll `txt_path.write_text(text, ...)` ENDAST om merge_pages fortfarande läser därifrån (Task 7 byter den till db också). Ordning: gör Task 7 i samma commit eller behåll temporärt.

- [ ] **Step 3: detect-only-grenen → `mark_redaction_checked`**

`src/ocr_pages.py:411-418`: efter `n = detect_redactions_file(...)` lägg till:
```python
conn = state_db.connect()
state_db.init_schema(conn)
state_db.upsert_pdf_file(conn, pdf_stem=pdf.stem,
                         source=("wpu" if "wpu" in str(pdf) else "files"),
                         pdf_path=str(pdf))
state_db.mark_redaction_checked(conn, pdf.stem, has_redactions=(n > 0))
```
Behåll `marker = txt_dir / f"{pdf.stem}.redact"`-skapandet i `detect_redactions_file` tills `detect_redactions.sh` slutar grep:a på .redact (Task 10).

- [ ] **Step 4: Kör befintliga tester**

Run: `.venv/bin/pytest tests/test_detect_redactions.py -v`
Behöver troligen fixture med `STATE_DB`-env. Uppdatera testen att peka på `tmp_path/state.db`.

- [ ] **Step 5: Commit**

```bash
git add src/ocr_pages.py tests/test_detect_redactions.py
git commit -m "refactor(ocr_pages): pdf_pages + mark_redaction_checked via db"
```

---

## Task 7: `merge_pages.py` → läs sidor från `db.pdf_pages`

**Files:**
- Modify: `src/merge_pages.py`
- Modify: `tests/test_merge_pages.py`

- [ ] **Step 1: Ersätt `find_updates`-disk-läsning med db-läsning**

I `src/merge_pages.py:54-67`, ändra `find_updates(stem_dir: Path)` så den i stället tar en `conn`:

```python
def find_updates(conn, pdf_stem: str) -> dict[int, str]:
    out: dict[int, str] = {}
    for row in conn.execute(
        "SELECT page_num, text FROM pdf_pages WHERE pdf_stem=? AND text IS NOT NULL",
        (pdf_stem,),
    ):
        out[row["page_num"]] = row["text"]
    return out
```

- [ ] **Step 2: Uppdatera `merge_one`**

Ersätt `updates = find_updates(stem_dir)` med:
```python
import db as state_db
conn = state_db.connect()
state_db.init_schema(conn)
updates = find_updates(conn, stem)
```

Efter lyckad merge: `state_db.mark_merged(conn, stem, text_mtime=txt_path.stat().st_mtime)`.

Ta bort `.png`/`.txt`-cleanup-loopen — den är överflödig när sidorna kommer från db. Lämna kvar borttagning av legacy `text_pages/<stem>.txt`.

- [ ] **Step 3: `--all`-läget itererar över db i stället för disk**

```python
stems = sorted({r["pdf_stem"] for r in conn.execute(
    "SELECT DISTINCT pdf_stem FROM pdf_pages")})
```

- [ ] **Step 4: Uppdatera test_merge_pages.py**

Skapa fixture som fyller `pdf_pages` + tom `text/<stem>.txt` och verifierar att `merge_one` skriver rätt innehåll.

- [ ] **Step 5: Kör**

Run: `.venv/bin/pytest tests/test_merge_pages.py -v`

- [ ] **Step 6: Commit**

```bash
git add src/merge_pages.py tests/test_merge_pages.py
git commit -m "refactor(merge_pages): läs sidor från db i stället för disk"
```

---

## Task 8: `normalize_text.py` → använda `db.mark_normalized`

**Files:**
- Modify: `src/normalize_text.py`
- Modify: `tests/test_normalize_text.py`

- [ ] **Step 1: Ta bort stamp-fil-logiken**

I `src/normalize_text.py:130-147`: ta bort `stamp = txt_dir / '.normalize_stamp'` och hela `since`-blocket. Ersätt med:

```python
import db as state_db
conn = state_db.connect()
state_db.init_schema(conn)

if args.files_from:
    # befintlig logik
    ...
else:
    needing = set(state_db.files_needing_normalize(conn))
    files = [f for f in all_files if f.stem in needing] \
            if not args.rebuild else all_files
    skipped = len(all_files) - len(files)
```

I slutet (`src/normalize_text.py:184-185`), efter lyckad körning per fil, anropa:
```python
state_db.mark_normalized(conn, f.stem, text_mtime=f.stat().st_mtime)
```
Ta bort `stamp.touch()`-anropet.

**Viktigt:** `files_needing_normalize` returnerar bara stems som har `text_mtime IS NOT NULL` (dvs som är mergade). Säkerställ att `merge_one` (Task 7) sätter text_mtime — det gör det redan via `mark_merged`.

- [ ] **Step 2: Uppdatera tests**

Lägg till fixture som först `mark_merged` på en stem, sedan kör `process_file`, sedan verifierar `mark_normalized` är satt.

- [ ] **Step 3: Kör**

Run: `.venv/bin/pytest tests/test_normalize_text.py -v`

- [ ] **Step 4: Commit**

```bash
git add src/normalize_text.py tests/test_normalize_text.py
git commit -m "refactor(normalize): db.mark_normalized i stället för .normalize_stamp"
```

---

## Task 9: `quality.py` → läs/skriv via db

**Files:**
- Modify: `src/quality.py`
- Modify: `tests/test_quality.py`

- [ ] **Step 1: Importera db, ta bort stamp + CSV/JSONL-läsning**

I `src/quality.py`: ta bort CSV-cachen och `stamp_path`-blocket (`src/quality.py:194-243`).

Lägg till:
```python
import db as state_db
conn = state_db.connect()
state_db.init_schema(conn)

needing = set(state_db.files_needing_quality(conn))
if args.rebuild:
    files_to_score = files_all
elif args.files_from:
    # befintlig listed_names-logik mot files_all
    ...
else:
    files_to_score = [f for f in files_all if f.stem in needing]
```

- [ ] **Step 2: Skriv resultat till db i stället för CSV/JSONL**

I huvudloopen (`src/quality.py:295-327`), efter `scored = score_text(...)`:
```python
text_mtime = f.stat().st_mtime
extras = {k: scored.get(k) for k in (
    "pct_swe", "junk_ratio", "short_word_ratio", "long_word_ratio",
    "digit_in_word_ratio", "avg_word_len", "vowel_ratio")}
extras["source_type"] = "text-layer" if original_had_text(f.stem, files_dir) else "ocr"
state_db.record_quality(
    conn, pdf_stem=f.stem,
    score=scored["score"], chars=scored["chars"],
    text_mtime=text_mtime, extras=extras,
)
if args.per_page:
    pages = text.split("\f") if "\f" in text else [text]
    for p_idx, page_text in enumerate(pages, start=1):
        p_scored = score_text(page_text, use_hunspell=False)
        alnum = sum(1 for c in page_text if c.isalnum())
        image_page = alnum < MIN_PAGE_ALNUM
        state_db.record_quality_page(
            conn, pdf_stem=f.stem, page_num=p_idx,
            score=(100.0 if image_page else p_scored["score"]),
            chars=p_scored["chars"], image_page=image_page,
            payload=p_scored,
        )
```

- [ ] **Step 3: Ta bort CSV/JSONL-output-blocket**

Hela `src/quality.py:339-360` (CSV-skrivning + JSONL-skrivning) tas bort. `--out` och `--pages-out` används inte längre — markera som deprecated (varning vid användning, ingen effekt). Ta bort `stamp_path.touch()`-anropet.

- [ ] **Step 4: Skapa exportkommando för bakåtkompatibilitet med webui**

Webui läser `quality.csv` idag — kontrollera med `grep -n "quality.csv\|quality_pages.jsonl" src/webui.py`. Om webui läser, lägg till `--export-csv FIL` som dumpar `quality`-tabellen till samma format. Tills webui omdirigeras (utanför detta plans scope) kan denna export köras vid behov.

- [ ] **Step 5: Kör tester**

Run: `.venv/bin/pytest tests/test_quality.py -v`

- [ ] **Step 6: Commit**

```bash
git add src/quality.py tests/test_quality.py
git commit -m "refactor(quality): läs/skriv via db, slopa stamp + csv/jsonl"
```

---

## Task 10: `llm_correct.py` → läs dåliga sidor från db

**Files:**
- Modify: `src/llm_correct.py`

- [ ] **Step 1: Ersätt jsonl-läsning**

Lokalisera där `quality_pages.jsonl` läses (omkring `src/llm_correct.py:250`). Ersätt med:

```python
import db as state_db
conn = state_db.connect()
state_db.init_schema(conn)
bad = state_db.get_bad_pages(conn, threshold=args.threshold)
# bad är list[Row] med pdf_stem, page_num, score, payload
```

Anpassa nedströms-kod att använda `pdf_stem` + `page_num` i stället för `file` + `page`.

- [ ] **Step 2: Verifiera manuellt**

Run: `./llm_correct.sh --threshold 60 --dry-run` (om flaggan finns; annars `--help`).

- [ ] **Step 3: Commit**

```bash
git add src/llm_correct.py
git commit -m "refactor(llm_correct): läs dåliga sidor från db"
```

---

## Task 11: `rag/ingest.py` → `db.ingest`

**Files:**
- Modify: `src/rag/ingest.py`
- Modify: `tests/test_reingest.py`

- [ ] **Step 1: Ersätt LanceDB-mtime-uppslag med db**

I `src/rag/ingest.py:231-254`, ersätt `already`-byggandet:

```python
import db as state_db
state_conn = state_db.connect()
state_db.init_schema(state_conn)

already: dict[str, float] = {
    Path(row["pdf_stem"]).name + ".txt": row["text_mtime"]
    for row in state_conn.execute(
        "SELECT pdf_stem, text_mtime FROM ingest"
    )
}
```

Behåll `unusable_mtimes.json`-läsningen (utanför scope).

- [ ] **Step 2: Skriv till `db.ingest` efter varje fil indexerats**

I huvud-indexerings-loopen, efter `table.add(...)`, anropa:
```python
state_db.record_ingest(
    state_conn, pdf_stem=path.stem,
    text_mtime=disk_mtime, chunks=len(chunks),
)
```

Behåll mtime-kolumnen i LanceDB-schema och skriv den fortfarande (används av sökning).

- [ ] **Step 3: Kör test**

Run: `.venv/bin/pytest tests/test_reingest.py -v`

- [ ] **Step 4: Commit**

```bash
git add src/rag/ingest.py tests/test_reingest.py
git commit -m "refactor(ingest): mtime-tracking via db.ingest i stället för LanceDB-kolumn"
```

---

## Task 12: `detect_redactions.sh` → filtrera via sqlite3

**Files:**
- Modify: `detect_redactions.sh`

- [ ] **Step 1: Hitta filterblocket**

Run: `grep -n "redact\|xargs" detect_redactions.sh`

Identifiera raden som filtrerar bort filer med `.redact`-markör innan `xargs`.

- [ ] **Step 2: Ersätt med sqlite3-query**

Lägg till en helper:
```bash
already_checked() {
  sqlite3 generated/state.db \
    "SELECT pdf_stem FROM pdf_files WHERE redaction_checked_at IS NOT NULL" 2>/dev/null
}
```

Filtrera in-list-PDF-stems mot output från `already_checked`:
```bash
comm -23 <(printf '%s\n' "${all_stems[@]}" | sort -u) <(already_checked | sort -u)
```

- [ ] **Step 3: Verifiera manuellt**

Run: `./detect_redactions.sh --jobs 1 --dry-run` (lägg till `--dry-run` vid behov, eller verifiera att redan-kontrollerade filer hoppas över).

- [ ] **Step 4: Commit**

```bash
git add detect_redactions.sh
git commit -m "refactor(detect_redactions): filtrera via sqlite3 i stället för .redact-glob"
```

---

## Task 13: Skarp migrering + cleanup av gamla filer

**Files:**
- Create: `cleanup_legacy_state.sh`

- [ ] **Step 1: Kör `migrate_to_db.sh` på riktiga `generated/`**

Run: `./migrate_to_db.sh`

Förväntad output: alla counts > 0, inga fel.

- [ ] **Step 2: Verifiera räkning**

```bash
.venv/bin/python -c "
import db
c = db.connect()
for t in ('downloads','pdf_files','pdf_pages','quality','quality_pages','ingest'):
    n = c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
    print(f'{t}: {n}')
"
```

Jämför mot:
- `wc -l downloaded/files/manifest.csv` + `wc -l downloaded/wpu_files/manifest.csv`
- `ls downloaded/files/*.pdf downloaded/wpu_files/*.pdf | wc -l`
- `find generated/text_pages -name 'page-*.json' | wc -l`
- `wc -l generated/quality.csv`
- `wc -l generated/quality_pages.jsonl`

Avvikelser måste utredas innan vidare.

- [ ] **Step 3: Noop-körning**

```bash
./run_pipeline.sh
```

Inget jobb ska triggas för normalize / quality / ingest — alla rapporterar "0 nya filer".

- [ ] **Step 4: Touch-test**

```bash
touch generated/text/<någon-fil>.txt
./quality.sh
./ingest.sh
```

Bara den filen ska re-processas. Verifiera i terminalens output.

- [ ] **Step 5: Skapa cleanup-skript**

```bash
#!/usr/bin/env bash
# Tar bort gamla state-filer efter verifierad migrering.
set -euo pipefail
read -p "Radera .normalize_stamp, .quality_stamp, .redact, page-*.json, manifest.csv, quality.csv, quality_pages.jsonl? [y/N] " yn
[[ "$yn" =~ ^[Yy]$ ]] || exit 0

rm -f generated/text/.normalize_stamp
rm -f generated/.quality_stamp
rm -f generated/text/*.redact
find generated/text_pages -name 'page-*.json' -delete
rm -f downloaded/files/manifest.csv downloaded/wpu_files/manifest.csv
rm -f generated/quality.csv generated/quality_pages.jsonl
echo "Klart."
```

Run: `chmod +x cleanup_legacy_state.sh`

- [ ] **Step 6: Kör inte cleanup i samma commit**

Cleanup ska köras manuellt när användaren bekräftar att skarp pipeline fungerat i några dagar. Skriptet committas så det finns redo.

- [ ] **Step 7: Commit**

```bash
git add cleanup_legacy_state.sh
git commit -m "chore: cleanup-skript för legacy state-filer (kör manuellt efter verifiering)"
```

---

## Task 14: Dokumentation

**Files:**
- Modify: `CLAUDE.md`, `README.md`

- [ ] **Step 1: Uppdatera CLAUDE.md**

I avsnittet **Non-obvious Design Decisions**: ersätt "Stamp-filbaserad normalisering" och "Stamp-filbaserad quality-bedömning" med:

> **SQLite-state (generated/state.db)**: all operativ pipeline-state lever här — downloads, per-PDF-status (redaktion/merge/normalize), per-sida OCR-resultat, kvalitetspoäng och ingest-tracking. Inkrementell logik via `text_mtime` jämfört mot `normalized_at`/`scored_at`/etc. `--rebuild` tvingar omkörning av allt. Modulen `src/db.py` exponerar alla CRUD- och delta-queries; konsumenter skriver aldrig egen SQL. Ny pipeline efter `git pull`: kör `./migrate_to_db.sh` engångsvis.

I **Directory Structure**: lägg till `src/db.py` och `src/migrate_to_db.py`. Lägg till `generated/state.db` i lista över genererade artefakter.

I **Common Gotchas**: ta bort eventuella stamp-fil-referenser, lägg till:
> **SQLite WAL-filer**: `generated/state.db-wal` och `-shm` är normalt och syncas vid checkpoint. Säkerhetskopiera alla tre samtidigt.

- [ ] **Step 2: Uppdatera README.md**

Lägg till kort avsnitt om `state.db`:

> ## State-databas
> Alla pipeline-markörer och status lagras i `generated/state.db` (SQLite). Inspektera med `sqlite3 generated/state.db`. Migrering från gamla filmarkörer (om du uppgraderar): `./migrate_to_db.sh`.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md README.md
git commit -m "docs: dokumentera SQLite-state-arkitekturen"
```

---

## Self-Review-checklista

Innan plan körs:

1. **Spec-täckning:** Alla 6 tabeller från spec implementerade i Task 1+3? ✓ Migrering i Task 4? ✓ Alla konsumentmoduler i Task 5–11? ✓ Shell-script i Task 12? ✓ Dokumentation Task 14? ✓
2. **Placeholders:** Inga TBD/TODO. Funktioner som introduceras i tidiga tasks (`record_page`, `mark_normalized`, etc.) återanvänds konsekvent i senare tasks.
3. **Typkonsistens:** `text_mtime` är `REAL` i schema, `float` i Python överallt. `pdf_stem` är `TEXT`, `str`. `engine` är `str` med värden `tesseract`/`surya`/`vision`/`detect-only` — matchar `ocr_pages.py`-flaggor.
4. **Beroenden mellan tasks:** Task 7 (merge_pages db-read) måste komma efter Task 6 (ocr_pages db-write) — i ordning. Task 8/9/11 beror på att Task 6/7 sätter `text_mtime` via `mark_merged`. Task 13 (skarp migrering) kräver alla Task 5–12 mergeade.
