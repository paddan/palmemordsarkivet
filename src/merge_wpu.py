#!/usr/bin/env python3
"""Jämför wpu-text mot palme-text och behåll bästa versionen.

Båda sidor måste ha gått genom ``ocr_tesseract.sh`` så att ``text/<stem>.txt``
och ``ocr/<stem>.pdf`` finns för varje PDF i ``downloaded/files/`` respektive
``downloaded/wpu_files/``. Den här modulen jämför poäng för matchande
dokument-ID och raderar förlorarens text/+ocr/-filer.

Tre utfall per wpu-fil:
  - vinner mot matchande palme  → palmes text/+ocr/ raderas (``better``)
  - förlorar mot matchande palme → wpus text/+ocr/ raderas  (``lost``)
  - inom margin / saknar match   → båda behålls               (``kept``/``new``)

Idempotent via ``wpu_decisions``-tabellen i ``state.db``.

Kör:
    python merge_wpu.py            # standardkörning
    python merge_wpu.py --dry-run  # visa beslut utan att radera
    python merge_wpu.py --rebuild  # ignorera wpu_decisions-tabellen
    python merge_wpu.py --jobs 8   # parallella processer
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES_WPU = ROOT / "downloaded" / "wpu_files"
TEXT_DIR = ROOT / "generated" / "text"
OCR_DIR = ROOT / "generated" / "ocr"

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
import db as state_db  # noqa: E402
from download_wpu import palme_id_keys, wpu_id_keys  # noqa: E402
from quality import has_hunspell_swe, score_text  # noqa: E402

DEFAULT_MARGIN = 5  # wpu måste vara minst N poäng bättre för att vinna

_PALME_MAP: dict[tuple, list[Path]] | None = None
_USE_HUNSPELL: bool = False


def decide(wpu_score: float, palme_score: float, margin: float) -> str:
    """Bestäm utfall för ett wpu/palme-par. Returnerar 'wpu', 'palme' eller 'tie'."""
    if wpu_score > palme_score + margin:
        return "wpu"
    if palme_score > wpu_score + margin:
        return "palme"
    return "tie"


def build_palme_key_map(text_dir: Path) -> dict[tuple, list[Path]]:
    """Bygg mapping ID-nyckel → lista av textfiler i text/."""
    mapping: defaultdict[tuple, list[Path]] = defaultdict(list)
    for txt in text_dir.glob("*.txt"):
        for key in palme_id_keys(txt.name):
            mapping[key].append(txt)
    return dict(mapping)


def cleanup_phantom_decisions(conn, text_dir: Path) -> int:
    """Radera wpu_decisions-rader vars text/<stem>.txt inte finns.

    Skyddar mot en historisk bugg där merge_wpu markerade filer som decided
    även när OCR inte hunnit producera texten. Idempotent — säker att köra
    varje gång.
    """
    rows = conn.execute(
        """SELECT w.pdf_stem, p.tesseract_done_at
           FROM wpu_decisions AS w
           LEFT JOIN pdf_files AS p ON p.pdf_stem = w.pdf_stem"""
    ).fetchall()
    bogus = [r["pdf_stem"] for r in rows
             if not (text_dir / f"{r['pdf_stem']}.txt").exists()
             and r["tesseract_done_at"] is None]
    if bogus:
        conn.executemany(
            "DELETE FROM wpu_decisions WHERE pdf_stem=?",
            [(s,) for s in bogus],
        )
        conn.commit()
    return len(bogus)


def _init_worker(palme_map: dict[tuple, list[Path]], use_hunspell: bool) -> None:
    global _PALME_MAP, _USE_HUNSPELL
    _PALME_MAP = palme_map
    _USE_HUNSPELL = use_hunspell


def _delete_pair(text_path: Path, ocr_dir: Path, dry_run: bool) -> None:
    if dry_run:
        return
    text_path.unlink(missing_ok=True)
    (ocr_dir / f"{text_path.stem}.pdf").unlink(missing_ok=True)
    # Ingen markörfil behövs: filen har redan tesseract_done_at i state.db, så
    # ocr_tesseract.sh hoppar över den även när text/+ocr/ raderats här.


def _process_one(
    pdf_path: str,
    text_dir_str: str,
    ocr_dir_str: str,
    dry_run: bool,
    rebuild: bool,
    margin: float,
) -> dict:
    pdf = Path(pdf_path)
    text_dir = Path(text_dir_str)
    ocr_dir = Path(ocr_dir_str)
    palme_map = _PALME_MAP or {}
    use_hunspell = _USE_HUNSPELL

    stem = pdf.stem

    result: dict = {"category": None, "lines": [], "errors": []}

    # Egen connection per worker — ProcessPoolExecutor delar inte sqlite-objekt.
    conn = state_db.connect()
    state_db.init_schema(conn)

    try:
        if not rebuild and state_db.wpu_decided(conn, stem):
            result["category"] = "skip"
            return result

        wpu_txt = text_dir / f"{stem}.txt"
        if not wpu_txt.exists():
            # Markera INTE som decided — OCR hann inte bli klar. Nästa körning
            # försöker igen när text/<stem>.txt finns.
            result["lines"].append(
                f"[saknar text] {stem[:70]} — kör ocr_tesseract.sh på downloaded/wpu_files först"
            )
            result["category"] = "skip"
            return result

        wpu_raw = wpu_txt.read_text(encoding="utf-8", errors="replace")
        wpu_score = score_text(wpu_raw, use_hunspell=use_hunspell)["score"]

        matched: list[Path] = []
        for key in wpu_id_keys(pdf.name):
            for p in palme_map.get(key, []):
                if p != wpu_txt and p not in matched:
                    matched.append(p)

        if not matched:
            result["lines"].append(f"[ensam]    {stem[:60]:60s} score={wpu_score:.0f}")
            result["category"] = "new"
            if not dry_run:
                state_db.mark_wpu_decided(conn, stem)
            return result

        wpu_alive = True
        won_any = False
        for palme_txt in matched:
            if not palme_txt.exists():
                continue  # raderad i tidigare iteration
            palme_raw = palme_txt.read_text(encoding="utf-8", errors="replace")
            palme_score = score_text(palme_raw, use_hunspell=use_hunspell)["score"]

            outcome = decide(wpu_score, palme_score, margin)
            if outcome == "wpu":
                result["lines"].append(
                    f"[wpu wins]  {stem[:30]:30s} ({wpu_score:.0f}) > "
                    f"palme={palme_txt.stem[:30]} ({palme_score:.0f}) — raderar palme"
                )
                _delete_pair(palme_txt, ocr_dir, dry_run)
                won_any = True
            elif outcome == "palme":
                result["lines"].append(
                    f"[palme wins] {palme_txt.stem[:28]:28s} ({palme_score:.0f}) > "
                    f"wpu={stem[:30]} ({wpu_score:.0f}) — raderar wpu"
                )
                _delete_pair(wpu_txt, ocr_dir, dry_run)
                wpu_alive = False
                break
            else:
                result["lines"].append(
                    f"[oavgjort]  {stem[:30]:30s} ({wpu_score:.0f}) ≈ "
                    f"palme={palme_txt.stem[:30]} ({palme_score:.0f}) — behåller båda"
                )

        if won_any:
            result["category"] = "better"
        elif wpu_alive:
            result["category"] = "kept"
        else:
            result["category"] = "lost"

        if not dry_run and not result["errors"]:
            state_db.mark_wpu_decided(conn, stem)
        return result
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--dry-run", action="store_true",
                    help="visa beslut utan att radera")
    ap.add_argument("--rebuild", action="store_true",
                    help="ignorera wpu_decisions-tabellen")
    ap.add_argument("--margin", type=float, default=DEFAULT_MARGIN,
                    help=f"min poängfördel för att vinna (default: {DEFAULT_MARGIN})")
    ap.add_argument("--files-wpu", dest="wpu_dir", default=str(FILES_WPU),
                    help=f"katalog med wpu-PDF:er (default: {FILES_WPU})")
    ap.add_argument("--text-dir", default=str(TEXT_DIR),
                    help=f"text-katalog (default: {TEXT_DIR})")
    ap.add_argument("--ocr-dir", default=str(OCR_DIR),
                    help=f"ocr-katalog (default: {OCR_DIR})")
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 4)),
                    help="antal parallella processer (default: cpu_count)")
    args = ap.parse_args()

    wpu_dir = Path(args.wpu_dir)
    text_dir = Path(args.text_dir)
    ocr_dir = Path(args.ocr_dir)

    if not wpu_dir.is_dir():
        print(f"Saknar {wpu_dir}/", file=sys.stderr)
        return 1
    if not text_dir.is_dir():
        print(f"Saknar {text_dir}/ — kör ocr_tesseract.sh först.", file=sys.stderr)
        return 1

    # Säkerställ att schemat finns innan workerprocesser försöker skriva.
    init_conn = state_db.connect()
    state_db.init_schema(init_conn)
    if args.rebuild and not args.dry_run:
        init_conn.execute("DELETE FROM wpu_decisions")
        init_conn.commit()
    elif not args.dry_run:
        # Självläka fantom-beslut: rader skrivna när text saknades. Kan annars
        # hindra merge_wpu från att fatta riktigt beslut när OCR producerat texten.
        removed = cleanup_phantom_decisions(init_conn, text_dir)
        if removed:
            print(f"Rensade {removed} fantom-rader ur wpu_decisions (text saknas på disk).")
    init_conn.close()

    use_hunspell = has_hunspell_swe()
    if not use_hunspell:
        print("hunspell saknas — hoppar över ordbokskontroll", file=sys.stderr)

    print("Bygger palme-ID-karta…")
    palme_map = build_palme_key_map(text_dir)
    print(f"  {len(palme_map)} ID-nycklar i {text_dir.name}/")

    wpu_pdfs = sorted(wpu_dir.glob("*.pdf"))
    print(f"  {len(wpu_pdfs)} PDF-filer i {wpu_dir.name}/")
    print(f"  parallellt: {args.jobs} processer\n")

    counts = {"better": 0, "kept": 0, "lost": 0, "new": 0, "skip": 0}

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
                str(ocr_dir),
                args.dry_run,
                args.rebuild,
                args.margin,
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
        f"\nKlart: {counts['better']} wpu vann (palme raderad), "
        f"{counts['lost']} wpu förlorade (wpu raderad), "
        f"{counts['kept']} oavgjorda (båda kvar), "
        f"{counts['new']} ensamma wpu, "
        f"{counts['skip']} hoppade."
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nAvbrutet.", file=sys.stderr)
        sys.exit(130)
