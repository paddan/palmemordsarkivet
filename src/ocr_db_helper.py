#!/usr/bin/env python3
"""CLI-hjälpare för ocr_tesseract.sh — sköter DB-operationer parallellsäkert.

Kommandon:
  check-done   <stem>             → exit 0 om done, exit 1 annars
  check-failed <stem>             → exit 0 om failed, exit 1 annars
  mark-done    <stem> <pdf_path>  → registrera lyckad OCR
  mark-failed  <stem> <pdf_path>  → registrera misslyckad OCR
  clear-failed                    → nollställ alla failed-flaggor, skriv count till stdout
  list-done                       → skriv en stem per rad (alla tesseract_done_at IS NOT NULL)
  list-failed                     → skriv en stem per rad (alla tesseract_failed=1)

Engångsmigrering av legacy .ocr-done/.ocr-failed-markörfiler sköts av migrate_to_db.py.
"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

import db


def _source(pdf_path: str) -> str:
    from pathlib import Path
    return db.source_for_path(pdf_path)


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)

    cmd = sys.argv[1]
    conn = db.connect()
    db.init_schema(conn)

    if cmd == "check-done":
        stem = sys.argv[2]
        row = conn.execute(
            "SELECT tesseract_done_at FROM pdf_files WHERE pdf_stem=? AND tesseract_done_at IS NOT NULL",
            (stem,),
        ).fetchone()
        sys.exit(0 if row else 1)

    elif cmd == "check-failed":
        stem = sys.argv[2]
        row = conn.execute(
            "SELECT tesseract_failed FROM pdf_files WHERE pdf_stem=? AND tesseract_failed=1",
            (stem,),
        ).fetchone()
        sys.exit(0 if row else 1)

    elif cmd == "mark-done":
        stem, pdf_path = sys.argv[2], sys.argv[3]
        db.mark_tesseract_done(conn, stem, pdf_path=pdf_path, source=_source(pdf_path))

    elif cmd == "mark-failed":
        stem, pdf_path = sys.argv[2], sys.argv[3]
        db.mark_tesseract_failed(conn, stem, pdf_path=pdf_path, source=_source(pdf_path))

    elif cmd == "clear-failed":
        count = db.clear_tesseract_failed(conn)
        print(count)

    elif cmd == "list-done":
        rows = conn.execute(
            "SELECT pdf_stem FROM pdf_files WHERE tesseract_done_at IS NOT NULL"
        ).fetchall()
        for r in rows:
            print(r["pdf_stem"])

    elif cmd == "list-failed":
        rows = conn.execute(
            "SELECT pdf_stem FROM pdf_files WHERE tesseract_failed=1"
        ).fetchall()
        for r in rows:
            print(r["pdf_stem"])

    else:
        print(f"okänt kommando: {cmd}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
