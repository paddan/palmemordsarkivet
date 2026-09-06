"""SQLite-baserad state för pipeline-tracking.

Ersätter filbaserade markörer (.normalize_stamp, .quality_stamp, .redact,
page-NNN.json, manifest.csv, quality.csv, quality_pages.jsonl, LanceDB-mtime).

Schema, åtkomstskikt och inkrementella frågor samlas här — konsumentmoduler
ska aldrig skriva egen SQL.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_DB: Path = Path(os.environ.get("STATE_DB", str(ROOT / "generated" / "db" / "state.db")))

SCHEMA_VERSION: int = 9

GRAPH_REVIEW_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS graph_review_decisions (
    item_key TEXT PRIMARY KEY,
    source_hash TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('keep', 'exclude', 'replace')),
    target_json TEXT NOT NULL,
    note TEXT NOT NULL CHECK(length(trim(note)) > 0),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS graph_review_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_key TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('keep', 'exclude', 'replace', 'reset')),
    target_json TEXT NOT NULL,
    note TEXT NOT NULL CHECK(length(trim(note)) > 0),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS graph_review_suggestions (
    item_key TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('keep', 'exclude', 'replace')),
    target_json TEXT NOT NULL,
    note TEXT NOT NULL,
    evidence TEXT NOT NULL,
    profile TEXT NOT NULL,
    model TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'accepted', 'rejected')),
    PRIMARY KEY(item_key, source_hash)
);
CREATE TABLE IF NOT EXISTS graph_review_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS graph_name_rules (
    typ TEXT NOT NULL CHECK(typ IN ('person', 'plats', 'organisation')),
    source TEXT NOT NULL CHECK(length(trim(source)) > 0),
    source_norm TEXT NOT NULL,
    target TEXT NOT NULL CHECK(length(trim(target)) > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (typ, source_norm)
);
"""

SCHEMA_SQL = GRAPH_REVIEW_SCHEMA_SQL + """
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

-- Obs: SQLite tillåter inte uttryck (COALESCE) i table-level PRIMARY KEY.
-- Vi uttrycker samma unikhetskrav via ett UNIQUE INDEX istället.
CREATE TABLE IF NOT EXISTS downloads (
    source        TEXT NOT NULL,
    drive_id      TEXT,
    url           TEXT,
    filename      TEXT NOT NULL,
    sha1          TEXT,
    bytes         INTEGER,
    downloaded_at TEXT NOT NULL,
    note          TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_downloads_pk
    ON downloads(source, COALESCE(drive_id, url));
CREATE INDEX IF NOT EXISTS idx_downloads_sha1 ON downloads(sha1);

CREATE TABLE IF NOT EXISTS pdf_files (
    pdf_stem                  TEXT PRIMARY KEY,
    source                    TEXT NOT NULL,
    pdf_path                  TEXT NOT NULL,
    redaction_checked_at      TEXT,
    has_redactions            INTEGER,
    merged_at                 TEXT,
    normalized_at             TEXT,
    text_mtime                REAL,
    tesseract_done_at         TEXT,
    tesseract_failed          INTEGER DEFAULT 0,
    tesseract_blacklisted_at  TEXT,
    surya_failed_at           TEXT
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

CREATE TABLE IF NOT EXISTS llm_corrections (
    pdf_stem     TEXT NOT NULL,
    page_num     INTEGER NOT NULL,
    corrected_at TEXT NOT NULL,
    PRIMARY KEY (pdf_stem, page_num)
);

CREATE TABLE IF NOT EXISTS wpu_decisions (
    pdf_stem    TEXT PRIMARY KEY,
    decided_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS doc_entities (
    pdf_stem     TEXT NOT NULL,
    page_num     INTEGER NOT NULL,
    payload      TEXT NOT NULL,
    model        TEXT,
    extracted_at TEXT NOT NULL,
    PRIMARY KEY (pdf_stem, page_num)
);

CREATE TABLE IF NOT EXISTS casebook_entries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    question      TEXT NOT NULL,
    answer        TEXT NOT NULL,
    mode          TEXT NOT NULL,
    backend       TEXT,
    model         TEXT,
    sources_json  TEXT NOT NULL,
    entities_json TEXT NOT NULL,
    note          TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_casebook_entries_created
    ON casebook_entries(created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS source_bookmarks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source     TEXT NOT NULL,
    page       INTEGER NOT NULL DEFAULT 0,
    nr         TEXT,
    title      TEXT,
    note       TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source, page)
);
CREATE INDEX IF NOT EXISTS idx_source_bookmarks_updated
    ON source_bookmarks(updated_at DESC, id DESC);

-- Fritextanteckningar (utredarens marginalanteckningar) knutna till en
-- specifik källa/sida. Till skillnad från bokmärken kan en källa/sida ha
-- flera anteckningar, så ingen UNIQUE(source, page) — id är nyckeln.
CREATE TABLE IF NOT EXISTS source_annotations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source     TEXT NOT NULL,
    page       INTEGER NOT NULL DEFAULT 0,
    nr         TEXT,
    title      TEXT,
    quote      TEXT,
    note       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_source_annotations_source
    ON source_annotations(source, page);
CREATE INDEX IF NOT EXISTS idx_source_annotations_updated
    ON source_annotations(updated_at DESC, id DESC);

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

CREATE TABLE IF NOT EXISTS map_observation_extractions (
    pdf_stem     TEXT NOT NULL,
    page_num     INTEGER NOT NULL,
    model        TEXT NOT NULL,
    observations INTEGER NOT NULL DEFAULT 0,
    extracted_at TEXT NOT NULL,
    PRIMARY KEY (pdf_stem, page_num)
);
CREATE INDEX IF NOT EXISTS idx_map_observation_extractions_extracted
    ON map_observation_extractions(extracted_at DESC);

CREATE TABLE IF NOT EXISTS admin_jobs (
    id                  TEXT PRIMARY KEY,
    operation           TEXT NOT NULL,
    params_json         TEXT NOT NULL,
    status              TEXT NOT NULL,
    active_slot         INTEGER,
    pid                 INTEGER,
    created_at          TEXT NOT NULL,
    started_at          TEXT,
    heartbeat_at        TEXT,
    finished_at         TEXT,
    current_step        TEXT,
    completed_units     INTEGER NOT NULL DEFAULT 0,
    total_units         INTEGER,
    message             TEXT,
    log_path            TEXT NOT NULL,
    exit_code           INTEGER,
    error               TEXT,
    cancel_requested_at TEXT,
    CHECK (active_slot IS NULL OR active_slot = 1),
    CHECK (status IN (
        'queued', 'running', 'cancel_requested',
        'succeeded', 'failed', 'cancelled', 'interrupted'
    ))
);

CREATE UNIQUE INDEX IF NOT EXISTS admin_jobs_one_active
    ON admin_jobs(active_slot)
    WHERE active_slot = 1;

CREATE INDEX IF NOT EXISTS idx_admin_jobs_created
    ON admin_jobs(created_at DESC, id DESC);
"""


def now() -> str:
    """Returnera ISO-timestamp i UTC med sekundprecision."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class _Migration(NamedTuple):
    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


class ActiveAdminJobError(RuntimeError):
    """Kastas när ett nytt jobb försöker starta medan ett annat är aktivt."""

    def __init__(self, active_job_id: str) -> None:
        super().__init__(f"Ett jobb körs redan: {active_job_id}")
        self.active_job_id = active_job_id


class DuplicateAdminJobError(RuntimeError):
    """Kastas när ett jobb-id redan finns i admin_jobs (PK-kollision)."""

    def __init__(self, job_id: str) -> None:
        super().__init__(f"Ett jobb med id {job_id} finns redan")
        self.job_id = job_id


class InvalidAdminJobTransition(RuntimeError):
    """Kastas när en jobbstatusövergång inte är tillåten."""


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _state_tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
    }


def _ensure_schema_version_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version    INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )


def schema_version(conn: sqlite3.Connection) -> int:
    """Returnera högsta tillämpade schema-version, eller 0 för omarkerad db."""
    if not _table_exists(conn, "schema_version"):
        return 0
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return int(row[0] or 0)


def _record_schema_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (?, ?)",
        (version, now()),
    )


def _set_sqlite_user_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(f"PRAGMA user_version = {int(version)}")


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, typedef: str
) -> None:
    if column not in _column_names(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {typedef}")


def _migration_006_pdf_file_ocr_status(conn: sqlite3.Connection) -> None:
    """Lägg till OCR-felstatus som tidigare låg i ad hoc-ALTER."""
    for col, typedef in [
        ("tesseract_done_at", "TEXT"),
        ("tesseract_failed", "INTEGER DEFAULT 0"),
        ("tesseract_blacklisted_at", "TEXT"),
        ("surya_failed_at", "TEXT"),
    ]:
        _add_column_if_missing(conn, "pdf_files", col, typedef)


def _migration_007_admin_jobs(conn: sqlite3.Connection) -> None:
    """Skapa jobbtabellen för bakgrundsjobb (idempotent)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_jobs (
            id                  TEXT PRIMARY KEY,
            operation           TEXT NOT NULL,
            params_json         TEXT NOT NULL,
            status              TEXT NOT NULL,
            active_slot         INTEGER,
            pid                 INTEGER,
            created_at          TEXT NOT NULL,
            started_at          TEXT,
            heartbeat_at        TEXT,
            finished_at         TEXT,
            current_step        TEXT,
            completed_units     INTEGER NOT NULL DEFAULT 0,
            total_units         INTEGER,
            message             TEXT,
            log_path            TEXT NOT NULL,
            exit_code           INTEGER,
            error               TEXT,
            cancel_requested_at TEXT,
            CHECK (active_slot IS NULL OR active_slot = 1),
            CHECK (status IN (
                'queued', 'running', 'cancel_requested',
                'succeeded', 'failed', 'cancelled', 'interrupted'
            ))
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS admin_jobs_one_active
            ON admin_jobs(active_slot)
            WHERE active_slot = 1
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_admin_jobs_created
            ON admin_jobs(created_at DESC, id DESC)
        """
    )


def _migration_008_graph_review(conn: sqlite3.Connection) -> None:
    """Skapa separat granskningsstate utan att ändra originalextraktionerna."""
    for statement in GRAPH_REVIEW_SCHEMA_SQL.split(";"):
        if statement.strip():
            conn.execute(statement)


def _migration_009_graph_name_rules(conn: sqlite3.Connection) -> None:
    """Skapa beständiga, typbundna namnregler för grafprojektionen."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS graph_name_rules (
            typ TEXT NOT NULL CHECK(typ IN ('person', 'plats', 'organisation')),
            source TEXT NOT NULL CHECK(length(trim(source)) > 0),
            source_norm TEXT NOT NULL,
            target TEXT NOT NULL CHECK(length(trim(target)) > 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (typ, source_norm)
        )"""
    )


MIGRATIONS: tuple[_Migration, ...] = (
    _Migration(6, "pdf_files OCR-felstatus", _migration_006_pdf_file_ocr_status),
    _Migration(7, "admin_jobs jobbmodell", _migration_007_admin_jobs),
    _Migration(8, "grafgranskning", _migration_008_graph_review),
    _Migration(9, "globala grafnamnregler", _migration_009_graph_name_rules),
)


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Öppna SQLite-anslutning med WAL, foreign keys och rimliga defaults.

    Skapar förälder-katalog vid behov. Om ``path`` är ``None`` läses
    ``STATE_DB`` från miljön vid anropstillfället (faller annars tillbaka
    på ``DEFAULT_DB``) — det gör att tester kan monkeypatcha env-variabeln
    efter att modulen importerats.
    """
    if path is None:
        path = Path(os.environ.get("STATE_DB") or DEFAULT_DB)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    # NORMAL är säkert under WAL och ~10x snabbare än FULL vid många commits.
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Idempotent schema-init och versionsstyrda uppgraderingar."""
    _ensure_schema_version_table(conn)
    current = schema_version(conn)
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"state.db har nyare schema-version {current} än koden stödjer "
            f"({SCHEMA_VERSION})"
        )

    existing_tables = _state_tables(conn) - {"schema_version"}
    conn.executescript(SCHEMA_SQL)

    if current == 0 and not existing_tables:
        _record_schema_version(conn, SCHEMA_VERSION)
        _set_sqlite_user_version(conn, SCHEMA_VERSION)
        conn.commit()
        return

    for migration in MIGRATIONS:
        if migration.version > current:
            migration.apply(conn)
            _record_schema_version(conn, migration.version)

    _set_sqlite_user_version(conn, schema_version(conn))
    conn.commit()


# --- helpers ----------------------------------------------------------

def source_for_path(path: Path | str, root: Path | None = None) -> str:
    """Bestäm 'files'/'wpu' baserat på var motsvarande artifact ligger.

    - Om path innehåller komponenten ``wpu_files`` → ``'wpu'``.
    - Om path är en ``.txt``-fil och ``root`` anges, leta efter motsvarande PDF i
      ``root/downloaded/wpu_files/<stem>.pdf``; om den finns → ``'wpu'``.
    - Default: ``'files'``.

    Detta är mer robust än ``"wpu" in str(path)`` som matchar t.ex. ``Wpunkt.pdf``.
    """
    p = Path(path)
    try:
        parts = p.resolve().parts
    except OSError:
        parts = p.parts
    if "wpu_files" in parts:
        return "wpu"
    if p.suffix == ".txt" and root is not None:
        wpu_pdf = Path(root) / "downloaded" / "wpu_files" / f"{p.stem}.pdf"
        if wpu_pdf.exists():
            return "wpu"
    return "files"


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
    """Returnera True om filen redan finns i downloads (matchar source+drive_id/url)."""
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
    """Slå upp första download-raden med matchande sha1, eller None."""
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM downloads WHERE sha1=? LIMIT 1", (sha1,)
    ).fetchone()
    return row


# --- pdf_files --------------------------------------------------------

def upsert_pdf_file(
    conn: sqlite3.Connection,
    *,
    pdf_stem: str,
    source: str,
    pdf_path: str,
) -> None:
    """Skriv eller uppdatera pdf_files-raden för pdf_stem (UPSERT)."""
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
    """Hämta pdf_files-raden för pdf_stem, eller None om den saknas."""
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM pdf_files WHERE pdf_stem=?", (pdf_stem,)
    ).fetchone()
    return row


def mark_redaction_checked(
    conn: sqlite3.Connection, pdf_stem: str, *, has_redactions: bool
) -> None:
    """Markera att redaktionsdetektering körts. Kastar KeyError om pdf_stem saknas."""
    cur = conn.execute(
        """UPDATE pdf_files
           SET redaction_checked_at=?, has_redactions=?
           WHERE pdf_stem=?""",
        (now(), 1 if has_redactions else 0, pdf_stem),
    )
    if cur.rowcount == 0:
        raise KeyError(pdf_stem)
    conn.commit()


def redaction_checked(conn: sqlite3.Connection, pdf_stem: str) -> bool:
    """Returnera True om redaktionsdetektering körts för pdf_stem."""
    row = conn.execute(
        "SELECT redaction_checked_at FROM pdf_files WHERE pdf_stem=?",
        (pdf_stem,),
    ).fetchone()
    return bool(row and row["redaction_checked_at"])


def mark_tesseract_done(
    conn: sqlite3.Connection, pdf_stem: str, *, pdf_path: str, source: str
) -> None:
    """Markera att Tesseract-OCR lyckades (UPSERT — skapar rad om den saknas)."""
    conn.execute(
        """
        INSERT INTO pdf_files(pdf_stem, source, pdf_path, tesseract_done_at, tesseract_failed)
        VALUES (?, ?, ?, ?, 0)
        ON CONFLICT(pdf_stem) DO UPDATE SET
            tesseract_done_at = excluded.tesseract_done_at,
            tesseract_failed  = 0,
            tesseract_blacklisted_at = NULL,
            surya_failed_at   = NULL
        """,
        (pdf_stem, source, pdf_path, now()),
    )
    conn.commit()


def mark_tesseract_failed(
    conn: sqlite3.Connection, pdf_stem: str, *, pdf_path: str, source: str
) -> None:
    """Markera att Tesseract-OCR misslyckades (UPSERT — skapar rad om den saknas)."""
    conn.execute(
        """
        INSERT INTO pdf_files(pdf_stem, source, pdf_path, tesseract_failed)
        VALUES (?, ?, ?, 1)
        ON CONFLICT(pdf_stem) DO UPDATE SET
            tesseract_failed  = 1,
            tesseract_done_at = NULL
        """,
        (pdf_stem, source, pdf_path),
    )
    conn.commit()


def clear_tesseract_failed(conn: sqlite3.Connection) -> int:
    """Nollställ tesseract_failed för alla filer. Returnerar antal påverkade rader."""
    cur = conn.execute(
        "UPDATE pdf_files SET tesseract_failed=0 WHERE tesseract_failed=1"
    )
    conn.commit()
    return cur.rowcount


def mark_tesseract_blacklisted(conn: sqlite3.Connection, pdf_stem: str) -> None:
    """Uteslut fler Tesseract-försök för pdf_stem.

    Surya-fallback får fortfarande försöka om ``surya_failed_at`` är tomt.
    ``--retry-blacklist`` tar in filen i Tesseract-flödet igen.
    Kräver att pdf_files-raden redan finns.
    """
    cur = conn.execute(
        "UPDATE pdf_files SET tesseract_blacklisted_at=? WHERE pdf_stem=?",
        (now(), pdf_stem),
    )
    if cur.rowcount == 0:
        raise KeyError(f"okänt pdf_stem: {pdf_stem}")
    conn.commit()


def clear_tesseract_blacklisted(conn: sqlite3.Connection) -> int:
    """Nollställ tesseract_blacklisted_at för alla filer. Returnerar antal påverkade rader."""
    cur = conn.execute(
        "UPDATE pdf_files SET tesseract_blacklisted_at=NULL, surya_failed_at=NULL "
        "WHERE tesseract_blacklisted_at IS NOT NULL"
    )
    conn.commit()
    return cur.rowcount


def retry_tesseract_blacklisted(conn: sqlite3.Connection) -> int:
    """Återaktivera blacklistade filer genom att nollställa OCR-felstatus."""
    cur = conn.execute(
        "UPDATE pdf_files "
        "SET tesseract_blacklisted_at=NULL, tesseract_failed=0, surya_failed_at=NULL "
        "WHERE tesseract_blacklisted_at IS NOT NULL"
    )
    conn.commit()
    return cur.rowcount


def is_tesseract_blacklisted(conn: sqlite3.Connection, pdf_stem: str) -> bool:
    """True om pdf_stem är uteslutet från fler Tesseract-försök."""
    row = conn.execute(
        "SELECT tesseract_blacklisted_at FROM pdf_files "
        "WHERE pdf_stem=? AND tesseract_blacklisted_at IS NOT NULL",
        (pdf_stem,),
    ).fetchone()
    return row is not None


def is_ocr_fully_failed(conn: sqlite3.Connection, pdf_stem: str) -> bool:
    """True om både Tesseract och Surya-fallback har misslyckats för pdf_stem."""
    row = conn.execute(
        "SELECT 1 FROM pdf_files "
        "WHERE pdf_stem=? "
        "AND tesseract_blacklisted_at IS NOT NULL "
        "AND surya_failed_at IS NOT NULL",
        (pdf_stem,),
    ).fetchone()
    return row is not None


def mark_surya_failed(conn: sqlite3.Connection, pdf_stem: str) -> None:
    """Markera att Surya-fallback misslyckades för pdf_stem."""
    cur = conn.execute(
        "UPDATE pdf_files SET surya_failed_at=? WHERE pdf_stem=?",
        (now(), pdf_stem),
    )
    if cur.rowcount == 0:
        raise KeyError(f"okänt pdf_stem: {pdf_stem}")
    conn.commit()


def clear_ocr_failures(conn: sqlite3.Connection, pdf_stem: str) -> bool:
    """Nollställ OCR-felstatus när en fallbackmotor har producerat text."""
    cur = conn.execute(
        """UPDATE pdf_files
           SET tesseract_failed=0,
               tesseract_blacklisted_at=NULL,
               surya_failed_at=NULL
           WHERE pdf_stem=?""",
        (pdf_stem,),
    )
    conn.commit()
    return cur.rowcount > 0


def list_tesseract_skip_stems(conn: sqlite3.Connection) -> set[str]:
    """Stems som Tesseract redan lyckats med, misslyckats med eller blacklistat."""
    return {
        row["pdf_stem"]
        for row in conn.execute(
            "SELECT pdf_stem FROM pdf_files "
            "WHERE tesseract_done_at IS NOT NULL "
            "OR tesseract_failed=1 OR tesseract_blacklisted_at IS NOT NULL"
        )
    }


def list_surya_fallback_candidates(conn: sqlite3.Connection) -> list[str]:
    """Stems där Tesseract misslyckats men Surya-fallback ännu inte provats."""
    return [
        row["pdf_stem"]
        for row in conn.execute(
            """SELECT pdf_stem FROM pdf_files
               WHERE (tesseract_failed=1 OR tesseract_blacklisted_at IS NOT NULL)
                 AND surya_failed_at IS NULL ORDER BY pdf_stem"""
        )
    ]



def mark_merged(
    conn: sqlite3.Connection, pdf_stem: str, *, text_mtime: float
) -> None:
    """Markera fil som mergad. text_mtime speglar senaste mutation av .txt-filen — skrivs över igen vid normalize. Kastar KeyError om pdf_stem saknas."""
    cur = conn.execute(
        "UPDATE pdf_files SET merged_at=?, text_mtime=? WHERE pdf_stem=?",
        (now(), text_mtime, pdf_stem),
    )
    if cur.rowcount == 0:
        raise KeyError(pdf_stem)
    conn.commit()


def touch_text_mtime(
    conn: sqlite3.Connection, pdf_stem: str, *, text_mtime: float
) -> None:
    """Uppdatera enbart text_mtime — för text skriven utanför merge_pages-spåret
    (t.ex. pdftotext i ``scripts/ocr_tesseract.py`` eller
    ``scripts/ocr.py --redo --mode files``), så att
    delta-frågorna ser filen. Kastar KeyError om pdf_stem saknas."""
    cur = conn.execute(
        "UPDATE pdf_files SET text_mtime=? WHERE pdf_stem=?",
        (text_mtime, pdf_stem),
    )
    if cur.rowcount == 0:
        raise KeyError(pdf_stem)
    conn.commit()


def mark_normalized(
    conn: sqlite3.Connection, pdf_stem: str, *, text_mtime: float
) -> None:
    """Markera fil som normaliserad och uppdatera text_mtime. Kastar KeyError om pdf_stem saknas."""
    cur = conn.execute(
        "UPDATE pdf_files SET normalized_at=?, text_mtime=? WHERE pdf_stem=?",
        (now(), text_mtime, pdf_stem),
    )
    if cur.rowcount == 0:
        raise KeyError(pdf_stem)
    conn.commit()


# --- pdf_pages --------------------------------------------------------

def record_page(
    conn: sqlite3.Connection, *,
    pdf_stem: str, page_num: int, engine: str,
    text: str | None, score: float | None,
) -> None:
    """Skriv en OCR-sida (UPSERT på pdf_stem+page_num). Ersätter page-NNN.json."""
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
    """Sann om sidan redan finns i pdf_pages."""
    return conn.execute(
        "SELECT 1 FROM pdf_pages WHERE pdf_stem=? AND page_num=?",
        (pdf_stem, page_num),
    ).fetchone() is not None


def get_pages_for_stem(
    conn: sqlite3.Connection, pdf_stem: str
) -> list[sqlite3.Row]:
    """Alla sidor för en stem, sorterat på page_num."""
    return list(conn.execute(
        "SELECT * FROM pdf_pages WHERE pdf_stem=? ORDER BY page_num",
        (pdf_stem,),
    ))


# --- quality ----------------------------------------------------------

def record_quality(
    conn: sqlite3.Connection, *,
    pdf_stem: str, score: float, chars: int,
    text_mtime: float, extras: dict | None = None,
) -> None:
    """UPSERT i quality. extras = dict med valfria heuristik-fält
    (pct_swe, junk_ratio, short_word_ratio, long_word_ratio,
     digit_in_word_ratio, avg_word_len, vowel_ratio, source_type)."""
    extras = extras or {}
    conn.execute(
        """
        INSERT INTO quality(
            pdf_stem, score, chars, text_mtime, scored_at,
            pct_swe, junk_ratio, short_word_ratio, long_word_ratio,
            digit_in_word_ratio, avg_word_len, vowel_ratio, source_type
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(pdf_stem) DO UPDATE SET
            score=excluded.score,
            chars=excluded.chars,
            text_mtime=excluded.text_mtime,
            scored_at=excluded.scored_at,
            pct_swe=excluded.pct_swe,
            junk_ratio=excluded.junk_ratio,
            short_word_ratio=excluded.short_word_ratio,
            long_word_ratio=excluded.long_word_ratio,
            digit_in_word_ratio=excluded.digit_in_word_ratio,
            avg_word_len=excluded.avg_word_len,
            vowel_ratio=excluded.vowel_ratio,
            source_type=excluded.source_type
        """,
        (
            pdf_stem, score, chars, text_mtime, now(),
            extras.get("pct_swe"), extras.get("junk_ratio"),
            extras.get("short_word_ratio"), extras.get("long_word_ratio"),
            extras.get("digit_in_word_ratio"), extras.get("avg_word_len"),
            extras.get("vowel_ratio"), extras.get("source_type"),
        ),
    )
    conn.commit()


def record_quality_page(
    conn: sqlite3.Connection, *,
    pdf_stem: str, page_num: int, score: float,
    chars: int | None = None, image_page: bool = False,
    payload: dict | None = None,
) -> None:
    """UPSERT i quality_pages. payload sparas som JSON-text."""
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
         json.dumps(payload) if payload else None,
         now()),
    )
    conn.commit()


def get_bad_pages(
    conn: sqlite3.Connection, *, threshold: float
) -> list[sqlite3.Row]:
    """Sidor med score under threshold, exklusive image_page-sidor."""
    return list(conn.execute(
        """SELECT pdf_stem, page_num, score, payload
           FROM quality_pages
           WHERE score < ? AND COALESCE(image_page, 0) = 0
           ORDER BY score ASC""",
        (threshold,),
    ))


def list_redo_pages(
    conn: sqlite3.Connection, *, threshold: float
) -> list[sqlite3.Row]:
    """Kandidatsidor för Surya-redo: score under tröskeln, inte bildsidor."""
    return list(conn.execute(
        """SELECT pdf_stem, page_num FROM quality_pages
           WHERE score < ? AND COALESCE(image_page, 0) = 0
           ORDER BY pdf_stem, page_num""",
        (threshold,),
    ))


def list_low_quality_stems(
    conn: sqlite3.Connection,
    *,
    threshold: float,
    source_type: str | None = None,
) -> list[str]:
    """Helfils-redo-kandidater från quality, eventuellt filtrerat på källtyp."""
    if source_type is None:
        rows = conn.execute(
            "SELECT pdf_stem FROM quality WHERE score < ? ORDER BY score",
            (threshold,),
        )
    else:
        rows = conn.execute(
            "SELECT pdf_stem FROM quality WHERE score < ? AND source_type = ? ORDER BY score",
            (threshold, source_type),
        )
    return [row["pdf_stem"] for row in rows]


# --- ingest -----------------------------------------------------------

def record_ingest(
    conn: sqlite3.Connection, *,
    pdf_stem: str, text_mtime: float, chunks: int,
) -> None:
    """UPSERT i ingest — markera att en stem indexerats med given mtime."""
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
    """Senast indexerade text_mtime för stem, eller None om ej indexerad."""
    row = conn.execute(
        "SELECT text_mtime FROM ingest WHERE pdf_stem=?", (pdf_stem,)
    ).fetchone()
    return row["text_mtime"] if row else None


# --- llm_corrections --------------------------------------------------

def mark_llm_corrected(
    conn: sqlite3.Connection, pdf_stem: str, page_num: int
) -> None:
    """Markera att en sida LLM-korrigerats. UPSERT — idempotent."""
    conn.execute(
        """INSERT INTO llm_corrections(pdf_stem, page_num, corrected_at)
           VALUES (?, ?, ?)
           ON CONFLICT(pdf_stem, page_num) DO UPDATE SET corrected_at=excluded.corrected_at""",
        (pdf_stem, page_num, now()),
    )
    conn.commit()


def llm_corrected(
    conn: sqlite3.Connection, pdf_stem: str, page_num: int
) -> bool:
    """Sann om sidan redan LLM-korrigerats."""
    return conn.execute(
        "SELECT 1 FROM llm_corrections WHERE pdf_stem=? AND page_num=?",
        (pdf_stem, page_num),
    ).fetchone() is not None


# --- wpu_decisions ----------------------------------------------------

def mark_wpu_decided(conn: sqlite3.Connection, pdf_stem: str) -> None:
    """Markera att merge_wpu fattat beslut för en wpu-stem (UPSERT)."""
    conn.execute(
        """INSERT INTO wpu_decisions(pdf_stem, decided_at) VALUES (?, ?)
           ON CONFLICT(pdf_stem) DO UPDATE SET decided_at=excluded.decided_at""",
        (pdf_stem, now()),
    )
    conn.commit()


def wpu_decided(conn: sqlite3.Connection, pdf_stem: str) -> bool:
    """Sann om merge_wpu redan fattat beslut för stem."""
    return conn.execute(
        "SELECT 1 FROM wpu_decisions WHERE pdf_stem=?", (pdf_stem,)
    ).fetchone() is not None


# --- doc_entities (kunskapsgraf) --------------------------------------

def record_doc_entities(
    conn: sqlite3.Connection, *,
    pdf_stem: str, page_num: int, payload: dict, model: str,
) -> None:
    """UPSERT av extraherade entiteter/relationer för en sida. payload som JSON."""
    conn.execute(
        """INSERT INTO doc_entities(pdf_stem, page_num, payload, model, extracted_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(pdf_stem, page_num) DO UPDATE SET
               payload=excluded.payload, model=excluded.model,
               extracted_at=excluded.extracted_at""",
        (pdf_stem, page_num, json.dumps(payload, ensure_ascii=False), model, now()),
    )
    conn.commit()


def doc_entities_extracted(
    conn: sqlite3.Connection, pdf_stem: str, page_num: int
) -> bool:
    """Sann om sidan redan entitetsextraherats."""
    return conn.execute(
        "SELECT 1 FROM doc_entities WHERE pdf_stem=? AND page_num=?",
        (pdf_stem, page_num),
    ).fetchone() is not None


def iter_doc_entities(conn: sqlite3.Connection) -> list[dict]:
    """Alla extraktioner som dictar med pdf_stem, page_num, payload (parsad JSON)."""
    return [
        {"pdf_stem": row["pdf_stem"], "page_num": row["page_num"],
         "payload": json.loads(row["payload"])}
        for row in conn.execute(
            "SELECT pdf_stem, page_num, payload FROM doc_entities "
            "ORDER BY pdf_stem, page_num"
        )
    ]


# --- casebook / bokmärken --------------------------------------------

def _json_list(value: list[dict] | None) -> str:
    """Serialisera listdata kompakt men läsbart i state-db."""
    return json.dumps(value or [], ensure_ascii=False, default=str)


def _parse_json_list(raw: str | None) -> list:
    """Tolka JSON-listor från state-db. Trasigt innehåll blir tom lista."""
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def record_casebook_entry(
    conn: sqlite3.Connection,
    *,
    question: str,
    answer: str,
    mode: str,
    backend: str | None = None,
    model: str | None = None,
    sources: list[dict] | None = None,
    entities: list[dict] | None = None,
    note: str | None = None,
) -> int:
    """Spara ett fråga/svar-spår i utredningspärmen och returnera rad-id."""
    if not question.strip():
        raise ValueError("question får inte vara tom")
    if not answer.strip():
        raise ValueError("answer får inte vara tom")
    cur = conn.execute(
        """
        INSERT INTO casebook_entries(
            question, answer, mode, backend, model, sources_json,
            entities_json, note, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            question.strip(),
            answer,
            mode.strip() or "okänt",
            backend,
            model,
            _json_list(sources),
            _json_list(entities),
            note.strip() if note and note.strip() else None,
            now(),
        ),
    )
    conn.commit()
    row_id = cur.lastrowid
    assert row_id is not None  # INSERT med autoincrement ger alltid ett rad-id
    return row_id


def list_casebook_entries(
    conn: sqlite3.Connection, *, limit: int = 20
) -> list[dict]:
    """Lista sparade fråga/svar-spår, nyast först."""
    rows = conn.execute(
        """
        SELECT id, question, answer, mode, backend, model, sources_json,
               entities_json, note, created_at
        FROM casebook_entries
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    )
    return [
        {
            "id": row["id"],
            "question": row["question"],
            "answer": row["answer"],
            "mode": row["mode"],
            "backend": row["backend"],
            "model": row["model"],
            "sources": _parse_json_list(row["sources_json"]),
            "entities": _parse_json_list(row["entities_json"]),
            "note": row["note"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def delete_casebook_entry(conn: sqlite3.Connection, entry_id: int) -> bool:
    """Radera ett sparat fråga/svar-spår. Returnerar True om något togs bort."""
    cur = conn.execute("DELETE FROM casebook_entries WHERE id=?", (entry_id,))
    conn.commit()
    return cur.rowcount > 0


def _stored_bookmark_page(page: int | None) -> int:
    """Spara okänd sida som 0 så UNIQUE(source, page) fungerar även utan sida."""
    return int(page) if page is not None else 0


def _public_bookmark_page(page: int) -> int | None:
    return page if page > 0 else None


def record_source_bookmark(
    conn: sqlite3.Connection,
    *,
    source: str,
    page: int | None = None,
    nr: str | None = None,
    title: str | None = None,
    note: str | None = None,
) -> int:
    """Spara eller uppdatera ett källbokmärke och returnera rad-id."""
    if not source.strip():
        raise ValueError("source får inte vara tom")
    stored_page = _stored_bookmark_page(page)
    stamp = now()
    conn.execute(
        """
        INSERT INTO source_bookmarks(
            source, page, nr, title, note, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source, page) DO UPDATE SET
            nr         = excluded.nr,
            title      = excluded.title,
            note       = excluded.note,
            updated_at = excluded.updated_at
        """,
        (
            source.strip(),
            stored_page,
            nr,
            title,
            note.strip() if note and note.strip() else None,
            stamp,
            stamp,
        ),
    )
    row = conn.execute(
        "SELECT id FROM source_bookmarks WHERE source=? AND page=?",
        (source.strip(), stored_page),
    ).fetchone()
    conn.commit()
    return int(row["id"])


def list_source_bookmarks(
    conn: sqlite3.Connection, *, limit: int = 50
) -> list[dict]:
    """Lista källbokmärken, senast uppdaterade först."""
    rows = conn.execute(
        """
        SELECT id, source, page, nr, title, note, created_at, updated_at
        FROM source_bookmarks
        ORDER BY updated_at DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    )
    return [
        {
            "id": row["id"],
            "source": row["source"],
            "page": _public_bookmark_page(row["page"]),
            "nr": row["nr"],
            "title": row["title"],
            "note": row["note"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def delete_source_bookmark(conn: sqlite3.Connection, bookmark_id: int) -> bool:
    """Radera ett källbokmärke. Returnerar True om något togs bort."""
    cur = conn.execute("DELETE FROM source_bookmarks WHERE id=?", (bookmark_id,))
    conn.commit()
    return cur.rowcount > 0


# --- source_annotations (utredarens marginalanteckningar) -------------

def record_source_annotation(
    conn: sqlite3.Connection,
    *,
    source: str,
    note: str,
    page: int | None = None,
    nr: str | None = None,
    title: str | None = None,
    quote: str | None = None,
) -> int:
    """Spara en ny anteckning knuten till en källa/sida och returnera rad-id.

    Flera anteckningar per källa/sida tillåts (till skillnad från bokmärken)."""
    if not source.strip():
        raise ValueError("source får inte vara tom")
    if not note.strip():
        raise ValueError("note får inte vara tom")
    stamp = now()
    cur = conn.execute(
        """
        INSERT INTO source_annotations(
            source, page, nr, title, quote, note, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source.strip(),
            _stored_bookmark_page(page),
            nr,
            title,
            quote.strip() if quote and quote.strip() else None,
            note.strip(),
            stamp,
            stamp,
        ),
    )
    conn.commit()
    row_id = cur.lastrowid
    assert row_id is not None  # INSERT med autoincrement ger alltid ett rad-id
    return row_id


def update_source_annotation(
    conn: sqlite3.Connection, annotation_id: int, *, note: str
) -> bool:
    """Uppdatera anteckningstexten. Returnerar True om raden fanns."""
    if not note.strip():
        raise ValueError("note får inte vara tom")
    cur = conn.execute(
        "UPDATE source_annotations SET note=?, updated_at=? WHERE id=?",
        (note.strip(), now(), annotation_id),
    )
    conn.commit()
    return cur.rowcount > 0


def list_source_annotations(
    conn: sqlite3.Connection, *, source: str | None = None, limit: int = 200
) -> list[dict]:
    """Lista anteckningar, senast uppdaterade först.

    Filtrera på ``source`` för en specifik källa (alla sidor)."""
    sql = (
        "SELECT id, source, page, nr, title, quote, note, created_at, updated_at "
        "FROM source_annotations"
    )
    params: list = []
    if source is not None:
        sql += " WHERE source=?"
        params.append(source.strip())
    sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
    params.append(limit)
    return [
        {
            "id": row["id"],
            "source": row["source"],
            "page": _public_bookmark_page(row["page"]),
            "nr": row["nr"],
            "title": row["title"],
            "quote": row["quote"],
            "note": row["note"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in conn.execute(sql, params)
    ]


def delete_source_annotation(conn: sqlite3.Connection, annotation_id: int) -> bool:
    """Radera en anteckning. Returnerar True om något togs bort."""
    cur = conn.execute(
        "DELETE FROM source_annotations WHERE id=?", (annotation_id,)
    )
    conn.commit()
    return cur.rowcount > 0


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


# --- delta-queries (inkrementell logik) -------------------------------

def files_needing_normalize(conn: sqlite3.Connection) -> list[str]:
    """pdf_stems vars text_mtime > normalized_at (eller aldrig normaliserade).

    Filer utan text_mtime (inte mergade än) inkluderas inte.

    Obs: ``strftime('%s', ...)`` returnerar TEXT — vi CAST:ar till INTEGER för
    att jämförelsen mot ``text_mtime`` (REAL) ska bli numerisk. Vi jämför på
    heltalssekunder eftersom ``normalized_at`` (ISO-timestamp via ``now()``) har
    sekundprecision medan ``text_mtime`` har subsekund — annars skulle filer
    alltid framstå som "nyare" direkt efter mark_normalized.
    """
    rows = conn.execute(
        """SELECT pdf_stem FROM pdf_files
           WHERE text_mtime IS NOT NULL
             AND (normalized_at IS NULL
                  OR CAST(text_mtime AS INTEGER)
                     > CAST(strftime('%s', normalized_at) AS INTEGER))"""
    )
    return [r["pdf_stem"] for r in rows]


def files_needing_quality(conn: sqlite3.Connection) -> list[str]:
    """pdf_stems som saknar quality-rad eller vars text_mtime är nyare."""
    rows = conn.execute(
        """SELECT pf.pdf_stem FROM pdf_files pf
           LEFT JOIN quality q USING (pdf_stem)
           WHERE pf.text_mtime IS NOT NULL
             AND (q.pdf_stem IS NULL OR pf.text_mtime > q.text_mtime)"""
    )
    return [r["pdf_stem"] for r in rows]


def files_needing_ingest(conn: sqlite3.Connection) -> list[str]:
    """pdf_stems som saknar ingest-rad eller vars text_mtime är nyare."""
    rows = conn.execute(
        """SELECT pf.pdf_stem FROM pdf_files pf
           LEFT JOIN ingest i USING (pdf_stem)
           WHERE pf.text_mtime IS NOT NULL
             AND (i.pdf_stem IS NULL OR pf.text_mtime > i.text_mtime)"""
    )
    return [r["pdf_stem"] for r in rows]


def list_pending_redaction_stems(
    conn: sqlite3.Connection, *, files_from: set[str] | None = None
) -> list[str]:
    """pdf_stems som ännu inte redaktionskontrollerats, valfritt filtrerat mot ``files_from``."""
    rows = conn.execute(
        "SELECT pdf_stem FROM pdf_files WHERE redaction_checked_at IS NULL"
    )
    if files_from is None:
        return [r["pdf_stem"] for r in rows]
    return [r["pdf_stem"] for r in rows if r["pdf_stem"] in files_from]


def reset_redaction_state(conn: sqlite3.Connection) -> int:
    """Nollställ redaction-flaggor för alla filer. Returnerar antal påverkade rader."""
    cur = conn.execute(
        "UPDATE pdf_files SET redaction_checked_at=NULL, has_redactions=NULL"
    )
    conn.commit()
    return cur.rowcount


def reset_pipeline_state_for_stem(conn: sqlite3.Connection, pdf_stem: str) -> None:
    """Nollställ per-sida- och Tesseract-state för en fil så kedjan körs om."""
    conn.execute("DELETE FROM pdf_pages WHERE pdf_stem=?", (pdf_stem,))
    conn.execute(
        """UPDATE pdf_files
           SET tesseract_done_at=NULL, tesseract_failed=0,
               tesseract_blacklisted_at=NULL, surya_failed_at=NULL
           WHERE pdf_stem=?""",
        (pdf_stem,),
    )
    conn.commit()


# --- admin_jobs ---------------------------------------------------------

_ACTIVE_ADMIN_JOB_STATUSES = ("queued", "running", "cancel_requested")
_TERMINAL_ADMIN_JOB_STATUSES = ("succeeded", "failed", "cancelled", "interrupted")


def _admin_job_active_slot_value(status: str) -> int | None:
    """Returnera active_slot-värdet (1 aktivt, None terminalt) för en status."""
    return 1 if status in _ACTIVE_ADMIN_JOB_STATUSES else None


def create_admin_job(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    operation: str,
    params_json: str,
    log_path: str,
) -> sqlite3.Row:
    """Skapa en köad jobbrad med active_slot=1.

    Kastar ``ActiveAdminJobError`` om ett annat jobb redan äger den aktiva
    platsen — den partiella unika indexeringen gör regeln atomisk.
    """
    try:
        conn.execute(
            """
            INSERT INTO admin_jobs(
                id, operation, params_json, status, active_slot,
                created_at, completed_units, log_path
            )
            VALUES (?, ?, ?, 'queued', 1, ?, 0, ?)
            """,
            (job_id, operation, params_json, now(), log_path),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        # Särskilj PK-kollision (dubblett-jobb-id) från kollision på den
        # partiella unika indexeringen av active_slot — bara det senare
        # betyder att ett annat jobb är aktivt.
        if get_admin_job(conn, job_id) is not None:
            raise DuplicateAdminJobError(job_id) from exc
        active = get_active_admin_job(conn)
        if active is not None:
            raise ActiveAdminJobError(active["id"]) from exc
        raise
    return get_admin_job(conn, job_id)  # type: ignore[return-value]


def claim_admin_job(conn: sqlite3.Connection, job_id: str, *, pid: int) -> bool:
    """Övergå queued → running och spara PID. Returnerar False vid misslyckad claim."""
    cur = conn.execute(
        """
        UPDATE admin_jobs
           SET status='running', pid=?, started_at=?, heartbeat_at=?
         WHERE id=? AND status='queued'
        """,
        (pid, now(), now(), job_id),
    )
    conn.commit()
    return cur.rowcount == 1


def get_admin_job(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
    """Hämta jobbraden för job_id, eller None om den saknas."""
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM admin_jobs WHERE id=?", (job_id,)
    ).fetchone()
    return row


def get_active_admin_job(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Returnera det aktiva jobbet (active_slot=1), eller None."""
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM admin_jobs WHERE active_slot=1"
    ).fetchone()
    return row


def list_admin_jobs(
    conn: sqlite3.Connection, *, limit: int | None = None
) -> list[sqlite3.Row]:
    """Returnera jobb nyast först, eventuellt begränsat till ``limit``."""
    query = "SELECT * FROM admin_jobs ORDER BY created_at DESC, id DESC"
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    return list(conn.execute(query))


def update_admin_job_progress(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    step: str | None,
    completed: int | None = None,
    total: int | None = None,
    message: str | None = None,
) -> None:
    """Uppdatera steg, enheter och senaste meddelande för ett jobb."""
    cur = conn.execute(
        """
        UPDATE admin_jobs
           SET current_step=COALESCE(?, current_step),
               completed_units=COALESCE(?, completed_units),
               total_units=COALESCE(?, total_units),
               message=COALESCE(?, message)
         WHERE id=?
        """,
        (step, completed, total, message, job_id),
    )
    if cur.rowcount == 0:
        raise KeyError(f"okänt jobb-id: {job_id}")
    conn.commit()


def heartbeat_admin_job(conn: sqlite3.Connection, job_id: str) -> None:
    """Uppdatera jobbets heartbeat-tidpunkt."""
    cur = conn.execute(
        "UPDATE admin_jobs SET heartbeat_at=? WHERE id=?", (now(), job_id)
    )
    if cur.rowcount == 0:
        raise KeyError(f"okänt jobb-id: {job_id}")
    conn.commit()


def request_admin_job_cancel(conn: sqlite3.Connection, job_id: str) -> bool:
    """Begär kontrollerad avbrytning av ett aktivt jobb.

    Övergår queued/running → cancel_requested. Returnerar True om jobbet nu är
    i cancel_requested, False om jobbet saknas eller redan är terminalt.
    """
    conn.execute(
        """
        UPDATE admin_jobs
           SET status='cancel_requested', cancel_requested_at=?
         WHERE id=? AND status IN ('queued', 'running')
        """,
        (now(), job_id),
    )
    conn.commit()
    row = get_admin_job(conn, job_id)
    return bool(row and row["status"] == "cancel_requested")


def _finish_admin_job_status_where(status: str) -> str:
    """Returnera villkoret för de nuvarande statusar som får övergå till ``status``."""
    if status == "succeeded":
        return "status='running'"
    if status == "failed":
        return "status IN ('queued', 'running')"
    if status in ("cancelled", "interrupted"):
        return "status IN ('queued', 'running', 'cancel_requested')"
    raise ValueError(f"Okänd terminal jobbstatus: {status!r}")


def finish_admin_job(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    status: str,
    exit_code: int | None,
    error: str | None = None,
) -> None:
    """Övergå ett jobb till terminal status och frigör active_slot atomiskt."""
    if status not in _TERMINAL_ADMIN_JOB_STATUSES:
        raise ValueError(f"Inte en terminal jobbstatus: {status!r}")

    cur = conn.execute(
        f"""
        UPDATE admin_jobs
           SET status=?, active_slot=NULL, finished_at=?, exit_code=?, error=?
         WHERE id=? AND {_finish_admin_job_status_where(status)}
        """,
        (status, now(), exit_code, error, job_id),
    )
    if cur.rowcount == 0:
        raise InvalidAdminJobTransition(
            f"Jobbet {job_id} kan inte övergå till {status!r} från sitt nuvarande tillstånd"
        )
    conn.commit()


def mark_admin_job_interrupted(conn: sqlite3.Connection, job_id: str) -> None:
    """Markera ett aktivt jobb som avbrutet av en omstart/krasch."""
    finish_admin_job(conn, job_id, status="interrupted", exit_code=None)


def delete_admin_job(conn: sqlite3.Connection, job_id: str) -> bool:
    """Ta bort ett terminalt jobb ur historiken. Returnerar True vid borttagning.

    Aktiva jobb (active_slot=1) skyddas från borttagning.
    """
    cur = conn.execute(
        "DELETE FROM admin_jobs WHERE id=? AND active_slot IS NULL", (job_id,)
    )
    conn.commit()
    return cur.rowcount == 1


# --- grafgranskning ---------------------------------------------------

_GRAPH_ENTITY_TYPES = {"person", "plats", "organisation"}


def _graph_name_rule_value(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} måste vara text")
    cleaned = " ".join(unicodedata.normalize("NFKC", value).split())
    if not cleaned:
        raise ValueError(f"{field} får inte vara tomt")
    return cleaned


def _graph_name_rule_norm(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def save_graph_name_rule(
    conn: sqlite3.Connection, *, typ: str, source: str, target: str,
) -> None:
    """Spara en typbunden global namnregel utan att ändra originalextraktion."""
    normalized_type = str(typ).strip().casefold()
    if normalized_type not in _GRAPH_ENTITY_TYPES:
        raise ValueError("Okänd entitetstyp för namnregeln")
    clean_source = _graph_name_rule_value(source, "Varianten")
    clean_target = _graph_name_rule_value(target, "Det kanoniska namnet")
    source_norm = _graph_name_rule_norm(clean_source)
    if source_norm == _graph_name_rule_norm(clean_target):
        raise ValueError("Varianten och det kanoniska namnet skiljer sig inte")
    stamp = now()
    with graph_review_write_transaction(conn):
        conn.execute(
            """INSERT INTO graph_name_rules
               (typ,source,source_norm,target,created_at,updated_at) VALUES(?,?,?,?,?,?)
               ON CONFLICT(typ,source_norm) DO UPDATE SET source=excluded.source,
               target=excluded.target,updated_at=excluded.updated_at""",
            (normalized_type, clean_source, source_norm, clean_target, stamp, stamp),
        )


def list_graph_name_rules(conn: sqlite3.Connection) -> list[dict]:
    """Lista globala namnregler i stabil ordning för projektion och UI."""
    return [
        {"typ": row["typ"], "source": row["source"], "target": row["target"]}
        for row in conn.execute(
            "SELECT typ,source,target FROM graph_name_rules ORDER BY typ,source_norm"
        )
    ]


def delete_graph_name_rule(conn: sqlite3.Connection, *, typ: str, source: str) -> bool:
    """Ta bort en exakt typbunden regel, oberoende av versaler och blanksteg."""
    normalized_type = str(typ).strip().casefold()
    if normalized_type not in _GRAPH_ENTITY_TYPES:
        raise ValueError("Okänd entitetstyp för namnregeln")
    source_norm = _graph_name_rule_norm(_graph_name_rule_value(source, "Varianten"))
    with graph_review_write_transaction(conn):
        cursor = conn.execute(
            "DELETE FROM graph_name_rules WHERE typ=? AND source_norm=?",
            (normalized_type, source_norm),
        )
    return cursor.rowcount == 1

def list_graph_review_decisions(conn: sqlite3.Connection) -> list[dict]:
    """Aktiva beslut; källans fingerprint valideras av granskningsmotorn."""
    return [
        {"item_key": row["item_key"], "source_hash": row["source_hash"],
         "action": row["action"], "target": json.loads(row["target_json"]),
         "note": row["note"]}
        for row in conn.execute("SELECT * FROM graph_review_decisions ORDER BY item_key")
    ]


def save_graph_review_decision(
    conn: sqlite3.Connection, *, item_key: str, source_hash: str,
    action: str, target: dict, note: str,
) -> None:
    """Spara beslut och historik atomiskt; reset tar bort det aktiva beslutet."""
    if action not in {"keep", "exclude", "replace", "reset"}:
        raise ValueError("Okänd granskningsåtgärd")
    if not isinstance(target, dict):
        raise ValueError("Målet måste vara ett objekt")
    if not all(isinstance(value, str) and value.strip()
               for value in (item_key, source_hash, note)):
        raise ValueError("Objektnyckel, källhash och motivering krävs")
    values = (item_key, source_hash, action,
              json.dumps(target, ensure_ascii=False, allow_nan=False), note.strip(), now())
    # En savepoint bevarar även en eventuell yttre transaktion vid fel.
    conn.execute("SAVEPOINT graph_review_decision")
    try:
        if action == "reset":
            conn.execute("DELETE FROM graph_review_decisions WHERE item_key=?", (item_key,))
        else:
            conn.execute(
                """INSERT INTO graph_review_decisions
                   (item_key,source_hash,action,target_json,note,created_at) VALUES(?,?,?,?,?,?)
                   ON CONFLICT(item_key) DO UPDATE SET source_hash=excluded.source_hash,
                   action=excluded.action,target_json=excluded.target_json,
                   note=excluded.note,created_at=excluded.created_at""", values,
            )
        conn.execute(
            """INSERT INTO graph_review_history
               (item_key,source_hash,action,target_json,note,created_at) VALUES(?,?,?,?,?,?)""",
            values,
        )
    except Exception:
        conn.execute("ROLLBACK TO graph_review_decision")
        conn.execute("RELEASE graph_review_decision")
        raise
    conn.execute("RELEASE graph_review_decision")


def record_graph_review_run(conn: sqlite3.Connection, *, report: dict) -> int:
    """Spara kontrollrapporten med dess fingerprint och fynd som JSON."""
    payload = json.dumps(report, ensure_ascii=False, allow_nan=False)
    cursor = conn.execute(
        "INSERT INTO graph_review_runs(report_json,created_at) VALUES(?,?)", (payload, now()),
    )
    conn.commit()
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def list_graph_review_runs(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    """Senaste kontrollerna först, med avkodad rapport."""
    return [
        {"id": row["id"], "created_at": row["created_at"],
         "report": json.loads(row["report_json"])}
        for row in conn.execute(
            "SELECT * FROM graph_review_runs ORDER BY id DESC LIMIT ?", (max(0, limit),),
        )
    ]


def get_graph_review_page(conn: sqlite3.Connection, stem: str, page_num: int) -> str:
    """Hämta sidans aktuella text, inklusive eventuell LLM-korrigering."""
    row = conn.execute(
        "SELECT text FROM pdf_pages WHERE pdf_stem=? AND page_num=?", (stem, page_num),
    ).fetchone()
    return (row["text"] or "") if row else ""


def read_graph_review_snapshot(conn: sqlite3.Connection) -> tuple[list[dict], list[dict]]:
    """Läs extraktioner och beslut ur samma snapshot, även i en yttre transaktion."""
    conn.execute("SAVEPOINT graph_review_snapshot")
    try:
        entries = iter_doc_entities(conn)
        decisions = list_graph_review_decisions(conn)
    except Exception:
        conn.execute("ROLLBACK TO graph_review_snapshot")
        conn.execute("RELEASE graph_review_snapshot")
        raise
    conn.execute("RELEASE graph_review_snapshot")
    return entries, decisions


def get_graph_review_decision_history(conn: sqlite3.Connection, item_key: str) -> list[dict]:
    """Beslutshistorik för ett objekt, äldsta först och inklusive återställningar."""
    return [
        {"id": row["id"], "item_key": row["item_key"], "source_hash": row["source_hash"],
         "action": row["action"], "target": json.loads(row["target_json"]),
         "note": row["note"], "created_at": row["created_at"]}
        for row in conn.execute(
            "SELECT * FROM graph_review_history WHERE item_key=? ORDER BY id", (item_key,),
        )
    ]


@contextmanager
def graph_review_write_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Lås före granskning och spara atomiskt; yttre transaktion måste redan ha skrivlås."""
    nested = conn.in_transaction
    conn.execute("SAVEPOINT graph_review_write" if nested else "BEGIN IMMEDIATE")
    try:
        yield conn
        if nested:
            conn.execute("RELEASE graph_review_write")
        else:
            conn.commit()
    except BaseException:
        if nested:
            conn.execute("ROLLBACK TO graph_review_write")
            conn.execute("RELEASE graph_review_write")
        else:
            conn.rollback()
        raise



def save_graph_review_suggestions(
    conn: sqlite3.Connection, suggestions: list[dict], profile: str, model: str,
) -> None:
    """Spara LLM-förslag atomiskt som väntande granskningsmaterial, aldrig beslut."""
    if not all(isinstance(v, str) and v.strip() for v in (profile, model)):
        raise ValueError("Profil och modell krävs")
    values = []
    for item in suggestions:
        if not isinstance(item, dict):
            raise ValueError("Förslaget måste vara ett objekt")
        if item.get("action") not in {"keep", "exclude", "replace"}:
            raise ValueError("Okänd förslagsåtgärd")
        if not isinstance(item.get("target"), dict):
            raise ValueError("Förslagets mål måste vara ett objekt")
        if not all(isinstance(item.get(k), str) and item[k].strip()
                   for k in ("item_key", "source_hash", "note", "evidence")):
            raise ValueError("Objektnyckel, källhash, motivering och källbelägg krävs")
        values.append((item["item_key"], item["source_hash"], item["action"],
                       json.dumps(item["target"], ensure_ascii=False, allow_nan=False),
                       item["note"].strip(), item["evidence"].strip(), profile, model, now()))
    with graph_review_write_transaction(conn):
        conn.executemany(
            """INSERT INTO graph_review_suggestions
               (item_key,source_hash,action,target_json,note,evidence,profile,model,created_at,status)
               VALUES(?,?,?,?,?,?,?,?,?,'pending')
               ON CONFLICT(item_key,source_hash) DO UPDATE SET action=excluded.action,
               target_json=excluded.target_json,note=excluded.note,evidence=excluded.evidence,
               profile=excluded.profile,model=excluded.model,created_at=excluded.created_at,
               status='pending'""", values,
        )


def list_graph_review_suggestions(conn: sqlite3.Connection) -> list[dict]:
    """Alla LLM-förslag inklusive status och ursprung, med avkodat mål."""
    result = []
    for row in conn.execute("SELECT * FROM graph_review_suggestions ORDER BY item_key,source_hash"):
        item = dict(row)
        item["target"] = json.loads(item.pop("target_json"))
        result.append(item)
    return result


def set_graph_review_suggestion_status(
    conn: sqlite3.Connection, item_key: str, source_hash: str, status: str,
) -> None:
    """Ändra förslagets status utan att själv skapa granskningsbeslut."""
    if status not in {"pending", "accepted", "rejected"}:
        raise ValueError("Okänd förslagsstatus")
    with graph_review_write_transaction(conn):
        cursor = conn.execute(
            "UPDATE graph_review_suggestions SET status=? WHERE item_key=? AND source_hash=?",
            (status, item_key, source_hash),
        )
        if cursor.rowcount != 1:
            raise ValueError("Granskningsförslaget finns inte")
