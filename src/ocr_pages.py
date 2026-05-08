#!/usr/bin/env python3
"""Per-sida OCR-pipeline.

Renderar en PDF sida för sida och OCR:ar varje sida individuellt så att
enskilda dåliga sidor kan om-OCR:as med en annan motor utan att hela filen
måste köras om.

Output i ``<out_dir>/<stem>/``:
    page-NNN.png   — render (raderas inte; idempotency)
    page-NNN.txt   — OCR-text
    page-NNN.json  — text + score (heuristik från quality.score_text)

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
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

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
    for i, page in enumerate(pdf, start=1):
        yield i, page.render(scale=dpi / 72).to_pil()


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


class _SuryaState:
    rec = None
    det = None


def _surya_load():
    if _SuryaState.rec is not None:
        return _SuryaState.rec, _SuryaState.det
    from surya.detection import DetectionPredictor
    from surya.foundation import FoundationPredictor
    from surya.recognition import RecognitionPredictor
    foundation = FoundationPredictor()
    _SuryaState.rec = RecognitionPredictor(foundation)
    _SuryaState.det = DetectionPredictor()
    return _SuryaState.rec, _SuryaState.det


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
            patched += 1

        # Skriv via tempfil om src == dst
        if dst_pdf.resolve() == src_pdf.resolve():
            tmp = dst_pdf.with_suffix(dst_pdf.suffix + ".tmp")
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inp",
                    default=os.environ.get("IN"),
                    required=not os.environ.get("IN"),
                    help="PDF-fil (env: IN)")
    ap.add_argument("--out-dir",
                    default=os.environ.get("OUT_DIR"),
                    required=not os.environ.get("OUT_DIR"),
                    help="output-katalog (env: OUT_DIR)")
    ap.add_argument("--engine",
                    default=os.environ.get("ENGINE", "tesseract"),
                    choices=["tesseract", "vision", "surya"],
                    help="OCR-motor (default: tesseract)")
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
                    help="om satt + engine=surya: patcha textlagret i "
                         "<ocr-dir>/<stem>.pdf med Surya-text på de "
                         "OCR:ade sidorna (default: av)")
    ap.add_argument("--no-update-pdf", action="store_true",
                    help="stäng av PDF-textlager-patchen även om --ocr-dir är satt")
    args = ap.parse_args()

    pdf = Path(args.inp)
    if not pdf.exists():
        print(f"Saknar {pdf}", file=sys.stderr)
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

    page_texts: dict[int, str] = {}
    page_lines: dict[int, list[dict]] = {}
    want_pdf_patch = (
        args.engine == "surya"
        and args.ocr_dir
        and not args.no_update_pdf
    )
    n_total = 0
    n_skipped = 0
    n_done = 0
    for page_num, image in render_pages(pdf, args.dpi):
        n_total += 1
        if only_pages is not None and page_num not in only_pages:
            # Behåll möjligheten att läsa befintlig text för slutsamlad txt
            existing = stem_dir / f"page-{page_num:03d}.txt"
            if existing.exists():
                page_texts[page_num] = existing.read_text(
                    encoding="utf-8", errors="replace"
                )
            continue

        txt_path = stem_dir / f"page-{page_num:03d}.txt"
        json_path = stem_dir / f"page-{page_num:03d}.json"
        png_path = stem_dir / f"page-{page_num:03d}.png"

        if txt_path.exists() and txt_path.stat().st_size > 0:
            page_texts[page_num] = txt_path.read_text(
                encoding="utf-8", errors="replace"
            )
            n_skipped += 1
            continue

        try:
            if args.engine == "surya":
                if want_pdf_patch:
                    text, lines = ocr_surya_lines(image)
                    page_lines[page_num] = lines
                else:
                    text = ocr_surya(image)
            else:
                # spara PNG (idempotent)
                if not png_path.exists():
                    image.save(str(png_path))
                if args.engine == "vision":
                    text = ocr_vision(png_path)
                else:
                    text = ocr_tesseract(png_path, args.langs)
        except Exception as e:  # noqa: BLE001
            print(f"  [{pdf.stem} p{page_num}] FEL: {e}", file=sys.stderr)
            log_error("ocr_pages", f"{pdf.name}#p{page_num}", str(e))
            continue

        try:
            scored = score_text(text, use_hunspell=False)
        except Exception:
            scored = {"chars": len(text), "score": 0.0}

        txt_path.write_text(text, encoding="utf-8")
        meta = {
            "file": pdf.name,
            "page": page_num,
            "engine": args.engine,
            **scored,
        }
        json_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        page_texts[page_num] = text
        n_done += 1
        print(f"  [{pdf.stem} p{page_num:03d}] {len(text):5d} tecken "
              f"score={scored.get('score', 0)}", flush=True)

    # Slutsamla
    combined = out_dir / f"{pdf.stem}.txt"
    if page_texts:
        ordered = [page_texts[i] for i in sorted(page_texts.keys())]
        combined.write_text("\f".join(ordered), encoding="utf-8")

    print(f"Klart {pdf.stem}: {n_done} OCR:ade, {n_skipped} hoppade, "
          f"{n_total} sidor totalt.")

    if want_pdf_patch and page_lines:
        ocr_pdf = Path(args.ocr_dir) / f"{pdf.stem}.pdf"
        if not ocr_pdf.exists():
            print(f"  [pdf-patch] {ocr_pdf} finns inte — hoppar.",
                  file=sys.stderr)
        else:
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
