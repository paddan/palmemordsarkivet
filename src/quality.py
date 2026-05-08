#!/usr/bin/env python3
"""
Bedöm OCR-kvalitet per textfil. Skriver quality.csv sorterat värst först.

Tar hänsyn till om originalet hade textlager — sidor som extraherats
direkt med pdftotext (utan OCR) markeras som 'text-layer' och får inte
samma straff som Tesseract-skräp.

Kör:
    python quality.py            # alla filer → quality.csv
    python quality.py --top 30   # visa även värsta 30 i terminalen
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from errors_log import log_error
except Exception:  # pragma: no cover
    def log_error(component: str, item: str, message: str) -> None:
        pass

ROOT = Path(os.environ.get("ROOT") or Path(__file__).resolve().parents[1])
TEXT_DIR = Path(os.environ.get("TEXT_DIR") or (ROOT / "text"))
FILES_DIR = Path(os.environ.get("FILES_DIR") or (ROOT / "files"))
MIN_TEXT_CHARS = int(os.environ.get("MIN_TEXT_CHARS", "200"))  # samma tröskel som ocr.sh

VOWELS = set("aeiouyåäöAEIOUYÅÄÖ")
PUNCT = set('.,;:!?"\'()-—–…/\\[]{}<>')
WORD_RE = re.compile(r"\S+")
ALPHA_WORD_RE = re.compile(r"[A-Za-zÅÄÖåäö\-]+")


def has_hunspell_swe() -> bool:
    if not shutil.which("hunspell"):
        return False
    try:
        out = subprocess.run(
            ["hunspell", "-D"], input=b"", capture_output=True, timeout=10
        )
        return b"sv_SE" in out.stderr or b"sv_SE" in out.stdout
    except subprocess.SubprocessError:
        return False


def original_had_text(stem: str, files_dir: Path = FILES_DIR) -> bool:
    pdf = files_dir / f"{stem}.pdf"
    if not pdf.exists():
        return False
    try:
        out = subprocess.run(
            ["pdftotext", "-q", "-layout", str(pdf), "-"],
            capture_output=True, timeout=60,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return False
    chars = sum(1 for c in out.stdout.decode("utf-8", errors="replace") if not c.isspace())
    return chars > MIN_TEXT_CHARS


def hunspell_pct(words: list[str]) -> float | None:
    """Andel ord hunspell godkänner som svenska."""
    if not words:
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

    # Sammanvägd 0–100 (högre = bättre).
    score = 100.0
    score -= out["junk_ratio"] * 200          # 50 % junk → -100
    score -= out["short_word_ratio"] * 80
    score -= out["long_word_ratio"] * 100
    score -= out["digit_in_word_ratio"] * 150
    score -= abs(out["vowel_ratio"] - 0.40) * 100
    if out["pct_swe"] is not None:
        score -= (1 - out["pct_swe"]) * 60
    out["score"] = round(max(0.0, min(100.0, score)), 1)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--top", type=int, help="visa även värsta N i terminalen")
    ap.add_argument("--out", default=os.environ.get("OUT", "quality.csv"),
                    help="output-CSV (default: quality.csv)")
    ap.add_argument("--limit", type=int, help="bara N första filerna (för testkörning)")
    ap.add_argument("--per-page", action="store_true",
                    help="skriv quality_pages.jsonl med en rad per sida")
    ap.add_argument("--pages-out",
                    default=os.environ.get("PAGES_OUT", "quality_pages.jsonl"),
                    help="output-fil för --per-page")
    ap.add_argument("--text-dir", default=str(TEXT_DIR),
                    help=f"katalog med .txt-filer (default: {TEXT_DIR})")
    ap.add_argument("--files-dir", default=str(FILES_DIR),
                    help=f"katalog med original-PDF:er (default: {FILES_DIR})")
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

    files = sorted(text_dir.glob("*.txt"))
    if not files:
        print(f"Inga .txt-filer i {text_dir}/", file=sys.stderr)
        return 1
    if args.limit:
        files = files[: args.limit]

    print(f"Bedömer {len(files)} filer…", file=sys.stderr)
    rows = []
    pages_fp = None
    if args.per_page:
        pages_fp = Path(args.pages_out).open("w", encoding="utf-8")
    try:
        for i, f in enumerate(files, 1):
            if i % 100 == 0:
                print(f"  {i}/{len(files)}", file=sys.stderr)
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                print(f"  SKIP {f.name}: {e}", file=sys.stderr)
                log_error("quality", f.name, str(e))
                continue
            scored = score_text(text, use_hunspell)
            scored["file"] = f.name
            scored["source"] = "text-layer" if original_had_text(f.stem, files_dir) else "ocr"
            rows.append(scored)

            if pages_fp is not None:
                pages = text.split("\f") if "\f" in text else [text]
                for p_idx, page_text in enumerate(pages, start=1):
                    p_scored = score_text(page_text, use_hunspell=False)
                    p_scored["file"] = f.name
                    p_scored["page"] = p_idx
                    pages_fp.write(json.dumps(p_scored, ensure_ascii=False) + "\n")
    finally:
        if pages_fp is not None:
            pages_fp.close()

    rows.sort(key=lambda r: r["score"])

    out_path = Path(args.out)
    cols = ["file", "source", "score", "chars", "pct_swe", "junk_ratio",
            "short_word_ratio", "long_word_ratio", "digit_in_word_ratio",
            "avg_word_len", "vowel_ratio"]
    with out_path.open("w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"\nSkrev {out_path} — {len(rows)} rader, sorterat värst först.")

    ocr = [r for r in rows if r["source"] == "ocr"]
    txt = [r for r in rows if r["source"] == "text-layer"]
    print(f"\n  text-layer (original hade text):  {len(txt)}")
    print(f"  ocr (Tesseract):                  {len(ocr)}")
    if ocr:
        s = sorted(r["score"] for r in ocr)
        print(f"  OCR median-score: {s[len(s) // 2]}")
        print(f"  OCR sidor med score < 50: {sum(1 for x in s if x < 50)}")
        print(f"  OCR sidor med score < 30: {sum(1 for x in s if x < 30)}")

    if args.top:
        print(f"\nVärsta {args.top} (alla källor):")
        for r in rows[:args.top]:
            swe = f"swe={r['pct_swe']:.0%}" if r["pct_swe"] is not None else "swe=?"
            print(f"  {r['score']:5.1f}  [{r['source']:10}] {swe:10}  {r['file'][:90]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
