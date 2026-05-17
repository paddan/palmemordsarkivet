#!/usr/bin/env python3
"""
Bedöm OCR-kvalitet per textfil. Skriver resultat till SQLite-state (tabellerna
``quality`` och ``quality_pages``) via ``db.record_quality`` /
``db.record_quality_page``.

Tar hänsyn till om originalet hade textlager — sidor som extraherats
direkt med pdftotext (utan OCR) markeras som 'text-layer' och får inte
samma straff som Tesseract-skräp.

Kör:
    python quality.py            # alla filer som behöver bedömas
    python quality.py --rebuild  # om-bedöm alla
    python quality.py --top 30   # visa även värsta 30 i terminalen
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    from errors_log import log_error
except Exception:  # pragma: no cover
    def log_error(component: str, item: str, message: str) -> None:
        pass

import db as state_db

ROOT = Path(os.environ.get("ROOT") or Path(__file__).resolve().parents[1])
TEXT_DIR = Path(os.environ.get("TEXT_DIR") or (ROOT / "generated" / "text"))
FILES_DIR = Path(os.environ.get("FILES_DIR") or (ROOT / "downloaded" / "files"))
MIN_TEXT_CHARS = int(os.environ.get("MIN_TEXT_CHARS", "200"))  # samma tröskel som ocr.sh
MIN_PAGE_ALNUM = 30  # sidor med färre alfanumeriska tecken = bildsida, hoppa re-OCR

VOWELS = set("aeiouyåäöAEIOUYÅÄÖ")
PUNCT = set('.,;:!?"\'()-—–…/\\[]{}<>')
WORD_RE = re.compile(r"\S+")
ALPHA_WORD_RE = re.compile(r"[A-Za-zÅÄÖåäö\-]+")

# Vikter för score_text(). Justera här för att kalibrera bedömningen.
# Formeln: score = 100 - sum(ratio * weight) för varje dimension.
WEIGHT_JUNK = 200         # 50 % skräptecken → -100 p
WEIGHT_SHORT_WORD = 80    # korta ord (≤2 tecken) — vanligt i OCR-brus
WEIGHT_LONG_WORD = 100    # långa ord (≥18 tecken) — troligen hopklistrade
WEIGHT_DIGIT_MIXED = 150  # bokstäver+siffror i samma token — OCR-fel
VOWEL_TARGET = 0.40       # idealvokaltäthet för svenska
WEIGHT_VOWEL = 100        # avvikelse från VOWEL_TARGET
WEIGHT_HUNSPELL = 60      # andel icke-svenska ord enligt hunspell

_hunspell_available: bool | None = None  # cachat resultat


def has_hunspell_swe() -> bool:
    global _hunspell_available
    if _hunspell_available is not None:
        return _hunspell_available
    if not shutil.which("hunspell"):
        _hunspell_available = False
        return False
    try:
        out = subprocess.run(
            ["hunspell", "-D"], input=b"", capture_output=True, timeout=10
        )
        _hunspell_available = b"sv_SE" in out.stderr or b"sv_SE" in out.stdout
    except subprocess.SubprocessError:
        _hunspell_available = False
    return _hunspell_available


_text_layer_cache: dict[tuple[str, Path], bool] = {}


def original_had_text(stem: str, files_dir: Path = FILES_DIR) -> bool:
    key = (stem, files_dir)
    if key in _text_layer_cache:
        return _text_layer_cache[key]
    pdf = files_dir / f"{stem}.pdf"
    if not pdf.exists():
        _text_layer_cache[key] = False
        return False
    try:
        out = subprocess.run(
            ["pdftotext", "-q", "-layout", str(pdf), "-"],
            capture_output=True, timeout=60,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        _text_layer_cache[key] = False
        return False
    chars = sum(1 for c in out.stdout.decode("utf-8", errors="replace") if not c.isspace())
    result = chars > MIN_TEXT_CHARS
    _text_layer_cache[key] = result
    return result


def hunspell_pct(words: list[str]) -> float | None:
    """Andel ord hunspell godkänner som svenska."""
    if not words:
        return None
    if len(words) > 50_000:
        return None
    try:
        out = subprocess.run(
            ["hunspell", "-d", "sv_SE", "-l"],
            input="\n".join(words).encode("utf-8"),
            capture_output=True, timeout=120,
        )
    except subprocess.SubprocessError:
        return None
    bad = set(out.stdout.decode("utf-8", errors="replace").split())
    return (len(words) - sum(1 for w in words if w in bad)) / len(words)


def score_text(text: str, use_hunspell: bool) -> dict:
    chars = len(text)
    if chars == 0:
        return {"chars": 0, "score": 0.0}

    junk = sum(1 for c in text if not (c.isalnum() or c.isspace() or c in PUNCT))
    tokens = WORD_RE.findall(text)
    if not tokens:
        return {"chars": chars, "score": 0.0}

    words = ALPHA_WORD_RE.findall(text)
    short = sum(1 for w in words if len(w) <= 2)
    long_ = sum(1 for w in words if len(w) >= 18)
    digit_mixed = sum(
        1 for t in tokens
        if re.search(r"\d", t) and re.search(r"[A-Za-zÅÄÖåäö]", t)
    )
    letters = sum(1 for c in text if c.isalpha())
    vowels = sum(1 for c in text if c in VOWELS)
    vowel_ratio = vowels / letters if letters else 0.0
    real_words = [w for w in words if len(w) > 2]
    avg_len = sum(len(w) for w in real_words) / len(real_words) if real_words else 0.0

    out = {
        "chars": chars,
        "tokens": len(tokens),
        "junk_ratio": round(junk / chars, 3),
        "short_word_ratio": round(short / max(len(words), 1), 3),
        "long_word_ratio": round(long_ / max(len(words), 1), 3),
        "digit_in_word_ratio": round(digit_mixed / max(len(tokens), 1), 3),
        "avg_word_len": round(avg_len, 2),
        "vowel_ratio": round(vowel_ratio, 3),
        "pct_swe": None,
    }

    if use_hunspell:
        pct = hunspell_pct(words)
        out["pct_swe"] = round(pct, 3) if pct is not None else None

    # Sammanvägd 0–100 (högre = bättre). Vikter definierade som konstanter ovan.
    score = 100.0
    score -= out["junk_ratio"] * WEIGHT_JUNK
    score -= out["short_word_ratio"] * WEIGHT_SHORT_WORD
    score -= out["long_word_ratio"] * WEIGHT_LONG_WORD
    score -= out["digit_in_word_ratio"] * WEIGHT_DIGIT_MIXED
    score -= abs(out["vowel_ratio"] - VOWEL_TARGET) * WEIGHT_VOWEL
    if out["pct_swe"] is not None:
        score -= (1 - out["pct_swe"]) * WEIGHT_HUNSPELL
    out["score"] = round(max(0.0, min(100.0, score)), 1)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--top", type=int, help="visa även värsta N i terminalen")
    ap.add_argument("--limit", type=int, help="bara N första filerna (för testkörning)")
    ap.add_argument("--per-page", action="store_true",
                    help="bedöm även per sida och skriv till quality_pages-tabellen")
    ap.add_argument("--text-dir", default=str(TEXT_DIR),
                    help=f"katalog med .txt-filer (default: {TEXT_DIR})")
    ap.add_argument("--files-dir", default=str(FILES_DIR),
                    help=f"katalog med original-PDF:er (default: {FILES_DIR})")
    ap.add_argument("--rebuild", action="store_true",
                    help="ignorera inkrementell delta och kör om alla filer")
    ap.add_argument("--files-from", default="",
                    help="bedöm bara filer listade i FILE (ett filnamn per rad)")
    args = ap.parse_args()

    text_dir = Path(args.text_dir)
    files_dir = Path(args.files_dir)

    if not text_dir.exists():
        print(f"Saknar {text_dir}/", file=sys.stderr)
        return 1

    use_hunspell = has_hunspell_swe()
    if not use_hunspell:
        print(
            "hunspell + sv_SE-ordlista saknas — hoppar över ordbokskontroll. "
            "Installera: brew install hunspell, lägg sedan sv_SE.{aff,dic} i ~/Library/Spelling/",
            file=sys.stderr,
        )

    files_all = sorted(text_dir.glob("*.txt"))
    if not files_all:
        print(f"Inga .txt-filer i {text_dir}/", file=sys.stderr)
        return 1
    if args.limit:
        files_all = files_all[: args.limit]

    conn = state_db.connect()
    state_db.init_schema(conn)

    # Filurval: --rebuild = alla; --files-from = listan; annars db-delta.
    if args.rebuild:
        files_to_score = files_all
    elif args.files_from:
        listed_names: set[str] = set()
        for line in Path(args.files_from).read_text(encoding="utf-8").splitlines():
            name = line.strip()
            if name:
                listed_names.add(name if name.endswith(".txt") else name + ".txt")
        files_to_score = [f for f in files_all if f.name in listed_names]
    else:
        needing = set(state_db.files_needing_quality(conn))
        # Fallback: filer utan pdf_files-rad behöver också bedömas (legacy/tidigare körningar
        # innan db-state existerade — eller filer som inte gått via mark_merged ännu).
        files_to_score = []
        for f in files_all:
            row = state_db.get_pdf_file(conn, f.stem)
            if row is None or f.stem in needing:
                files_to_score.append(f)

    if not files_to_score:
        print(
            f"Alla {len(files_all)} filer aktuella i quality-tabellen — inget att göra.",
            file=sys.stderr,
        )
        return 0

    prefix = f"Bedömer {len(files_to_score)} filer"
    if len(files_to_score) < len(files_all):
        prefix += f" ({len(files_all) - len(files_to_score)} oförändrade hoppas över)"
    prefix += "…"
    print(prefix, end=" ", file=sys.stderr, flush=True)

    t0 = time.monotonic()
    new_rows: list[dict] = []  # för terminal-stats nedan

    for i, f in enumerate(files_to_score, 1):
        if i % 10 == 0 or i == len(files_to_score):
            elapsed = time.monotonic() - t0
            rate = i / elapsed if elapsed else 0
            eta = int((len(files_to_score) - i) / rate) if rate else 0
            eta_s = f"{eta // 60}m{eta % 60:02d}s"
            print(f"\r{prefix} {i}/{len(files_to_score)} eta {eta_s}", end="",
                  file=sys.stderr, flush=True)
            if i == len(files_to_score):
                print(file=sys.stderr)
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"  SKIP {f.name}: {e}", file=sys.stderr)
            log_error("quality", f.name, str(e))
            continue
        scored = score_text(text, use_hunspell)
        source_type = "text-layer" if original_had_text(f.stem, files_dir) else "ocr"
        text_mtime = f.stat().st_mtime

        # Defensiv: om pdf_files saknar raden (legacy) — skapa den så att
        # foreign-relationen håller och files_needing_* fungerar framöver.
        if state_db.get_pdf_file(conn, f.stem) is None:
            source = "wpu" if "wpu" in str(f).lower() else "files"
            state_db.upsert_pdf_file(
                conn, pdf_stem=f.stem, source=source, pdf_path=str(f),
            )

        extras = {k: scored.get(k) for k in (
            "pct_swe", "junk_ratio", "short_word_ratio", "long_word_ratio",
            "digit_in_word_ratio", "avg_word_len", "vowel_ratio",
        )}
        extras["source_type"] = source_type

        try:
            state_db.record_quality(
                conn, pdf_stem=f.stem,
                score=scored["score"], chars=scored["chars"],
                text_mtime=text_mtime, extras=extras,
            )
        except Exception as e:  # noqa: BLE001
            print(f"  SKIP {f.name}: db: {e}", file=sys.stderr)
            log_error("quality", f.name, f"db: {e}")
            continue

        if args.per_page:
            pages = text.split("\f") if "\f" in text else [text]
            for p_idx, page_text in enumerate(pages, start=1):
                p_scored = score_text(page_text, use_hunspell=False)
                alnum = sum(1 for c in page_text if c.isalnum())
                image_page = alnum < MIN_PAGE_ALNUM
                state_db.record_quality_page(
                    conn, pdf_stem=f.stem, page_num=p_idx,
                    score=(100.0 if image_page else p_scored["score"]),
                    chars=p_scored.get("chars"),
                    image_page=image_page,
                    payload=p_scored,
                )

        # Behåll för terminal-stats nedan.
        scored["file"] = f.name
        scored["source"] = source_type
        new_rows.append(scored)

    print(f"\nSkrev {len(new_rows)} rader till quality-tabellen.")

    # Stats för de bedömda filerna.
    ocr = [r for r in new_rows if r.get("source") == "ocr"]
    txt = [r for r in new_rows if r.get("source") == "text-layer"]
    print("\nNya/uppdaterade filer:")
    print(f"  text-layer (original hade text):  {len(txt)}")
    print(f"  ocr (Tesseract):                  {len(ocr)}")
    if ocr:
        s = sorted(float(r["score"]) for r in ocr)
        print(f"  OCR median-score: {s[len(s) // 2]}")
        print(f"  OCR sidor med score < 50: {sum(1 for x in s if x < 50)}")
        print(f"  OCR sidor med score < 30: {sum(1 for x in s if x < 30)}")

    if args.top:
        # Visa värsta N — om vi har nya rader, ranka dem; annars kör fallback mot db.
        if new_rows:
            rows_to_display = sorted(new_rows, key=lambda r: float(r.get("score") or 0))
            print(f"\nVärsta {args.top} (av nya/uppdaterade):")
            for r in rows_to_display[: args.top]:
                pct = r.get("pct_swe")
                swe = f"swe={float(pct):.0%}" if pct not in (None, "") else "swe=?"
                print(f"  {float(r['score']):5.1f}  [{r.get('source', '?'):10}] {swe:10}  {r['file'][:90]}")
        else:
            print(f"\nVärsta {args.top} (från quality-tabellen):")
            for r in conn.execute(
                "SELECT pdf_stem, source_type, score, pct_swe FROM quality "
                "ORDER BY score ASC LIMIT ?",
                (args.top,),
            ):
                pct = r["pct_swe"]
                swe = f"swe={float(pct):.0%}" if pct is not None else "swe=?"
                src = r["source_type"] or "?"
                print(f"  {float(r['score']):5.1f}  [{src:10}] {swe:10}  {r['pdf_stem']}.txt"[:120])

    return 0


if __name__ == "__main__":
    sys.exit(main())
