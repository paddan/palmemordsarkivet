#!/usr/bin/env python3
"""Per-sida OCR-pipeline.

Renderar en PDF sida för sida och OCR:ar varje sida individuellt så att
enskilda dåliga sidor kan om-OCR:as med en annan motor utan att hela filen
måste köras om.

Output i ``<out_dir>/<stem>/``:
    page-NNN.png   — render för Tesseract/Vision

OCR-text och metadata lagras i ``state.db``-tabellen ``pdf_pages``.

Idempotens och metadata (engine, score) skrivs till ``state.db`` via
``db.record_page`` — ingen .json-markör skrivs längre.

Sammansatt ``<out_dir>/<stem>.txt`` skrivs på slutet, sidor separerade med ``\f``.

Engines:
    tesseract  — subprocess till `tesseract`
    vision     — subprocess till `ocrit` (macOS Vision Framework)
    surya      — Surya-modeller, samma stack som ocr_surya.py

Kör:
    python ocr_pages.py --in path/to/foo.pdf --out-dir text_pages
    python ocr_pages.py --in foo.pdf --out-dir out --engine surya
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db as state_db

try:
    from quality import score_text  # type: ignore
except Exception:  # pragma: no cover
    def score_text(text: str, use_hunspell: bool = False) -> dict:
        return {"chars": len(text), "score": 0.0}

try:
    from errors_log import log_error
except Exception:  # pragma: no cover
    def log_error(component: str, item: str, message: str) -> None:
        pass


def render_pages(pdf_path: Path, dpi: int):
    import pypdfium2 as pdfium  # local import — Surya/Vision-pad kanske saknar
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        for i in range(len(pdf)):
            try:
                page = pdf[i]
                yield i + 1, page.render(scale=dpi / 72).to_pil()
            except pdfium.PdfiumError as exc:
                print(f"  [render_pages] sida {i + 1} kunde inte laddas: {exc}", file=sys.stderr)
    finally:
        pdf.close()


def ocr_tesseract(png_path: Path, langs: str = "swe") -> str:
    if not shutil.which("tesseract"):
        raise RuntimeError("tesseract saknas i PATH")
    cmd = ["tesseract", str(png_path), "-", "-l", langs]
    out = subprocess.run(cmd, capture_output=True, timeout=600, check=False)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.decode("utf-8", errors="replace")[:400])
    return out.stdout.decode("utf-8", errors="replace")


def ocr_vision(png_path: Path) -> str:
    if not shutil.which("ocrit"):
        raise RuntimeError("ocrit saknas — installera: brew install insidegui/tap/ocrit")
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        cmd = ["ocrit", str(png_path), "-o", str(tmpdir)]
        out = subprocess.run(cmd, capture_output=True, timeout=600, check=False)
        if out.returncode != 0:
            raise RuntimeError(out.stderr.decode("utf-8", errors="replace")[:400])
        # ocrit skriver <stem>.txt till -o-katalogen
        candidate = tmpdir / (png_path.stem + ".txt")
        if candidate.exists():
            return candidate.read_text(encoding="utf-8", errors="replace")
        # fallback: första .txt:n
        for f in tmpdir.glob("*.txt"):
            return f.read_text(encoding="utf-8", errors="replace")
    return ""


@lru_cache(maxsize=1)
def _surya_load():
    """Laddar Surya-modellerna en gång per process (cachas via lru_cache)."""
    from surya.detection import DetectionPredictor
    from surya.foundation import FoundationPredictor
    from surya.recognition import RecognitionPredictor
    foundation = FoundationPredictor()
    rec = RecognitionPredictor(foundation)
    det = DetectionPredictor()
    return rec, det


def ocr_surya(image) -> str:
    rec, det = _surya_load()
    preds = rec([image], det_predictor=det)
    if not preds:
        return ""
    lines = [ln.text for ln in preds[0].text_lines]
    return "\n".join(lines)


def ocr_surya_lines(image) -> tuple[str, list[dict]]:
    """Som ocr_surya men returnerar även bboxes per rad i bildkoordinater."""
    rec, det = _surya_load()
    preds = rec([image], det_predictor=det)
    if not preds:
        return "", []
    lines: list[dict] = []
    for ln in preds[0].text_lines:
        if not ln.text.strip():
            continue
        x0, y0, x1, y1 = ln.bbox
        lines.append({"text": ln.text, "bbox": [x0, y0, x1, y1]})
    return "\n".join(line["text"] for line in lines), lines


def update_pdf_text_layer(
    src_pdf: Path, dst_pdf: Path, page_lines: dict[int, list[dict]], dpi: int
) -> int:
    """Skriv ny PDF där angivna sidor får Surya-rader som osynligt textlager.

    src_pdf läses, befintligt textlager på de berörda sidorna redaktas bort,
    och Surya-textraderna skrivs in via insert_textbox med render_mode=3
    (osynlig). Övriga sidor lämnas orörda. Skrivs till dst_pdf (kan vara
    samma som src_pdf — vi använder en tempfil).

    Returnerar antal sidor som faktiskt patchades.
    """
    import pymupdf  # local import — pymupdf är optional

    scale = 72.0 / dpi
    doc = pymupdf.open(str(src_pdf))
    patched = 0
    try:
        for page_num, lines in page_lines.items():
            if page_num < 1 or page_num > len(doc) or not lines:
                continue
            page = doc[page_num - 1]
            # Rensa befintligt textlager på sidan
            for block in page.get_text("dict")["blocks"]:
                if block.get("type") == 0:  # text-block
                    page.add_redact_annot(block["bbox"])
            page.apply_redactions(images=0, graphics=0)

            # Lägg in Surya-text osynligt
            for line in lines:
                x0, y0, x1, y1 = line["bbox"]
                rect = pymupdf.Rect(
                    x0 * scale, y0 * scale, x1 * scale, y1 * scale
                )
                fontsize = max(1.0, (y1 - y0) * scale * 0.85)
                for _ in range(5):
                    rc = page.insert_textbox(
                        rect, line["text"],
                        fontsize=fontsize, fontname="helv",
                        render_mode=3, align=0,
                    )
                    if rc >= 0:
                        break
                    fontsize = max(1.0, fontsize * 0.5)
                if rc < 0:
                    raise RuntimeError(
                        f"kunde inte infoga textrad på sida {page_num}: {line['text'][:80]!r}"
                    )
            patched += 1

        # Skriv via tempfil om src == dst
        if dst_pdf.resolve() == src_pdf.resolve():
            tmp = dst_pdf.with_suffix(dst_pdf.suffix + ".tmp")
            tmp.unlink(missing_ok=True)  # rensa ev. kvarleva från krasch
            doc.save(str(tmp), garbage=4, deflate=True)
            doc.close()
            tmp.replace(dst_pdf)
        else:
            doc.save(str(dst_pdf), garbage=4, deflate=True)
            doc.close()
    except Exception:
        doc.close()
        raise
    return patched


def detect_redactions_image(
    image,
    darkness: int = 40,
    min_width_frac: float = 0.10,
    min_height: int = 8,
) -> list[tuple[int, int]]:
    """Hitta svarta maskeringsblock i PIL-bild via pixelanalys.

    Söker rader med ett sammanhängande mörkt span >= min_width_frac × sidbredd.
    Returnerar lista av (y0, y1) i bildpixlar, en tuple per funnet block.
    """
    gray = image.convert("L")
    w, h = gray.size
    data = gray.tobytes()
    min_span = int(w * min_width_frac)

    dark_rows: list[int] = []
    for y in range(h):
        row = data[y * w:(y + 1) * w]
        max_span = span = 0
        for px in row:
            if px < darkness:
                span += 1
                if span > max_span:
                    max_span = span
            else:
                span = 0
        if max_span >= min_span:
            dark_rows.append(y)

    if not dark_rows:
        return []

    blocks: list[tuple[int, int]] = []
    y0 = dark_rows[0]
    yp = dark_rows[0]
    for y in dark_rows[1:]:
        if y > yp + 3:
            if yp - y0 + 1 >= min_height:
                blocks.append((y0, yp))
            y0 = y
        yp = y
    if yp - y0 + 1 >= min_height:
        blocks.append((y0, yp))
    return blocks


def _merge_redaction_markers(
    text: str,
    blocks: list[tuple[int, int]],
    image_height: int,
    line_bboxes: list[dict] | None = None,
) -> str:
    """Infoga [MASKAD]-markeringar i text vid detekterade maskeringsblock.

    Med line_bboxes (surya-format {text, bbox: [x0, y0, x1, y1]}) används
    exakta y-koordinater. Utan dem approximeras positionen via andel av sidans
    höjd multiplicerat med antal rader.
    """
    if not blocks:
        return text

    if line_bboxes is not None:
        items: list[tuple[float, str]] = [
            ((ln["bbox"][1] + ln["bbox"][3]) / 2, ln["text"])
            for ln in line_bboxes
        ]
        for b0, b1 in blocks:
            items.append(((b0 + b1) / 2, "[MASKAD]"))
        items.sort(key=lambda x: x[0])
        return "\n".join(t for _, t in items)

    lines = text.split("\n")
    n = len(lines)
    insertions: list[tuple[int, str]] = []
    for b0, b1 in blocks:
        mid_y = (b0 + b1) / 2
        idx = max(0, min(n, round(mid_y / image_height * n)))
        insertions.append((idx, "[MASKAD]"))
    for idx, marker in sorted(insertions, key=lambda x: x[0], reverse=True):
        lines.insert(idx, marker)
    return "\n".join(lines)


def detect_redactions_file(
    pdf: Path,
    txt_file: Path,
    marker: Path,
    dpi: int = 72,
    ocr_pdf: Path | None = None,
) -> int:
    """Infoga [MASKAD] i befintlig txt-fil baserat på bildanalys av pdf.

    Renderar sidor vid låg DPI (behövs bara för att hitta stora svarta block).
    Om ocr_pdf anges används dess textlager (via PyMuPDF) för att positionera
    [MASKAD] exakt efter hur textrader faktiskt fördelar sig på sidan, istället
    för att approximera med jämn fördelning.
    Idempotent via marker-fil. Returnerar antal infogade block.
    """
    if marker.exists():
        # Markörfilen finns — synka DB om redaction_checked_at saknas (t.ex. efter
        # migrering eller om DB-uppdateringen missades vid föregående körning).
        try:
            conn = state_db.connect()
            state_db.init_schema(conn)
            row = conn.execute(
                "SELECT redaction_checked_at FROM pdf_files WHERE pdf_stem=?",
                (pdf.stem,),
            ).fetchone()
            if row is None or row["redaction_checked_at"] is None:
                source = state_db.source_for_path(pdf)
                state_db.upsert_pdf_file(
                    conn, pdf_stem=pdf.stem, source=source, pdf_path=str(pdf),
                )
                has_red = (
                    txt_file.exists()
                    and "[MASKAD]" in txt_file.read_text(encoding="utf-8", errors="replace")
                )
                state_db.mark_redaction_checked(conn, pdf.stem, has_redactions=has_red)
        except Exception:
            pass
        marker.unlink(missing_ok=True)
        return 0
    if not txt_file.exists():
        return 0

    full_text = txt_file.read_text(encoding="utf-8", errors="replace")
    pages_text = full_text.split("\f")

    # Öppna OCR-PDF en gång för alla sidor (om tillgänglig).
    ocr_doc = None
    if ocr_pdf is not None and ocr_pdf.exists():
        try:
            import pymupdf
            ocr_doc = pymupdf.open(str(ocr_pdf))
        except Exception:
            pass

    pt_per_px = 72.0 / dpi  # bildpixlar → PDF-punkter (vid dpi=72 är faktorn 1.0)

    try:
        n_blocks = 0
        updated = False
        for page_num, image in render_pages(pdf, dpi):
            page_idx = page_num - 1
            if page_idx >= len(pages_text):
                continue
            page_text = pages_text[page_idx]
            if "[MASKAD]" in page_text:
                continue
            blocks = detect_redactions_image(image)
            if not blocks:
                continue

            # Hämta y-mittpunkter för textrader från OCR-PDF:ens textlager.
            y_centers: list[float] = []
            if ocr_doc is not None and page_idx < len(ocr_doc):
                try:
                    for blk in ocr_doc[page_idx].get_text("dict")["blocks"]:
                        if blk.get("type") != 0:
                            continue
                        for ln in blk.get("lines", []):
                            y0, y1 = ln["bbox"][1], ln["bbox"][3]
                            y_centers.append((y0 + y1) / 2)
                    y_centers.sort()
                except Exception:
                    y_centers = []

            lines = page_text.split("\n")
            n = len(lines)
            insertions: list[tuple[int, str]] = []
            for b0, b1 in blocks:
                if y_centers:
                    # Exakt: räkna PDF-rader ovanför blockets mittpunkt.
                    mid_pt = (b0 + b1) / 2 * pt_per_px
                    pdf_idx = sum(1 for y in y_centers if y < mid_pt)
                    idx = max(0, min(n, round(pdf_idx / len(y_centers) * n)))
                else:
                    # Fallback: proportionell mot bildhöjd (jämn fördelning antas).
                    idx = max(0, min(n, round((b0 + b1) / 2 / image.height * n)))
                insertions.append((idx, "[MASKAD]"))
            for idx, m in sorted(insertions, key=lambda x: x[0], reverse=True):
                lines.insert(idx, m)
            pages_text[page_idx] = "\n".join(lines)
            n_blocks += len(blocks)
            updated = True
    finally:
        if ocr_doc is not None:
            ocr_doc.close()

    if updated:
        txt_file.write_text("\f".join(pages_text), encoding="utf-8")

    # Skriv till state.db — autoritativ källa (inga .redact-filer skapas längre).
    try:
        conn = state_db.connect()
        state_db.init_schema(conn)
        source = state_db.source_for_path(pdf)
        state_db.upsert_pdf_file(
            conn, pdf_stem=pdf.stem, source=source, pdf_path=str(pdf),
        )
        state_db.mark_redaction_checked(
            conn, pdf.stem, has_redactions=(n_blocks > 0),
        )
    except Exception as e:  # pragma: no cover
        log_error("ocr_pages.detect_redactions", pdf.name, f"db: {e}")

    return n_blocks


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inp",
                    default=os.environ.get("IN"),
                    required=not os.environ.get("IN"),
                    help="PDF-fil (env: IN)")
    ap.add_argument("--out-dir",
                    default=os.environ.get("OUT_DIR"),
                    help="output-katalog (env: OUT_DIR); krävs ej för --engine detect-only")
    ap.add_argument("--engine",
                    default=os.environ.get("ENGINE", "tesseract"),
                    choices=["tesseract", "vision", "surya", "detect-only"],
                    help="OCR-motor (default: tesseract). detect-only: bara redaktionsdetektering, ingen om-OCR")
    ap.add_argument("--langs",
                    default=os.environ.get("LANGS", "swe"),
                    help="tesseract-språk (default: swe)")
    ap.add_argument("--dpi", type=int,
                    default=int(os.environ.get("DPI", "300")),
                    help="render-DPI (default: 300)")
    ap.add_argument("--pages",
                    default=os.environ.get("PAGES"),
                    help="kommaseparerade sidnummer (1-baserade); default alla")
    ap.add_argument("--ocr-dir",
                    default=os.environ.get("OCR_DIR"),
                    help="katalog med OCR-ade PDF:er (generated/ocr). "
                         "detect-only: används för exakt radpositionering via PyMuPDF. "
                         "surya: patchar textlagret i <ocr-dir>/<stem>.pdf (default: av)")
    ap.add_argument("--no-update-pdf", action="store_true",
                    help="stäng av PDF-textlager-patchen även om --ocr-dir är satt")
    ap.add_argument("--no-detect-redactions", action="store_true",
                    help="stäng av automatisk detektering av maskeringsblock")
    ap.add_argument("--txt-dir",
                    default=os.environ.get("TXT_DIR"),
                    help="text-katalog för detect-only (default: <root>/generated/text)")
    args = ap.parse_args()

    pdf = Path(args.inp)
    if not pdf.exists():
        print(f"Saknar {pdf}", file=sys.stderr)
        return 1

    if args.engine == "detect-only":
        txt_dir = Path(args.txt_dir) if args.txt_dir else ROOT / "generated" / "text"
        txt_file = txt_dir / f"{pdf.stem}.txt"
        marker = txt_dir / f"{pdf.stem}.redact"
        ocr_pdf = Path(args.ocr_dir) / f"{pdf.stem}.pdf" if args.ocr_dir else None
        n = detect_redactions_file(pdf, txt_file, marker, args.dpi, ocr_pdf=ocr_pdf)
        print(f"inga" if n == 0 else f"{n} block")
        return 0

    if not args.out_dir:
        print("--out-dir krävs för alla motorer utom detect-only", file=sys.stderr)
        return 1

    out_dir = Path(args.out_dir)
    stem_dir = out_dir / pdf.stem
    stem_dir.mkdir(parents=True, exist_ok=True)

    only_pages: set[int] | None = None
    if args.pages:
        try:
            only_pages = {int(x) for x in args.pages.split(",") if x.strip()}
        except ValueError:
            print("--pages ska vara kommaseparerade heltal", file=sys.stderr)
            return 1

    page_lines: dict[int, list[dict]] = {}
    want_pdf_patch = (
        args.engine == "surya"
        and args.ocr_dir
        and not args.no_update_pdf
    )
    n_total = 0
    n_skipped = 0
    n_done = 0
    conn = state_db.connect()
    state_db.init_schema(conn)
    source = "wpu" if "wpu" in str(pdf).lower() else "files"
    state_db.upsert_pdf_file(
        conn, pdf_stem=pdf.stem, source=source, pdf_path=str(pdf),
    )
    for page_num, image in render_pages(pdf, args.dpi):
        n_total += 1
        if only_pages is not None and page_num not in only_pages:
            continue

        png_path = stem_dir / f"page-{page_num:03d}.png"

        # Idempotens-markör: db.pdf_pages-raden. Skapas alltid sist efter
        # lyckad OCR (rad nedan), så om raden finns vet vi att sidan redan körts.
        # Per-sida-text lagras i pdf_pages-tabellen och mergas in i
        # text/<stem>.txt av merge_pages — vi skriver inte page-NNN.txt längre.
        if state_db.page_exists(conn, pdf.stem, page_num):
            n_skipped += 1
            continue

        redaction_blocks: list[tuple[int, int]] = []
        if not args.no_detect_redactions:
            redaction_blocks = detect_redactions_image(image)

        try:
            if args.engine == "surya":
                if want_pdf_patch:
                    text, lines = ocr_surya_lines(image)
                    page_lines[page_num] = lines
                    if redaction_blocks:
                        text = _merge_redaction_markers(
                            text, redaction_blocks, image.height, line_bboxes=lines
                        )
                else:
                    text = ocr_surya(image)
                    if redaction_blocks:
                        text = _merge_redaction_markers(
                            text, redaction_blocks, image.height
                        )
            else:
                # spara PNG (idempotent)
                if not png_path.exists():
                    image.save(str(png_path))
                if args.engine == "vision":
                    text = ocr_vision(png_path)
                else:
                    text = ocr_tesseract(png_path, args.langs)
                if redaction_blocks:
                    text = _merge_redaction_markers(
                        text, redaction_blocks, image.height
                    )
        except Exception as e:  # noqa: BLE001
            print(f"  [{pdf.stem} p{page_num}] FEL: {e}", file=sys.stderr)
            log_error("ocr_pages", f"{pdf.name}#p{page_num}", str(e))
            continue

        try:
            scored = score_text(text, use_hunspell=False)
        except Exception:
            scored = {"chars": len(text), "score": 0.0}

        redact_suffix = f" [{len(redaction_blocks)} mask]" if redaction_blocks else ""
        state_db.record_page(
            conn, pdf_stem=pdf.stem, page_num=page_num,
            engine=args.engine, text=text, score=scored.get("score"),
        )
        n_done += 1
        print(f"  [{pdf.stem} p{page_num:03d}] {len(text):5d} tecken "
              f"score={scored.get('score', 0)}{redact_suffix}", flush=True)

    # Slutsamla — combined-fil i text_pages/ skapas inte längre. Per-sida-text
    # mergas direkt in i text/<stem>.txt av merge_pages (anropas från ocr.sh).
    print(f"Klart {pdf.stem}: {n_done} OCR:ade, {n_skipped} hoppade, "
          f"{n_total} sidor totalt.")

    # Om specifika sidor begärdes men ingen matchade (sidan finns inte i PDF:en)
    # skapar vi tomma pdf_pages-poster så att de inte hämtas igen nästa körning.
    if only_pages and n_done == 0 and n_skipped == 0:
        missing = only_pages - set(range(1, n_total + 1))
        for p in missing:
            if not state_db.page_exists(conn, pdf.stem, p):
                state_db.record_page(
                    conn, pdf_stem=pdf.stem, page_num=p,
                    engine=args.engine, text="", score=0.0,
                )
        if missing:
            print(f"  [varning] {len(missing)} begärda sidor saknas i PDF:en "
                  f"({min(missing)}–{max(missing)}), markerade som försökta.")

    if want_pdf_patch and page_lines:
        ocr_pdf = Path(args.ocr_dir) / f"{pdf.stem}.pdf"
        if not ocr_pdf.exists():
            # Wpu-PDF:er (och andra som inte gått genom ocrmypdf) saknar
            # motsvarande fil i ocr/. Kopiera källan dit som utgångspunkt så
            # patchen får något att skriva till — wpu-PDF:er har redan ett
            # textlager från wpu.nu, vi skriver bara över de patchade sidorna.
            ocr_pdf.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(pdf, ocr_pdf)
            print(f"  [pdf-patch] kopierade {pdf.name} → {ocr_pdf.parent.name}/")
        if ocr_pdf.exists():
            try:
                n = update_pdf_text_layer(
                    ocr_pdf, ocr_pdf, page_lines, args.dpi
                )
                print(f"  [pdf-patch] {ocr_pdf.name}: patchade {n} sidor")
            except Exception as e:  # noqa: BLE001
                print(f"  [pdf-patch] FEL: {e}", file=sys.stderr)
                log_error("ocr_pages.pdf_patch", pdf.name, str(e))
    return 0


if __name__ == "__main__":
    sys.exit(main())
