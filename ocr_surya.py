#!/usr/bin/env python3
"""OCR via Surya — alternativ till Tesseract.

Renderar varje PDF-sida med pypdfium2 och kör Surya-detektor + recognition.
Modellen är multilingual transformer-baserad och brukar slå Tesseract
markant på degraderade scans.

Kör:
    python ocr_surya.py --in /tmp/ocr_sample/files --out /tmp/ocr_sample/text_surya
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

def render_pdf(pdf_path: Path, dpi: int = 200):
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(str(pdf_path))
    return [page.render(scale=dpi / 72).to_pil() for page in pdf]


def ocr_pdf(pdf_path: Path, rec, det) -> str:
    images = render_pdf(pdf_path)
    if not images:
        return ""
    predictions = rec(images, det_predictor=det)
    pages = []
    for pred in predictions:
        lines = [line.text for line in pred.text_lines]
        pages.append("\n".join(lines))
    return "\n\n".join(pages)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="indir",
                    default=os.environ.get("IN"),
                    required=not os.environ.get("IN"),
                    help="katalog med ingångs-PDF:er (env: IN)")
    ap.add_argument("--out", dest="outdir",
                    default=os.environ.get("OUT"),
                    required=not os.environ.get("OUT"),
                    help="output-katalog för .txt (env: OUT)")
    ap.add_argument("--dpi", type=int,
                    default=int(os.environ.get("DPI", "200")),
                    help="render-DPI (default: 200)")
    args = ap.parse_args()

    indir = Path(args.indir)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    pdfs = sorted(indir.glob("*.pdf"))
    if not pdfs:
        print(f"Inga PDF:er i {indir}", file=sys.stderr)
        return 1

    print("Laddar Surya-modeller (kan ta en stund första gången)…", file=sys.stderr)
    from surya.detection import DetectionPredictor
    from surya.foundation import FoundationPredictor
    from surya.recognition import RecognitionPredictor
    foundation = FoundationPredictor()
    rec = RecognitionPredictor(foundation)
    det = DetectionPredictor()
    print(f"OCR:ar {len(pdfs)} filer…", file=sys.stderr)

    t0 = time.time()
    for i, pdf in enumerate(pdfs, 1):
        out = outdir / f"{pdf.stem}.txt"
        if out.exists() and out.stat().st_size > 0:
            print(f"[{i}/{len(pdfs)}] hoppar {pdf.stem[:60]}", file=sys.stderr)
            continue
        try:
            t1 = time.time()
            text = ocr_pdf(pdf, rec, det)
            out.write_text(text, encoding="utf-8")
            elapsed = time.time() - t1
            print(
                f"[{i}/{len(pdfs)}] {len(text):6d} tecken, {elapsed:5.1f}s — {pdf.stem[:60]}",
                file=sys.stderr,
            )
        except Exception as e:  # noqa: BLE001
            print(f"[{i}/{len(pdfs)}] FEL: {e} — {pdf.stem[:60]}", file=sys.stderr)

    print(f"\nKlart på {time.time() - t0:.0f}s.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
