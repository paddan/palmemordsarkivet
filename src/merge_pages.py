#!/usr/bin/env python3
"""Slå ihop per-sida-OCR-text från ``pdf_pages``-tabellen in i text/<stem>.txt.

OCR-pipelinen skriver varje sida till SQLite-tabellen ``pdf_pages``
(``ocr_pages.py``). Denna modul plockar ut sidorna för en stem och ersätter
motsvarande sidor i ``text/<stem>.txt`` (sidor separerade med ``\\f``), en
sida i taget. Efter lyckad merge stämplas ``pdf_files.merged_at`` +
``text_mtime`` via ``db.mark_merged``.

CLI:
    python merge_pages.py --stem <namn>
    python merge_pages.py --all
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

import db as state_db

ROOT = Path(__file__).resolve().parent.parent
TEXT_DIR = ROOT / "generated" / "text"


def merge_text(original: str, page_updates: dict[int, str]) -> str:
    """Ersätt enstaka sidor i ``original`` med text från ``page_updates``.

    - ``original``: hela dokumentet, sidor separerade med ``\\f``.
    - ``page_updates``: ``{sidnummer (1-indexerat): ny text för den sidan}``.
    - Sidnummer > originalets antal sidor expanderar originalet med tomma
      sidor — Surya kör mot PDF:ens verkliga sidnumrering, så om text/-filen
      saknar sidor (ocrmypdf/pdftotext missade dem) ska den växa, inte tappa
      data tyst.
    - Sidnummer <= 0 ignoreras (ogiltigt).
    - Tom ``original`` returneras oförändrad (inget att slå ihop mot).
    """
    if not original:
        return ""
    pages = original.split("\f")
    valid_updates = {n: t for n, t in page_updates.items() if n >= 1}
    if valid_updates:
        max_page = max(valid_updates)
        if max_page > len(pages):
            pages.extend([""] * (max_page - len(pages)))
    for page_num, new_text in valid_updates.items():
        pages[page_num - 1] = new_text
    return "\f".join(pages)


def find_updates(conn: sqlite3.Connection, pdf_stem: str) -> dict[int, str]:
    """Hämta alla OCR-sidor för stem från pdf_pages-tabellen som dict {sidnr: text}."""
    out: dict[int, str] = {}
    for row in conn.execute(
        "SELECT page_num, text FROM pdf_pages WHERE pdf_stem=? AND text IS NOT NULL "
        "ORDER BY page_num",
        (pdf_stem,),
    ):
        out[row["page_num"]] = row["text"]
    return out


def merge_one(stem: str, txt_dir: Path, conn: sqlite3.Connection | None = None) -> bool:
    """Slå ihop pdf_pages-sidor för ``stem`` in i ``text/<stem>.txt``.

    Returnerar True om filen uppdaterades på disk. Stämplar
    ``pdf_files.merged_at`` + ``text_mtime`` via ``db.mark_merged`` när
    texten faktiskt skrivs om.

    Om ``conn`` anges återanvänds den; annars öppnas en egen connection
    som stängs när funktionen returnerar (undviker fd-läck i stora loopar).
    """
    txt_path = txt_dir / f"{stem}.txt"

    if not txt_path.exists():
        print(f"[merge_pages] {stem}: saknar {txt_path}, hoppar över",
              file=sys.stderr)
        return False

    own_conn = conn is None
    if own_conn:
        conn = state_db.connect()
        state_db.init_schema(conn)

    try:
        updates = find_updates(conn, stem)
        if not updates:
            return False

        original = txt_path.read_text(encoding="utf-8", errors="replace")
        merged = merge_text(original, updates)

        orig_n_pages = original.count("\f") + 1 if original else 0
        invalid = sorted(n for n in updates if n < 1)
        merged_pages = sorted(n for n in updates if n >= 1)
        n_pages = max(orig_n_pages, max(merged_pages) if merged_pages else 0)

        text_changed = merged != original
        if text_changed:
            txt_path.write_text(merged, encoding="utf-8")
            try:
                state_db.mark_merged(conn, stem, text_mtime=txt_path.stat().st_mtime)
            except KeyError:
                print(f"[merge_pages] {stem}: saknar pdf_files-rad — "
                      "merged_at sätts inte", file=sys.stderr)
            msg = f"[merge_pages] {stem}: uppdaterade {len(merged_pages)} av {n_pages} sidor"
            if invalid:
                msg += f" (ignorerade ogiltiga sidnr: {invalid})"
            print(msg)
        return text_changed
    finally:
        if own_conn:
            conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--stem", help="filnamn utan .txt/.pdf-extension")
    grp.add_argument("--all", action="store_true",
                     help="kör för alla stems som har sidor i pdf_pages")
    ap.add_argument("--txt-dir", default=str(TEXT_DIR),
                    help=f"katalog med text/<stem>.txt (default: {TEXT_DIR})")
    args = ap.parse_args()

    txt_dir = Path(args.txt_dir)

    if args.all:
        conn = state_db.connect()
        state_db.init_schema(conn)
        stems = sorted({r["pdf_stem"] for r in conn.execute(
            "SELECT DISTINCT pdf_stem FROM pdf_pages"
        )})
        updated = 0
        total = len(stems)
        t0 = time.monotonic()
        label = f"Slår ihop {total} dokument…"
        print(label, end=" ", flush=True)
        for i, stem in enumerate(stems, 1):
            if merge_one(stem, txt_dir, conn=conn):
                updated += 1
            elapsed = time.monotonic() - t0
            rate = i / elapsed if elapsed else 0
            eta = int((total - i) / rate) if rate else 0
            eta_s = f"{eta // 60}m{eta % 60:02d}s"
            print(f"\r{label} {i}/{total} eta {eta_s}", end="", flush=True)
        print()
        conn.close()
        print(f"Klart. {updated} av {total} filer uppdaterades.")
    else:
        merge_one(args.stem, txt_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
