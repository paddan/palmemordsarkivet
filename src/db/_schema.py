"""Kärna för state.db: konstanter, schema, migrationer, anslutning och gemensamma hjälpare.

Domänmodulerna i paketet importerar härifrån. Här finns ingen domänlogik
bortsett från de små hjälparna längst ned."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[2]

DEFAULT_DB: Path = Path(os.environ.get("STATE_DB", str(ROOT / "generated" / "db" / "state.db")))

SCHEMA_VERSION: int = 10

LLM_USAGE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS llm_usage (
    profile          TEXT PRIMARY KEY,
    calls            INTEGER NOT NULL DEFAULT 0,
    input_tokens     INTEGER NOT NULL DEFAULT 0,
    output_tokens    INTEGER NOT NULL DEFAULT 0,
    cache_hit_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd         REAL    NOT NULL DEFAULT 0,
    cost_partial     INTEGER NOT NULL DEFAULT 0,
    last_model       TEXT    NOT NULL DEFAULT '',
    updated_at       TEXT    NOT NULL
);
"""

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

SCHEMA_SQL = GRAPH_REVIEW_SCHEMA_SQL + LLM_USAGE_SCHEMA_SQL + """
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


def _migration_010_llm_usage(conn: sqlite3.Connection) -> None:
    """Skapa räknartabellen för token och kostnad per LLM-profil."""
    for statement in LLM_USAGE_SCHEMA_SQL.split(";"):
        if statement.strip():
            conn.execute(statement)


MIGRATIONS: tuple[_Migration, ...] = (
    _Migration(6, "pdf_files OCR-felstatus", _migration_006_pdf_file_ocr_status),
    _Migration(7, "admin_jobs jobbmodell", _migration_007_admin_jobs),
    _Migration(8, "grafgranskning", _migration_008_graph_review),
    _Migration(9, "globala grafnamnregler", _migration_009_graph_name_rules),
    _Migration(10, "LLM-räknare per profil", _migration_010_llm_usage),
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
