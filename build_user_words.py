#!/usr/bin/env python3
"""Bygg `tessdata/swe.user-words.auto` från befintlig OCR-text.

Räknar förekomster av alfa-ord (4+ tecken) i `text/*.txt`. Filtrerar mot
hunspell sv_SE om tillgängligt; annars kräver freq >= 30. Slår ihop med
befintliga `tessdata/swe.user-words` (om finns), dedupar, skriver
`swe.user-words.auto`.

Idempotent.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    from errors_log import log_error
except Exception:  # pragma: no cover
    def log_error(component: str, item: str, message: str) -> None:
        pass

TEXT_DIR = ROOT / "text"
TESSDATA = ROOT / "tessdata"
USER_WORDS = TESSDATA / "swe.user-words"
OUT = TESSDATA / "swe.user-words.auto"

ALPHA_WORD_RE = re.compile(r"[A-Za-zÅÄÖåäö]{4,}")
MIN_FREQ_HUNSPELL = 10
MIN_FREQ_NO_HUNSPELL = 30


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


def hunspell_known(words: list[str]) -> set[str]:
    """Returnera delmängd av words som hunspell godkänner som svenska."""
    if not words:
        return set()
    try:
        out = subprocess.run(
            ["hunspell", "-d", "sv_SE", "-l"],
            input="\n".join(words).encode("utf-8"),
            capture_output=True, timeout=300,
        )
    except subprocess.SubprocessError as e:
        log_error("build_user_words", "hunspell", str(e))
        return set()
    bad = set(out.stdout.decode("utf-8", errors="replace").split())
    return {w for w in words if w not in bad}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text-dir", default=str(TEXT_DIR))
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    text_dir = Path(args.text_dir)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if not text_dir.exists():
        print(f"Saknar {text_dir}/", file=sys.stderr)
        return 1

    counter: Counter[str] = Counter()
    n_files = 0
    for f in sorted(text_dir.glob("*.txt")):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            log_error("build_user_words", f.name, str(e))
            continue
        for w in ALPHA_WORD_RE.findall(text):
            counter[w.lower()] += 1
        n_files += 1

    print(f"Läste {n_files} filer, {len(counter)} unika ord.", file=sys.stderr)

    use_hunspell = has_hunspell_swe()
    threshold = MIN_FREQ_HUNSPELL if use_hunspell else MIN_FREQ_NO_HUNSPELL
    candidates = [w for w, c in counter.items() if c >= threshold]
    print(f"Kandidater med freq >= {threshold}: {len(candidates)}", file=sys.stderr)

    if use_hunspell:
        # Behåll bara ord hunspell godkänner som svenska
        candidates = sorted(hunspell_known(candidates))
        print(f"Efter hunspell-filter: {len(candidates)}", file=sys.stderr)

    existing: set[str] = set()
    if USER_WORDS.exists():
        for line in USER_WORDS.read_text(encoding="utf-8").splitlines():
            w = line.strip()
            if w:
                existing.add(w)

    merged = sorted(existing | set(candidates))
    out_path.write_text("\n".join(merged) + "\n", encoding="utf-8")

    print(f"Skrev {out_path}: {len(merged)} ord "
          f"(befintliga {len(existing)}, nya {len(merged) - len(existing)}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
