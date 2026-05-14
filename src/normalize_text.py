"""Regelbaserad normalisering av OCR-text.

Rättar Unicode-ligaturer, mjuka bindestreck, styrtecken och
whitespace-artefakter. Idempotent: en andra körning ger samma utdata.

Kör:
    python normalize_text.py [--txt text] [--dry-run]
"""
from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Unicode-ligaturer som pdftotext/Tesseract kan lämna kvar
_LIGATURES = str.maketrans({
    'ﬀ': 'ff',   # ﬀ
    'ﬁ': 'fi',   # ﬁ
    'ﬂ': 'fl',   # ﬂ
    'ﬃ': 'ffi',  # ﬃ
    'ﬄ': 'ffl',  # ﬄ
    'ﬅ': 'st',   # ﬅ
    'ﬆ': 'st',   # ﬆ
    'ĳ': 'ij',   # ĳ
    '­': '',     # mjukt bindestreck (soft hyphen) — osynligt men stör ordmatchning
    '﻿': '',     # BOM
})

# Styrtecken förutom \n \r \t \f (dessa behålls — \f är sidseparator)
_CTRL_RE = re.compile(r'[\x00-\x08\x0b\x0e-\x1f\x7f]')

# Fler än två blankrader → två (bevara styckeindelning men inte onödig luft)
_BLANK_RE = re.compile(r'\n{3,}')

# Fler mellanslag/tabbar på en rad → ett (men inte nyrad)
_SPACE_RE = re.compile(r'[ \t]{2,}')


def normalize(text: str) -> str:
    """Normalisera OCR-text. Ändrar inte meningsinnehåll."""
    text = text.translate(_LIGATURES)
    text = unicodedata.normalize('NFC', text)
    text = _CTRL_RE.sub('', text)
    text = _SPACE_RE.sub(' ', text)
    text = _BLANK_RE.sub('\n\n', text)
    return text


def process_file(path: Path, dry_run: bool = False) -> bool:
    """Normalisera en fil på plats. Returnerar True om filen förändrades."""
    original = path.read_text(encoding='utf-8', errors='replace')
    cleaned = normalize(original)
    if cleaned == original:
        return False
    if not dry_run:
        path.write_text(cleaned, encoding='utf-8')
    return True


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description='Normalisera OCR-textfiler (regelbaserat).')
    ap.add_argument('--txt', default='', help='text-katalog (default: <root>/text)')
    ap.add_argument('--root', default='', help='projektrot')
    ap.add_argument('--dry-run', action='store_true',
                    help='visa vad som skulle ändras utan att skriva')
    ap.add_argument('--stats', action='store_true',
                    help='visa per-fil-statistik för ändrade filer')
    args = ap.parse_args()

    root = Path(args.root) if args.root else ROOT
    txt_dir = Path(args.txt) if args.txt else root / 'text'

    files = sorted(txt_dir.glob('*.txt'))
    if not files:
        print(f'Inga .txt-filer i {txt_dir}')
        return

    changed = errors = 0
    for f in files:
        try:
            was_changed = process_file(f, dry_run=args.dry_run)
            if was_changed:
                changed += 1
                if args.stats or args.dry_run:
                    print(f'  [ändrad] {f.name}')
        except OSError as e:
            print(f'  [fel] {f.name}: {e}', file=sys.stderr)
            errors += 1

    prefix = '[dry-run] ' if args.dry_run else ''
    print(f'{prefix}{changed}/{len(files)} filer normaliserade'
          + (f' ({errors} fel)' if errors else '') + '.')


if __name__ == '__main__':
    main()
