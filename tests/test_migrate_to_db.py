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

    # OCR-text + redaktionsmarkör + stamp
    (root / "generated/text").mkdir(parents=True)
    txt = root / "generated/text/00001-0001.txt"
    txt.write_text("hej världen", encoding="utf-8")
    (root / "generated/text/00001-0001.redact").write_text("1")
    stamp = root / "generated/text/.normalize_stamp"
    stamp.touch()

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

    # wpu-decision-markörer
    (root / "generated/text_wpu").mkdir(parents=True)
    (root / "generated/text_wpu/foo.done").touch()
    (root / "generated/text_wpu/bar.done").touch()

    db_path = root / "generated/state.db"
    conn = connect(db_path)
    init_schema(conn)

    stats = migrate(conn, root)
    assert stats["downloads"] == 1
    assert stats["pdf_files"] == 1
    assert stats["pdf_pages"] == 1
    assert stats["quality"] == 1
    assert stats["quality_pages"] == 1
    assert stats["wpu_decisions"] == 2

    row = conn.execute(
        "SELECT * FROM pdf_files WHERE pdf_stem='00001-0001'"
    ).fetchone()
    assert row["has_redactions"] == 1
    assert row["normalized_at"] is not None
    assert row["text_mtime"] is not None


def test_migrate_idempotent(tmp_path):
    """Andra körningen ska ge samma resultat (UPSERT)."""
    root = tmp_path
    _make_csv(root / "downloaded/files/manifest.csv",
              [{"drive_id": "a", "filename": "x.pdf", "sha1": "h",
                "downloaded_at": "2026-01-01T00:00:00", "bytes": "1"}],
              ["drive_id", "filename", "sha1", "downloaded_at", "bytes"])
    (root / "downloaded/files/x.pdf").write_bytes(b"%PDF-fake")
    conn = connect(root / "state.db")
    init_schema(conn)
    migrate(conn, root)
    migrate(conn, root)  # andra körningen
    assert conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM pdf_files").fetchone()[0] == 1
