CREATE TABLE schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

INSERT INTO schema_version(version, applied_at)
VALUES (5, '2026-07-01T00:00:00+00:00');

CREATE TABLE pdf_files (
    pdf_stem                 TEXT PRIMARY KEY,
    source                   TEXT NOT NULL,
    pdf_path                 TEXT NOT NULL,
    redaction_checked_at     TEXT,
    has_redactions           INTEGER,
    merged_at                TEXT,
    normalized_at            TEXT,
    text_mtime               REAL,
    tesseract_done_at        TEXT,
    tesseract_failed         INTEGER DEFAULT 0,
    tesseract_blacklisted_at TEXT
);

INSERT INTO pdf_files(
    pdf_stem,
    source,
    pdf_path,
    tesseract_failed,
    tesseract_blacklisted_at
)
VALUES (
    'wpu-legacy',
    'wpu',
    'downloaded/wpu_files/wpu-legacy.pdf',
    1,
    '2026-07-01T00:10:00+00:00'
);
