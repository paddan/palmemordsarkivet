"""Pipeline-state: nedladdningar, per-PDF-status, per-sida-OCR, kvalitetspoäng,
ingest, LLM-korrigeringar, wpu-beslut och inkrementella delta-queries."""

from __future__ import annotations

import json
import sqlite3

from ._schema import now

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


def list_downloads_for_source(
    conn: sqlite3.Connection, source: str
) -> list[sqlite3.Row]:
    """Download-rader för en källa, med drive_id, sha1, filename och note."""
    return list(conn.execute(
        "SELECT drive_id, sha1, filename, note FROM downloads WHERE source=?",
        (source,),
    ))


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


def is_tesseract_done(conn: sqlite3.Connection, pdf_stem: str) -> bool:
    """True om Tesseract-OCR lyckats för pdf_stem (tesseract_done_at satt)."""
    return conn.execute(
        "SELECT tesseract_done_at FROM pdf_files "
        "WHERE pdf_stem=? AND tesseract_done_at IS NOT NULL",
        (pdf_stem,),
    ).fetchone() is not None


def is_tesseract_failed(conn: sqlite3.Connection, pdf_stem: str) -> bool:
    """True om Tesseract-OCR markerats som misslyckad för pdf_stem."""
    return conn.execute(
        "SELECT tesseract_failed FROM pdf_files WHERE pdf_stem=? AND tesseract_failed=1",
        (pdf_stem,),
    ).fetchone() is not None


def list_tesseract_done_stems(conn: sqlite3.Connection) -> list[str]:
    """Alla pdf_stems där Tesseract-OCR lyckats."""
    return [
        row["pdf_stem"] for row in conn.execute(
            "SELECT pdf_stem FROM pdf_files WHERE tesseract_done_at IS NOT NULL"
        )
    ]


def list_tesseract_failed_stems(conn: sqlite3.Connection) -> list[str]:
    """Alla pdf_stems där Tesseract-OCR markerats som misslyckad."""
    return [
        row["pdf_stem"] for row in conn.execute(
            "SELECT pdf_stem FROM pdf_files WHERE tesseract_failed=1"
        )
    ]


def list_tesseract_blacklisted_stems(conn: sqlite3.Connection) -> list[str]:
    """Alla pdf_stems som blacklistats för fler Tesseract-försök."""
    return [
        row["pdf_stem"] for row in conn.execute(
            "SELECT pdf_stem FROM pdf_files WHERE tesseract_blacklisted_at IS NOT NULL"
        )
    ]


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
    markdown: str | None = None,
    layout_json: str | None = None,
    engine_identity: str | None = None,
) -> None:
    """Skriv en OCR-sida (UPSERT på pdf_stem+page_num). Ersätter page-NNN.json.

    Markdown, layout och motoridentitet skrivs alltid: en omkörning utan dem
    (t.ex. Tesseract efter Surya) nollar medvetet gammal Markdown så den inte
    blir kvar och motsäger den nya texten.
    """
    conn.execute(
        """
        INSERT INTO pdf_pages(pdf_stem, page_num, engine, text, score, processed_at,
                              markdown, layout_json, engine_identity)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(pdf_stem, page_num) DO UPDATE SET
            engine       = excluded.engine,
            text         = excluded.text,
            score        = excluded.score,
            processed_at = excluded.processed_at,
            markdown        = excluded.markdown,
            layout_json     = excluded.layout_json,
            engine_identity = excluded.engine_identity
        """,
        (pdf_stem, page_num, engine, text, score, now(),
         markdown, layout_json, engine_identity),
    )
    conn.commit()


def page_exists(conn: sqlite3.Connection, pdf_stem: str, page_num: int) -> bool:
    """Sann om sidan redan finns i pdf_pages."""
    return conn.execute(
        "SELECT 1 FROM pdf_pages WHERE pdf_stem=? AND page_num=?",
        (pdf_stem, page_num),
    ).fetchone() is not None


def count_pages_for_stem(conn: sqlite3.Connection, pdf_stem: str) -> int:
    """Antal sidor för en stem i pdf_pages (före/efter en körning: sparades nya sidor?)."""
    row = conn.execute(
        "SELECT COUNT(*) FROM pdf_pages WHERE pdf_stem=?", (pdf_stem,)
    ).fetchone()
    return int(row[0])


def get_pages_for_stem(
    conn: sqlite3.Connection, pdf_stem: str
) -> list[sqlite3.Row]:
    """Alla sidor för en stem, sorterat på page_num."""
    return list(conn.execute(
        "SELECT * FROM pdf_pages WHERE pdf_stem=? ORDER BY page_num",
        (pdf_stem,),
    ))


def get_page_markdown_map(
    conn: sqlite3.Connection, pdf_stem: str
) -> dict[int, tuple[str, str]]:
    """``page_num`` → ``(text, markdown)`` för sidor som har Markdown."""
    return {
        row["page_num"]: (row["text"] or "", row["markdown"])
        for row in conn.execute(
            "SELECT page_num, text, markdown FROM pdf_pages "
            "WHERE pdf_stem=? AND markdown IS NOT NULL ORDER BY page_num",
            (pdf_stem,),
        )
    }


def list_page_texts_for_stem(
    conn: sqlite3.Connection, pdf_stem: str
) -> list[sqlite3.Row]:
    """page_num och text för alla sidor i en stem (även sidor utan text), sorterat på page_num."""
    return list(conn.execute(
        "SELECT page_num, text FROM pdf_pages WHERE pdf_stem=? ORDER BY page_num",
        (pdf_stem,),
    ))


def list_ocr_page_texts(
    conn: sqlite3.Connection, pdf_stem: str
) -> list[sqlite3.Row]:
    """page_num och text för sidor i en stem som har text, sorterat på page_num."""
    return list(conn.execute(
        "SELECT page_num, text FROM pdf_pages WHERE pdf_stem=? AND text IS NOT NULL "
        "ORDER BY page_num",
        (pdf_stem,),
    ))


def list_pages_matching_text(
    conn: sqlite3.Connection, like_pattern: str
) -> list[sqlite3.Row]:
    """pdf_stem, page_num och text för sidor vars text matchar LIKE-mönstret."""
    return list(conn.execute(
        "SELECT pdf_stem, page_num, text FROM pdf_pages WHERE text LIKE ?",
        (like_pattern,),
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


def list_page_stems(conn: sqlite3.Connection) -> list[str]:
    """pdf_stems som har minst en per-sida-rad (underlag för merge)."""
    return [
        row["pdf_stem"] for row in conn.execute(
            "SELECT DISTINCT pdf_stem FROM pdf_pages ORDER BY pdf_stem"
        )
    ]


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


def list_worst_quality(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    """De ``limit`` lägsta quality-raderna (score stigande) för terminalrapporten."""
    return list(conn.execute(
        "SELECT pdf_stem, source_type, score, pct_swe FROM quality "
        "ORDER BY score ASC LIMIT ?",
        (limit,),
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


def list_ingested_mtimes(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """pdf_stem och text_mtime för alla indexerade stems."""
    return list(conn.execute("SELECT pdf_stem, text_mtime FROM ingest"))


def delete_ingest(conn: sqlite3.Connection, pdf_stem: str) -> None:
    """Ta bort ingest-raden för pdf_stem. Committas INTE här — anroparen committar."""
    conn.execute("DELETE FROM ingest WHERE pdf_stem=?", (pdf_stem,))


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


def list_wpu_decisions_with_ocr(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Alla wpu_decisions-stems med pdf_files.tesseract_done_at (LEFT JOIN)."""
    return list(conn.execute(
        """SELECT w.pdf_stem, p.tesseract_done_at
           FROM wpu_decisions AS w
           LEFT JOIN pdf_files AS p ON p.pdf_stem = w.pdf_stem"""
    ))


def delete_wpu_decisions(conn: sqlite3.Connection, pdf_stems: list[str]) -> None:
    """Radera wpu_decisions för givna stems och committa."""
    conn.executemany(
        "DELETE FROM wpu_decisions WHERE pdf_stem=?",
        [(s,) for s in pdf_stems],
    )
    conn.commit()


def clear_wpu_decisions(conn: sqlite3.Connection) -> None:
    """Radera alla wpu_decisions-rader och committa (används vid --rebuild)."""
    conn.execute("DELETE FROM wpu_decisions")
    conn.commit()


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
