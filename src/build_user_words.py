#!/usr/bin/env python3
"""Bygg `tessdata/swe.user-words.auto` från befintlig OCR-text.

Räknar förekomster av alfa-ord (4+ tecken) i `text/*.txt`. Filtrerar mot
hunspell sv_SE om tillgängligt; annars kräver freq >= 30. Slår ihop med
befintliga `tessdata/swe.user-words` (om finns), dedupar, skriver
`swe.user-words.auto`.

Idempotent. Cachar ordfrekvenser per fil i `tessdata/user_words_state.json`
så att bara nya/ändrade filer läses om vid inkrementella körningar.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from errors_log import log_error
except Exception:  # pragma: no cover
    def log_error(component: str, item: str, message: str) -> None:
        pass

TEXT_DIR = ROOT / "generated" / "text"
TESSDATA = ROOT / "tessdata"
USER_WORDS = TESSDATA / "swe.user-words"
OUT = TESSDATA / "swe.user-words.auto"
STATE_FILE = TESSDATA / "user_words_state.json"

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


def _load_state(path: Path) -> dict:
    """Ladda inkrementell state: {mtimes: {filename: mtime}, counts: {word: n}}."""
    if not path.exists():
        return {"mtimes": {}, "counts": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data.get("mtimes"), dict) and isinstance(data.get("counts"), dict):
            return data
    except Exception:
        pass
    return {"mtimes": {}, "counts": {}}


def _save_state(path: Path, state: dict) -> None:
    try:
        path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        log_error("build_user_words", "state_save", str(e))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--text-dir",
                    default=os.environ.get("TEXT_DIR", str(TEXT_DIR)),
                    help=f"katalog med .txt-filer (default: {TEXT_DIR})")
    ap.add_argument("--out",
                    default=os.environ.get("OUT", str(OUT)),
                    help=f"output-fil (default: {OUT})")
    ap.add_argument("--user-words",
                    default=os.environ.get("USER_WORDS", str(USER_WORDS)),
                    help=f"befintliga user-words att slå ihop med (default: {USER_WORDS})")
    ap.add_argument("--min-freq",
                    type=int,
                    default=int(os.environ.get("MIN_FREQ", "0")),
                    help="minsta frekvens; 0 = auto (10 med hunspell, annars 30)")
    ap.add_argument("--rebuild", action="store_true",
                    help="ignorera cache och läs om alla filer")
    args = ap.parse_args()

    text_dir = Path(args.text_dir)
    out_path = Path(args.out)
    user_words_path = Path(args.user_words)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if not text_dir.exists():
        print(f"Saknar {text_dir}/", file=sys.stderr)
        return 1

    state = {"mtimes": {}, "counts": {}} if args.rebuild else _load_state(STATE_FILE)
    cached_mtimes: dict[str, float] = state["mtimes"]
    cached_counts: dict[str, int] = state["counts"]

    counter: Counter[str] = Counter(cached_counts)
    n_files = 0
    n_cached = 0
    n_updated = 0

    all_files = sorted(text_dir.glob("*.txt"))

    # Ta bort borttagna filer från cachen
    gone = set(cached_mtimes) - {f.name for f in all_files}
    if gone:
        for name in gone:
            cached_mtimes.pop(name, None)
            # Vi kan inte ta bort enskilda filbidrag utan att räkna om från grunden
            # — enklaste lösning: markera som dirty och räkna om
        counter = Counter()
        cached_mtimes.clear()

    total = len(all_files)
    label = f"Läser {total} filer…"
    print(label, end=" ", file=sys.stderr, flush=True)
    t0 = time.monotonic()
    for i, f in enumerate(all_files, 1):
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        if not args.rebuild and f.name in cached_mtimes and cached_mtimes[f.name] == mtime:
            n_cached += 1
        else:
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                log_error("build_user_words", f.name, str(e))
            else:
                for w in ALPHA_WORD_RE.findall(text):
                    counter[w.lower()] += 1
                cached_mtimes[f.name] = mtime
                n_updated += 1
        if i % 50 == 0 or i == total:
            elapsed = time.monotonic() - t0
            rate = i / elapsed if elapsed else 0
            eta = int((total - i) / rate) if rate else 0
            eta_s = f"{eta // 60}m{eta % 60:02d}s"
            print(f"\r{label} {i}/{total} eta {eta_s}", end="",
                  file=sys.stderr, flush=True)
            if i == total:
                print(file=sys.stderr)

    n_files = len(all_files)
    print(f"Läste {n_updated} nya/ändrade filer ({n_cached} cachadde), "
          f"{len(counter)} unika ord.", file=sys.stderr)

    # Spara uppdaterad state
    state["mtimes"] = cached_mtimes
    state["counts"] = dict(counter)
    _save_state(STATE_FILE, state)

    use_hunspell = has_hunspell_swe()
    if args.min_freq > 0:
        threshold = args.min_freq
    else:
        threshold = MIN_FREQ_HUNSPELL if use_hunspell else MIN_FREQ_NO_HUNSPELL
    candidates = [w for w, c in counter.items() if c >= threshold]
    print(f"Kandidater med freq >= {threshold}: {len(candidates)}", file=sys.stderr)

    if use_hunspell:
        # Behåll bara ord hunspell godkänner som svenska
        candidates = sorted(hunspell_known(candidates))
        print(f"Efter hunspell-filter: {len(candidates)}", file=sys.stderr)

    existing: set[str] = set()
    if user_words_path.exists():
        for line in user_words_path.read_text(encoding="utf-8").splitlines():
            w = line.strip()
            if w:
                existing.add(w)

    merged = sorted(existing | set(candidates))
    out_path.write_text("\n".join(merged) + "\n", encoding="utf-8")

    print(f"Skrev {out_path}: {len(merged)} ord "
          f"(befintliga {len(existing)}, nya {len(merged) - len(existing)}).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nAvbrutet.", file=sys.stderr)
        sys.exit(130)
