"""SQLite-baserad state för pipeline-tracking.

Ersätter filbaserade markörer (.normalize_stamp, .quality_stamp, .redact,
page-NNN.json, manifest.csv, quality.csv, quality_pages.jsonl, LanceDB-mtime).

Schema, åtkomstskikt och inkrementella frågor samlas här — konsumentmoduler
ska aldrig skriva egen SQL.
"""

from __future__ import annotations

import json
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

CREATE TABLE IF NOT EXISTS llm_corrections (
    pdf_stem     TEXT NOT NULL,
    page_num     INTEGER NOT NULL,
    corrected_at TEXT NOT NULL,
    PRIMARY KEY (pdf_stem, page_num)
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
    return conn.execute(
        "SELECT * FROM pdf_files WHERE pdf_stem=?", (pdf_stem,)
    ).fetchone()


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
