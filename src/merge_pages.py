#!/usr/bin/env python3
"""Slå ihop per-sida-text från text_pages/<stem>/ in i text/<stem>.txt.

text_pages/<stem>/page-NNN.txt skapas av ocr_pages.py för enskilda sidor (oftast
de som flaggats som dåliga). text/<stem>.txt har hela dokumentet med sidor
separerade av ``\\f``. Denna modul ersätter sidor i text/-versionen med
text_pages-versionen, en sida i taget.

CLI:
    python merge_pages.py --stem <namn>                # default-kataloger
    python merge_pages.py --stem <namn> --txt-dir text --pages-dir text_pages
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEXT_DIR = ROOT / "text"
PAGES_DIR = ROOT / "text_pages"

PAGE_FILE_RE = re.compile(r"^page-(\d+)\.txt$")


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


def find_updates(stem_dir: Path) -> dict[int, str]:
    """Hitta alla ``page-NNN.txt`` i ``stem_dir`` och returnera dict {sidnr: text}."""
    out: dict[int, str] = {}
    if not stem_dir.exists():
        return out
    for p in sorted(stem_dir.iterdir()):
        m = PAGE_FILE_RE.match(p.name)
        if not m:
            continue
        try:
            out[int(m.group(1))] = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"[merge_pages] kan inte läsa {p}: {e}", file=sys.stderr)
    return out


def merge_one(stem: str, txt_dir: Path, pages_dir: Path) -> bool:
    """Slå ihop för en fil. Returnerar True om filen uppdaterades.

    Efter lyckad merge raderas per-sida-artefakter som inte längre behövs:
    ``page-NNN.txt`` (innehållet finns i text/) och ``page-NNN.png`` (kan
    re-renderas från PDF). ``page-NNN.json`` behålls som idempotens-markör
    för ``ocr_pages.py`` och som spårbarhet. Eventuell legacy-combined
    ``text_pages/<stem>.txt`` raderas också.
    """
    txt_path = txt_dir / f"{stem}.txt"
    stem_dir = pages_dir / stem

    if not txt_path.exists():
        print(f"[merge_pages] {stem}: saknar {txt_path}, hoppar över",
              file=sys.stderr)
        return False

    updates = find_updates(stem_dir)
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

    # Cleanup körs även om texten redan var identisk — page-NNN.txt-filerna
    # är då redan mergade (sannolikt från en tidigare körning) och kan rensas.
    removed = 0
    for n in merged_pages:
        for suffix in (".txt", ".png"):
            p = stem_dir / f"page-{n:03d}{suffix}"
            if p.exists():
                p.unlink()
                if suffix == ".txt":
                    removed += 1
    legacy_combined = pages_dir / f"{stem}.txt"
    if legacy_combined.exists():
        legacy_combined.unlink()
        removed += 1

    if text_changed or removed:
        action = "uppdaterade" if text_changed else "rensade redan-mergade"
        msg = f"[merge_pages] {stem}: {action} {len(merged_pages)} av {n_pages} sidor"
        if invalid:
            msg += f" (ignorerade ogiltiga sidnr: {invalid})"
        print(msg)
    return text_changed or removed > 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--stem", help="filnamn utan .txt/.pdf-extension")
    grp.add_argument("--all", action="store_true",
                     help="kör för alla text_pages/<stem>/-mappar")
    ap.add_argument("--txt-dir", default=str(TEXT_DIR),
                    help=f"katalog med text/<stem>.txt (default: {TEXT_DIR})")
    ap.add_argument("--pages-dir", default=str(PAGES_DIR),
                    help=f"katalog med text_pages/<stem>/page-*.txt "
                         f"(default: {PAGES_DIR})")
    args = ap.parse_args()

    txt_dir = Path(args.txt_dir)
    pages_dir = Path(args.pages_dir)

    if args.all:
        if not pages_dir.exists():
            print(f"Saknar {pages_dir}/", file=sys.stderr)
            return 1
        stems = sorted(p.name for p in pages_dir.iterdir() if p.is_dir())
        updated = 0
        for stem in stems:
            if merge_one(stem, txt_dir, pages_dir):
                updated += 1
        print(f"\nKlart. {updated} av {len(stems)} filer uppdaterades.")
    else:
        merge_one(args.stem, txt_dir, pages_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
