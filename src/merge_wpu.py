#!/usr/bin/env python3
"""
Slår samman text från wpu.nu-filer med palmemordsarkivet.

För varje PDF i files_wpu/:
  1. Extraherar text med pdftotext och bedömer kvalitet.
  2. Söker matchande palme-textfil i text/ via dokument-ID.

  Utfall beroende på kvalitet och matchning:

  ┌────────────────────┬──────────────────────────────────────────────────┐
  │                    │ Match i palme?                                   │
  │                    ├───────────────────────┬──────────────────────────┤
  │                    │ Ja                    │ Nej                      │
  ├────────────────────┼───────────────────────┼──────────────────────────┤
  │ wpu text OK        │ Jämför, bäst vinner   │ Skriv som ny fil         │
  │ wpu text dålig     │ Behåll palme-text     │ OCR-skanna wpu-filen     │
  └────────────────────┴───────────────────────┴──────────────────────────┘

Extraherad wpu-text cachas i text_wpu/ med .done-marker (idempotent).
Filerna processas parallellt över en process-pool (--jobs).

Kör:
    python merge_wpu.py            # kör merging
    python merge_wpu.py --dry-run  # visa vad som skulle hända
    python merge_wpu.py --rebuild  # kör om alla, ignorera .done-marker
    python merge_wpu.py --jobs 8   # antal parallella processer
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES_WPU = ROOT / "files_wpu"
TEXT_DIR = ROOT / "text"
TEXT_WPU = ROOT / "text_wpu"
TESSDATA = ROOT / "tessdata"

sys.path.insert(0, str(ROOT / "src"))
from download_wpu import palme_id_keys, wpu_id_keys  # noqa: E402
from quality import has_hunspell_swe, score_text  # noqa: E402

DEFAULT_MARGIN = 5      # wpu måste vara minst N poäng bättre för att ersätta palme
DEFAULT_OCR_THR = 30    # under detta anses wpu-texten oanvändbar → OCR behövs

# Worker-globaler — sätts en gång per process via _init_worker så att den stora
# palme-kartan inte pickleas på nytt för varje fil.
_PALME_MAP: dict[tuple, list[Path]] | None = None
_USE_HUNSPELL: bool = False


def _pdftotext(pdf: Path, layout: bool = False) -> str:
    """Extrahera text ur PDF. layout=False för kvalitetscheck (undviker whitespace-inflation)."""
    cmd = ["pdftotext", "-q"]
    if layout:
        cmd.append("-layout")
    cmd += [str(pdf), "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
        return r.stdout
    except Exception:
        return ""


def _ocr_pdf(pdf: Path, dest_txt: Path, per_file_jobs: int = 2) -> bool:
    """Kör ocrmypdf på en wpu-fil som saknar läsbart textlager.
    Försöker med --clean, sedan utan om det misslyckas.
    Returnerar True om det gick bra.
    """
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp_pdf = Path(tmp.name)
    try:
        base_cmd = [
            "ocrmypdf",
            "-l", "swe",
            "--jobs", str(per_file_jobs),
            "--optimize", "0",
            "--quiet",
            "--skip-text",
            "--rotate-pages",
            "--deskew",
            "--tesseract-pagesegmode", "6",
        ]
        cfg = TESSDATA / "tesseract.config"
        if cfg.exists():
            base_cmd += ["--tesseract-config", str(cfg)]
        uw = TESSDATA / "swe.user-words"
        if uw.exists():
            base_cmd += ["--user-words", str(uw)]

        for attempt_flags in ([["--clean"]], [[]]): # försök 1: med --clean; försök 2: utan
            cmd = base_cmd + attempt_flags[0] + [str(pdf), str(tmp_pdf)]
            if subprocess.run(cmd, capture_output=True).returncode == 0:
                text = _pdftotext(tmp_pdf, layout=True)
                dest_txt.write_text(text, encoding="utf-8")
                return True
        return False
    finally:
        tmp_pdf.unlink(missing_ok=True)


def build_palme_key_map(text_dir: Path) -> dict[tuple, list[Path]]:
    """Bygg mapping ID-nyckel → lista av textfiler i text/."""
    mapping: defaultdict[tuple, list[Path]] = defaultdict(list)
    for txt in text_dir.glob("*.txt"):
        for key in palme_id_keys(txt.name):
            mapping[key].append(txt)
    return dict(mapping)


def _init_worker(palme_map: dict[tuple, list[Path]], use_hunspell: bool) -> None:
    global _PALME_MAP, _USE_HUNSPELL
    _PALME_MAP = palme_map
    _USE_HUNSPELL = use_hunspell


def _process_one(
    pdf_path: str,
    text_dir_str: str,
    text_wpu_str: str,
    dry_run: bool,
    rebuild: bool,
    margin: float,
    ocr_threshold: float,
    ocr_per_file_jobs: int,
) -> dict:
    """Processa en wpu-PDF. Körs i worker-process. Returnerar resultat-dict
    med category ('better'|'kept'|'new'|'ocr'|'skip'), log-rader och ev. fel.
    """
    pdf = Path(pdf_path)
    text_dir = Path(text_dir_str)
    text_wpu = Path(text_wpu_str)
    palme_map = _PALME_MAP or {}
    use_hunspell = _USE_HUNSPELL

    stem = pdf.stem
    marker = text_wpu / f"{stem}.done"
    wpu_cache = text_wpu / f"{stem}.txt"

    result: dict = {"category": None, "lines": [], "errors": []}

    if not rebuild and marker.exists():
        result["category"] = "skip"
        return result

    if wpu_cache.exists() and not rebuild:
        raw = wpu_cache.read_text(encoding="utf-8", errors="replace")
    else:
        raw = _pdftotext(pdf, layout=False)
        if not dry_run:
            wpu_cache.write_text(raw, encoding="utf-8")

    wpu_score = score_text(raw, use_hunspell=use_hunspell)["score"]
    wpu_text_ok = wpu_score >= ocr_threshold

    keys = wpu_id_keys(pdf.name)
    matched: list[Path] = []
    for key in keys:
        for p in palme_map.get(key, []):
            if p not in matched:
                matched.append(p)

    if matched:
        if not wpu_text_ok:
            palme_raw = matched[0].read_text(encoding="utf-8", errors="replace")
            palme_score = score_text(palme_raw, use_hunspell=use_hunspell)["score"]
            result["lines"].append(
                f"[behåller]   {matched[0].name[:50]:50s} "
                f"palme={palme_score:.0f} (wpu={wpu_score:.0f} oanvändbar)"
            )
            result["category"] = "kept"
        else:
            cat = "kept"
            for palme_txt in matched:
                palme_raw = palme_txt.read_text(encoding="utf-8", errors="replace")
                palme_score = score_text(palme_raw, use_hunspell=use_hunspell)["score"]

                if wpu_score > palme_score + margin:
                    result["lines"].append(
                        f"[bättre wpu] {stem[:50]:50s} "
                        f"wpu={wpu_score:.0f} > palme={palme_score:.0f} "
                        f"→ {palme_txt.name[:35]}"
                    )
                    if not dry_run:
                        layout_text = _pdftotext(pdf, layout=True)
                        palme_txt.write_text(layout_text, encoding="utf-8")
                    cat = "better"
                else:
                    result["lines"].append(
                        f"[behåller]   {palme_txt.name[:50]:50s} "
                        f"palme={palme_score:.0f} >= wpu={wpu_score:.0f}"
                    )
            result["category"] = cat
    else:
        dest = text_dir / f"{stem}.txt"
        if dest.exists() and not rebuild:
            result["category"] = "skip"
        elif wpu_text_ok:
            result["lines"].append(f"[ny]         {stem[:70]}")
            if not dry_run:
                layout_text = _pdftotext(pdf, layout=True)
                dest.write_text(layout_text, encoding="utf-8")
            result["category"] = "new"
        else:
            result["lines"].append(f"[ocr-wpu]    {stem[:70]}  (wpu-poäng={wpu_score:.0f})")
            if not dry_run:
                ok = _ocr_pdf(pdf, dest, per_file_jobs=ocr_per_file_jobs)
                if not ok:
                    result["errors"].append(f"  [fel] ocrmypdf misslyckades för {stem}")
            result["category"] = "ocr"

    if not dry_run and not result["errors"]:
        marker.touch()

    return result


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--dry-run", action="store_true",
                    help="visa vad som skulle hända utan att göra det")
    ap.add_argument("--rebuild", action="store_true",
                    help="kör om alla, ignorera .done-marker")
    ap.add_argument("--margin", type=float, default=DEFAULT_MARGIN,
                    help=f"min poängfördel för att ersätta palme-text (default: {DEFAULT_MARGIN})")
    ap.add_argument("--ocr-threshold", type=float, default=DEFAULT_OCR_THR,
                    help=f"wpu-text under detta poäng anses oanvändbar (default: {DEFAULT_OCR_THR})")
    ap.add_argument("--files-wpu", default=str(FILES_WPU),
                    help=f"katalog med wpu-PDF:er (default: {FILES_WPU})")
    ap.add_argument("--text-dir", default=str(TEXT_DIR),
                    help=f"palme text-katalog (default: {TEXT_DIR})")
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 4)),
                    help="antal parallella processer (default: cpu_count)")
    ap.add_argument("--per-file-jobs", type=int, default=2,
                    help="OCR-trådar per fil när ocrmypdf körs (default: 2)")
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

    use_hunspell = has_hunspell_swe()
    if not use_hunspell:
        print("hunspell saknas — hoppar över ordbokskontroll", file=sys.stderr)

    print("Bygger palme-ID-karta…")
    palme_map = build_palme_key_map(text_dir)
    print(f"  {len(palme_map)} ID-nycklar i {text_dir.name}/")

    wpu_pdfs = sorted(files_wpu.glob("*.pdf"))
    print(f"  {len(wpu_pdfs)} PDF-filer i {files_wpu.name}/")
    print(f"  parallellt: {args.jobs} processer\n")

    counts = {"better": 0, "kept": 0, "new": 0, "ocr": 0, "skip": 0}

    with ProcessPoolExecutor(
        max_workers=args.jobs,
        initializer=_init_worker,
        initargs=(palme_map, use_hunspell),
    ) as pool:
        futures = {
            pool.submit(
                _process_one,
                str(pdf),
                str(text_dir),
                str(TEXT_WPU),
                args.dry_run,
                args.rebuild,
                args.margin,
                args.ocr_threshold,
                args.per_file_jobs,
            ): pdf
            for pdf in wpu_pdfs
        }

        for fut in as_completed(futures):
            pdf = futures[fut]
            try:
                res = fut.result()
            except Exception as exc:
                print(f"  [fel] {pdf.name}: {exc}", file=sys.stderr)
                continue
            for line in res["lines"]:
                print(line)
            for err in res["errors"]:
                print(err, file=sys.stderr)
            cat = res["category"]
            if cat in counts:
                counts[cat] += 1

    print(
        f"\nKlart: {counts['better']} ersatta (wpu bättre), "
        f"{counts['kept']} behållna (palme/wpu bättre), "
        f"{counts['new']} nya (direkt), {counts['ocr']} OCR-skannade, "
        f"{counts['skip']} hoppade."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
