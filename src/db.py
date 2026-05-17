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

DEFAULT_DB: Path = Path(os.environ.get("STATE_DB", str(ROOT / "generated" / "state.db")))

SCHEMA_VERSION: int = 1

SCHEMA_SQL = """
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


def now() -> str:
    """Returnera ISO-timestamp i UTC med sekundprecision."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


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
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Idempotent schema-init. Säker att köra flera gånger."""
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (?, ?)",
        (SCHEMA_VERSION, now()),
    )
    conn.commit()


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
