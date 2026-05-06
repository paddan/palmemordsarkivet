#!/usr/bin/env python3
"""Kör Surya och bädda in osynligt textlager i PDF (sökbar PDF som ocrmypdf).

Skillnad mot `ocr_surya.py`: producerar både .txt OCH PDF med textöverdrag,
så filerna i `ocr/` får en användbar text-layer som matchar Surya-textens
kvalitet (ej Tesseract).

Förutsätter pymupdf installerat:
    pip install pymupdf

Kör:
    python ocr_surya_pdf.py --in <indir> --out ocr --text-out text_surya
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pymupdf
import pypdfium2 as pdfium
from surya.detection import DetectionPredictor
from surya.foundation import FoundationPredictor
from surya.recognition import RecognitionPredictor


def render_pdf(pdf_path: Path, dpi: int):
    pdf = pdfium.PdfDocument(str(pdf_path))
    return [page.render(scale=dpi / 72).to_pil() for page in pdf]


def write_searchable_pdf(src_pdf: Path, dst_pdf: Path, predictions, dpi: int):
    """Skriv kopia av src_pdf med osynlig textöverdrag baserat på Surya-bboxes.

    Surya-koordinater är i renderad bildupplösning (dpi). PDF använder punkter
    (1 pt = 1/72 tum). Skala: pt = px * 72 / dpi.

    PyMuPDF Page-koordinater: origo uppe vänster, y växer nedåt, enhet pt.
    page.insert_text((x, y), text) sätter textens baslinje vid (x, y).
    render_mode=3 = osynlig (varken fyllning eller kontur).
    """
    scale = 72.0 / dpi
    doc = pymupdf.open(str(src_pdf))
    for page_idx, pred in enumerate(predictions):
        if page_idx >= len(doc):
            break
        page = doc[page_idx]
        for line in pred.text_lines:
            if not line.text.strip():
                continue
            x0, y0, x1, y1 = line.bbox
            pdf_x = x0 * scale
            pdf_y_baseline = y1 * scale  # baslinje vid bbox-botten
            fontsize = max(1.0, (y1 - y0) * scale * 0.85)
            try:
                page.insert_text(
                    (pdf_x, pdf_y_baseline),
                    line.text,
                    fontsize=fontsize,
                    fontname="helv",
                    render_mode=3,
                )
            except Exception:
                # icke-renderbara glyfer → hoppa just den raden
                pass
    doc.save(str(dst_pdf), garbage=4, deflate=True)
    doc.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="indir", required=True,
                    help="katalog med ingångs-PDF:er")
    ap.add_argument("--out", dest="outdir", required=True,
                    help="katalog för PDF:er med inbäddad Surya-text")
    ap.add_argument("--text-out", dest="text_outdir",
                    help="valfri katalog för .txt-filer")
    ap.add_argument("--dpi", type=int, default=200)
    args = ap.parse_args()

    indir = Path(args.indir)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    text_outdir = Path(args.text_outdir) if args.text_outdir else None
    if text_outdir:
        text_outdir.mkdir(parents=True, exist_ok=True)

    pdfs = sorted(indir.glob("*.pdf"))
    if not pdfs:
        print(f"Inga PDF:er i {indir}", file=sys.stderr)
        return 1

    print("Laddar Surya-modeller…", file=sys.stderr)
    foundation = FoundationPredictor()
    rec = RecognitionPredictor(foundation)
    det = DetectionPredictor()
    print(f"Kör Surya + skriv sökbara PDF:er på {len(pdfs)} filer…", file=sys.stderr)

    t0 = time.time()
    for i, pdf in enumerate(pdfs, 1):
        out_pdf = outdir / pdf.name
        out_txt = text_outdir / f"{pdf.stem}.txt" if text_outdir else None
        if out_pdf.exists() and out_pdf.stat().st_size > 0:
            print(f"[{i}/{len(pdfs)}] hoppar {pdf.stem[:60]}", file=sys.stderr)
            continue
        try:
            t1 = time.time()
            images = render_pdf(pdf, dpi=args.dpi)
            if not images:
                continue
            preds = rec(images, det_predictor=det)
            write_searchable_pdf(pdf, out_pdf, preds, args.dpi)
            if out_txt:
                pages = ["\n".join(line.text for line in p.text_lines) for p in preds]
                out_txt.write_text("\n\n".join(pages), encoding="utf-8")
            print(
                f"[{i}/{len(pdfs)}] {time.time() - t1:5.1f}s — {pdf.stem[:60]}",
                file=sys.stderr,
            )
        except Exception as e:  # noqa: BLE001
            print(f"[{i}/{len(pdfs)}] FEL: {e} — {pdf.stem[:60]}", file=sys.stderr)

    print(f"\nKlart på {time.time() - t0:.0f}s.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
