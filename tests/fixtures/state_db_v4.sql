CREATE TABLE schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

INSERT INTO schema_version(version, applied_at)
VALUES (4, '2026-06-20T00:00:00+00:00');

CREATE TABLE downloads (
    source        TEXT NOT NULL,
    drive_id      TEXT,
    url           TEXT,
    filename      TEXT NOT NULL,
    sha1          TEXT,
    bytes         INTEGER,
    downloaded_at TEXT NOT NULL,
    note          TEXT
);

CREATE TABLE pdf_files (
    pdf_stem             TEXT PRIMARY KEY,
    source               TEXT NOT NULL,
    pdf_path             TEXT NOT NULL,
    redaction_checked_at TEXT,
    has_redactions       INTEGER,
    merged_at            TEXT,
    normalized_at        TEXT,
    text_mtime           REAL
);

INSERT INTO pdf_files(
    pdf_stem,
    source,
    pdf_path,
    redaction_checked_at,
    has_redactions,
    merged_at,
    normalized_at,
    text_mtime
)
VALUES (
    '00001-0001',
    'files',
    'downloaded/files/00001-0001.pdf',
    '2026-06-20T00:01:00+00:00',
    1,
    '2026-06-20T00:02:00+00:00',
    '2026-06-20T00:03:00+00:00',
    123.0
);
