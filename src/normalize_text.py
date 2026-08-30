"""Regelbaserad normalisering av OCR-text.

Rättar Unicode-ligaturer, mjuka bindestreck, styrtecken och
whitespace-artefakter. Idempotent: en andra körning ger samma utdata.

Inkrementell logik via ``state.db``: kör bara på filer vars text_mtime är
nyare än senaste ``mark_normalized`` (eller saknar pdf_files-rad helt —
legacy/direktskrivna filer behandlas defensivt).

Kör:
    python normalize_text.py [--txt text] [--dry-run]
"""
from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path

import db as state_db

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
    '\xad': '',  # mjukt bindestreck (soft hyphen) — osynligt men stör ordmatchning
    '﻿': '', # BOM
})

# Styrtecken förutom \n \r \t \f (dessa behålls — \f är sidseparator)
_CTRL_RE = re.compile(r'[\x00-\x08\x0b\x0e-\x1f\x7f]')

# HTML-taggar som pdftotext ibland lämnar kvar (<b>, <del>, etc.)
_HTML_RE = re.compile(r'<[^>]{1,100}>')

# Fler än två blankrader → två (bevara styckeindelning men inte onödig luft)
_BLANK_RE = re.compile(r'\n{3,}')

# Dekorativa separatorrader: 3+ repetitioner av ~, =, -, _, •, ·, . (ensamma på raden)
_DECOR_RE = re.compile(r'^[-~=_·•.]{3,}$')

# Punktorader (· · · eller . . .) som i innehållsförteckningar
_DOT_LEADER_RE = re.compile(r'^[\s·•.]+$')

# Rader med enbart pipe-tecken och blanksteg (tabellkanter från pdftotext)
_PIPE_ONLY_RE = re.compile(r'^[\s|]+$')


def _clean_line(line: str) -> str | None:
    """Rensa en rad. Returnerar None om raden skall tas bort helt."""
    # Strip ledande och avslutande blanksteg (pdftotext positionerar med mellanslag)
    line = line.strip()

    # Ta bort HTML-taggar
    line = _HTML_RE.sub('', line).strip()

    if not line:
        return ''  # blank rad — behålls (collapse sker senare)

    # Rader utan ett enda alfanumeriskt tecken
    if not any(c.isalnum() for c in line):
        if len(line) <= 2:
            return None          # enstaka symbol (|, =, ,, .) → bort
        if _PIPE_ONLY_RE.match(line):
            return None          # tabellkanter (|   |   |) → bort
        if _DECOR_RE.match(line):
            return None          # ~~~~ eller ==== → bort
        if _DOT_LEADER_RE.match(line):
            return None          # · · · · (innehållsförteckning) → bort

    return line


def normalize(text: str) -> str:
    """Normalisera OCR-text. Ändrar inte meningsinnehåll."""
    # Globala teckentransformationer
    text = text.translate(_LIGATURES)
    text = unicodedata.normalize('NFC', text)
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = _CTRL_RE.sub('', text)

    # Per-sida bearbetning (bevara \f sidseparatorer)
    pages = text.split('\f')
    result_pages = []
    for page in pages:
        cleaned_lines = []
        for line in page.split('\n'):
            out = _clean_line(line)
            if out is not None:
                cleaned_lines.append(out)
        page_text = '\n'.join(cleaned_lines)
        # Collapse fler än 2 blankrader till 2
        page_text = _BLANK_RE.sub('\n\n', page_text)
        result_pages.append(page_text)

    return '\f'.join(result_pages)


def process_file(path: Path, dry_run: bool = False) -> bool:
    """Normalisera en fil på plats. Returnerar True om filen förändrades."""
    original = path.read_text(encoding='utf-8', errors='replace')
    cleaned = normalize(original)
    if cleaned == original:
        return False
    if not dry_run:
        path.write_text(cleaned, encoding='utf-8')
    return True


def _ctx(context):
    """Returnera ``context`` eller en terminal-context för förgrundskörning."""
    if context is not None:
        return context
    from operations.context import ensure_terminal_context

    return ensure_terminal_context(None)


def run_normalize(
    *,
    root: Path,
    txt_dir: Path,
    dry_run: bool,
    stats: bool,
    rebuild: bool,
    files_from: Path | None,
    context=None,
) -> int:
    """Normalisera textfiler. Returnerar antal förändrade filer."""
    ctx = _ctx(context)
    conn = state_db.connect()
    state_db.init_schema(conn)

    all_files = sorted(txt_dir.glob('*.txt'))

    if files_from is not None:
        listed_names: set[str] = set()
        for line in Path(files_from).read_text(encoding='utf-8').splitlines():
            name = line.strip()
            if name:
                listed_names.add(name if name.endswith('.txt') else name + '.txt')
        files = [f for f in all_files if f.name in listed_names]
        skipped = 0
    elif rebuild or dry_run:
        files = all_files
        skipped = 0
    else:
        needing = set(state_db.files_needing_normalize(conn))
        files = []
        for f in all_files:
            row = state_db.get_pdf_file(conn, f.stem)
            if row is None:
                # Legacy / direktskrivna filer utan pdf_files-rad → ta med.
                files.append(f)
            elif row["text_mtime"] is None:
                # Rad skapad av t.ex. mark_tesseract_done utan text_mtime —
                # filen har aldrig normaliserats (delta-frågan kräver
                # text_mtime IS NOT NULL och missar annars dessa).
                files.append(f)
            elif f.stem in needing:
                files.append(f)
        skipped = len(all_files) - len(files)

    if not files:
        if skipped:
            ctx.log(f'Normalisering klar — {skipped} filer oförändrade sedan föregående normalize.')
        else:
            ctx.log(f'Inga .txt-filer i {txt_dir}')
        return 0

    total = len(files)
    changed = errors = 0
    skip_note = f' ({skipped} oförändrade hoppas över)' if skipped else ''
    label = f"Normaliserar {total} filer{skip_note}…"
    ctx.log(label)
    for i, f in enumerate(files, 1):
        ctx.check_cancelled()
        ctx.progress(i, total, f.name)
        try:
            was_changed = process_file(f, dry_run=dry_run)
            if was_changed:
                changed += 1
                if stats or dry_run:
                    ctx.log(f'  [ändrad] {f.name}')
            if not dry_run:
                try:
                    state_db.mark_normalized(
                        conn, f.stem, text_mtime=f.stat().st_mtime
                    )
                except KeyError:
                    # pdf_files-rad saknas (legacy / direktskriven fil).
                    # Skapa raden defensivt och markera normaliserad.
                    source = state_db.source_for_path(f, root=root)
                    state_db.upsert_pdf_file(
                        conn, pdf_stem=f.stem, source=source, pdf_path=str(f)
                    )
                    state_db.mark_normalized(
                        conn, f.stem, text_mtime=f.stat().st_mtime
                    )
        except OSError as e:
            ctx.log(f'  [fel] {f.name}: {e}', level='error')
            errors += 1
        if i == total:
            ctx.log(f'{label} {i}/{total}')

    prefix = '[dry-run] ' if dry_run else ''
    ctx.log(f'{prefix}{changed}/{total} filer normaliserade'
            + (f' ({errors} fel)' if errors else '') + '.')
    return changed


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description='Normalisera OCR-textfiler (regelbaserat).')
    ap.add_argument('--txt', default='', help='text-katalog (default: <root>/text)')
    ap.add_argument('--root', default='', help='projektrot')
    ap.add_argument('--dry-run', action='store_true',
                    help='visa vad som skulle ändras utan att skriva')
    ap.add_argument('--stats', action='store_true',
                    help='visa per-fil-statistik för ändrade filer')
    ap.add_argument('--rebuild', action='store_true',
                    help='ignorera db-delta och normalisera alla filer')
    ap.add_argument('--files-from', default='',
                    help='bearbeta bara filer listade i FILE (ett filnamn per rad)')
    args = ap.parse_args()

    root = Path(args.root) if args.root else ROOT
    txt_dir = Path(args.txt) if args.txt else root / 'generated' / 'text'
    run_normalize(
        root=root,
        txt_dir=txt_dir,
        dry_run=args.dry_run,
        stats=args.stats,
        rebuild=args.rebuild,
        files_from=Path(args.files_from) if args.files_from else None,
    )


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nAvbrutet.', file=sys.stderr)
        sys.exit(130)
