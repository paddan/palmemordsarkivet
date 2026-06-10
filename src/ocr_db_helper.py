#!/usr/bin/env python3
"""CLI-hjälpare för ocr_tesseract.sh — sköter DB-operationer parallellsäkert.

Kommandon:
  check-done        <stem>             → exit 0 om done, exit 1 annars
  check-failed      <stem>             → exit 0 om failed, exit 1 annars
  check-blacklisted <stem>             → exit 0 om blacklistad, exit 1 annars
  mark-done         <stem> <pdf_path> [txt_path]
                                       → registrera lyckad OCR; med txt_path
                                         stämplas även text_mtime (krävs för
                                         normalize/quality-deltat)
  touch-mtime       <stem> <txt_path>  → uppdatera text_mtime efter om-OCR
  mark-failed       <stem> <pdf_path>  → registrera misslyckad OCR
  mark-blacklisted  <stem>             → permanent uteslut från OCR
  clear-failed                         → nollställ alla failed-flaggor, skriv count till stdout
  clear-blacklisted                    → återaktivera blacklistade filer, skriv count till stdout
  list-done                            → skriv en stem per rad (alla tesseract_done_at IS NOT NULL)
  list-failed                          → skriv en stem per rad (alla tesseract_failed=1)
  list-blacklisted                     → skriv en stem per rad (alla tesseract_blacklisted_at IS NOT NULL)

Engångsmigrering av legacy .ocr-done/.ocr-failed-markörfiler sköts av migrate_to_db.py.
"""

from __future__ import annotations

import sys
import os
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

import db


def _source(pdf_path: str) -> str:
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
        if len(sys.argv) > 4:
            txt = Path(sys.argv[4])
            if txt.is_file():
                db.touch_text_mtime(conn, stem, text_mtime=txt.stat().st_mtime)

    elif cmd == "touch-mtime":
        stem, txt_path = sys.argv[2], sys.argv[3]
        txt = Path(txt_path)
        if txt.is_file():
            db.touch_text_mtime(conn, stem, text_mtime=txt.stat().st_mtime)
        else:
            print(f"saknar txt-fil: {txt_path}", file=sys.stderr)
            sys.exit(1)

    elif cmd == "mark-failed":
        stem, pdf_path = sys.argv[2], sys.argv[3]
        db.mark_tesseract_failed(conn, stem, pdf_path=pdf_path, source=_source(pdf_path))

    elif cmd == "clear-failed":
        count = db.clear_tesseract_failed(conn)
        print(count)

    elif cmd == "check-blacklisted":
        stem = sys.argv[2]
        sys.exit(0 if db.is_tesseract_blacklisted(conn, stem) else 1)

    elif cmd == "mark-blacklisted":
        stem = sys.argv[2]
        db.mark_tesseract_blacklisted(conn, stem)

    elif cmd == "clear-blacklisted":
        count = db.retry_tesseract_blacklisted(conn)
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

    elif cmd == "list-blacklisted":
        rows = conn.execute(
            "SELECT pdf_stem FROM pdf_files WHERE tesseract_blacklisted_at IS NOT NULL"
        ).fetchall()
        for r in rows:
            print(r["pdf_stem"])

    else:
        print(f"okänt kommando: {cmd}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
