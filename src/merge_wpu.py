#!/usr/bin/env python3
"""
Slår samman text från wpu.nu-filer med palmemordsarkivet.

För varje PDF i files_wpu/:
  1. Extraherar text med pdftotext (wpu-filer har redan textlager).
  2. Beräknar textkvalitetspoäng.
  3. Söker efter matchande palme-textfil i text/ via dokument-ID
     (DA-14244-A ↔ DA14244-00-A, EDE-9980 ↔ EDE9980-00 osv.).
  4a. Match hittad och wpu bättre (≥ --margin poäng): ersätter palme-texten.
  4b. Match hittad och palme bättre: behåller palme-texten.
  4c. Ingen match: skriver wpu-texten som ny fil i text/.

Extraherad wpu-text cachas i text_wpu/ (markerat med .done-filer).

Kör:
    python merge_wpu.py            # kör merging
    python merge_wpu.py --dry-run  # visa vad som skulle hända
    python merge_wpu.py --rebuild  # kör om även redan behandlade filer
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES_WPU = ROOT / "files_wpu"
TEXT_DIR = ROOT / "text"
TEXT_WPU = ROOT / "text_wpu"

sys.path.insert(0, str(ROOT / "src"))
from download_wpu import palme_id_keys, wpu_id_keys  # noqa: E402
from quality import score_text  # noqa: E402

DEFAULT_MARGIN = 5  # wpu måste vara minst N poäng bättre för att ersätta palme


def _pdftotext(pdf: Path, layout: bool = False) -> str:
    """Kör pdftotext och returnera texten. layout=False för kvalitetscheck."""
    cmd = ["pdftotext", "-q"]
    if layout:
        cmd.append("-layout")
    cmd += [str(pdf), "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
        return r.stdout
    except Exception:
        return ""


def build_palme_key_map(text_dir: Path) -> dict[tuple, list[Path]]:
    """Bygg mapping ID-nyckel → lista av textfiler i text/."""
    mapping: defaultdict[tuple, list[Path]] = defaultdict(list)
    for txt in text_dir.glob("*.txt"):
        for key in palme_id_keys(txt.name):
            mapping[key].append(txt)
    return dict(mapping)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--dry-run", action="store_true",
                    help="visa vad som skulle hända utan att göra det")
    ap.add_argument("--rebuild", action="store_true",
                    help="kör om även redan behandlade filer (ignorerar .done-marker)")
    ap.add_argument("--margin", type=float, default=DEFAULT_MARGIN,
                    help=f"min poängfördel för att ersätta palme-text (default: {DEFAULT_MARGIN})")
    ap.add_argument("--files-wpu", default=str(FILES_WPU),
                    help=f"katalog med wpu-PDF:er (default: {FILES_WPU})")
    ap.add_argument("--text-dir", default=str(TEXT_DIR),
                    help=f"palme text-katalog (default: {TEXT_DIR})")
    args = ap.parse_args()

    files_wpu = Path(args.files_wpu)
    text_dir = Path(args.text_dir)

    if not files_wpu.is_dir():
        print(f"Saknar {files_wpu}/ — kör download_wpu.sh först.", file=sys.stderr)
        return 1
    if not text_dir.is_dir():
        print(f"Saknar {text_dir}/ — kör ocr_tesseract.sh (eller ocr.sh) först.", file=sys.stderr)
        return 1

    if not args.dry_run:
        TEXT_WPU.mkdir(exist_ok=True)

    print("Bygger palme-ID-karta…")
    palme_map = build_palme_key_map(text_dir)
    print(f"  {len(palme_map)} ID-nycklar i {text_dir.name}/")

    wpu_pdfs = sorted(files_wpu.glob("*.pdf"))
    print(f"  {len(wpu_pdfs)} PDF-filer i {files_wpu.name}/\n")

    n_better = 0
    n_kept = 0
    n_new = 0
    n_skip = 0
    n_no_text = 0

    for pdf in wpu_pdfs:
        stem = pdf.stem
        marker = TEXT_WPU / f"{stem}.done"
        wpu_cache = TEXT_WPU / f"{stem}.txt"

        if not args.rebuild and marker.exists():
            n_skip += 1
            continue

        # Extrahera text för kvalitetscheck (utan -layout undviker whitespace-inflation)
        if wpu_cache.exists() and not args.rebuild:
            raw_for_score = wpu_cache.read_text(encoding="utf-8", errors="replace")
        else:
            raw_for_score = _pdftotext(pdf, layout=False)
            if not args.dry_run:
                wpu_cache.write_text(raw_for_score, encoding="utf-8")

        wpu_score = score_text(raw_for_score)["score"]

        if wpu_score < 10:
            print(f"[tom/skräp]  {stem[:70]}  (poäng={wpu_score:.0f})")
            n_no_text += 1
            if not args.dry_run:
                marker.touch()
            continue

        # Hitta matchande palme-textfiler
        keys = wpu_id_keys(pdf.name)
        matched: list[Path] = []
        for key in keys:
            for p in palme_map.get(key, []):
                if p not in matched:
                    matched.append(p)

        if matched:
            for palme_txt in matched:
                palme_raw = palme_txt.read_text(encoding="utf-8", errors="replace")
                palme_score = score_text(palme_raw)["score"]

                if wpu_score > palme_score + args.margin:
                    print(
                        f"[bättre wpu] {stem[:50]:50s} "
                        f"wpu={wpu_score:.0f} > palme={palme_score:.0f} "
                        f"→ {palme_txt.name[:35]}"
                    )
                    if not args.dry_run:
                        layout_text = _pdftotext(pdf, layout=True)
                        palme_txt.write_text(layout_text, encoding="utf-8")
                    n_better += 1
                else:
                    print(
                        f"[behåller]   {palme_txt.name[:50]:50s} "
                        f"palme={palme_score:.0f} >= wpu={wpu_score:.0f}"
                    )
                    n_kept += 1
        else:
            # Ingen palme-match: lägg till som ny fil i text/
            dest = text_dir / f"{stem}.txt"
            if not dest.exists() or args.rebuild:
                print(f"[ny]         {stem[:70]}")
                if not args.dry_run:
                    layout_text = _pdftotext(pdf, layout=True)
                    dest.write_text(layout_text, encoding="utf-8")
                n_new += 1
            else:
                n_skip += 1

        if not args.dry_run:
            marker.touch()

    print(
        f"\nKlart: {n_better} ersatta (wpu bättre), {n_kept} behållna (palme bättre), "
        f"{n_new} nya, {n_no_text} tomma/skräp, {n_skip} hoppade."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
