#!/usr/bin/env python3
"""macOS Vision Framework OCR-wrapper kring `ocrit` CLI.

Användning:
    python ocr_vision.py --in <dir-eller-pdf> --out <dir>

För PDF:er: rendera sidor med pypdfium2 till PNG, kör ocrit per sida, samla
output till en .txt med ``\f`` mellan sidorna.

Förkrav:
    brew install insidegui/tap/ocrit
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    from errors_log import log_error
except Exception:  # pragma: no cover
    def log_error(component: str, item: str, message: str) -> None:
        pass


def ensure_ocrit() -> None:
    if shutil.which("ocrit"):
        return
    print(
        "ocrit saknas. Installera med:\n"
        "    brew install insidegui/tap/ocrit",
        file=sys.stderr,
    )
    sys.exit(1)


def ocr_image(png_path: Path) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        out = subprocess.run(
            ["ocrit", str(png_path), "-o", str(tmpdir)],
            capture_output=True, timeout=600, check=False,
        )
        if out.returncode != 0:
            raise RuntimeError(out.stderr.decode("utf-8", errors="replace")[:400])
        candidate = tmpdir / (png_path.stem + ".txt")
        if candidate.exists():
            return candidate.read_text(encoding="utf-8", errors="replace")
        for f in tmpdir.glob("*.txt"):
            return f.read_text(encoding="utf-8", errors="replace")
    return ""


def ocr_pdf(pdf_path: Path, out_txt: Path, dpi: int) -> None:
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(str(pdf_path))
    pages: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        for i, page in enumerate(pdf, start=1):
            img = page.render(scale=dpi / 72).to_pil()
            png = tmpdir / f"page-{i:03d}.png"
            img.save(str(png))
            try:
                text = ocr_image(png)
            except Exception as e:  # noqa: BLE001
                log_error("ocr_vision", f"{pdf_path.name}#p{i}", str(e))
                text = ""
            pages.append(text)
    out_txt.write_text("\f".join(pages), encoding="utf-8")


def process(in_path: Path, out_dir: Path, dpi: int) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)

    if in_path.is_file() and in_path.suffix.lower() == ".pdf":
        targets = [in_path]
    elif in_path.is_dir():
        targets = sorted(in_path.glob("*.pdf"))
        # Plus rena bilder direkt i katalogen
        for ext in (".png", ".jpg", ".jpeg", ".tif", ".tiff"):
            targets += sorted(in_path.glob(f"*{ext}"))
    else:
        print(f"--in måste vara PDF eller katalog: {in_path}", file=sys.stderr)
        return 1

    if not targets:
        print(f"Inga filer att processa i {in_path}", file=sys.stderr)
        return 1

    n_done = 0
    for f in targets:
        out_txt = out_dir / f"{f.stem}.txt"
        if out_txt.exists() and out_txt.stat().st_size > 0:
            print(f"[hoppar] {f.stem}")
            continue
        try:
            if f.suffix.lower() == ".pdf":
                ocr_pdf(f, out_txt, dpi)
            else:
                txt = ocr_image(f)
                out_txt.write_text(txt, encoding="utf-8")
            n_done += 1
            print(f"[ok] {f.stem}")
        except Exception as e:  # noqa: BLE001
            print(f"[fel] {f.stem}: {e}", file=sys.stderr)
            log_error("ocr_vision", f.name, str(e))
    print(f"Klart: {n_done} filer.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inp",
                    default=os.environ.get("IN"),
                    required=not os.environ.get("IN"),
                    help="PDF eller katalog med PDF/bilder (env: IN)")
    ap.add_argument("--out", dest="outdir",
                    default=os.environ.get("OUT"),
                    required=not os.environ.get("OUT"),
                    help="output-katalog för .txt (env: OUT)")
    ap.add_argument("--dpi", type=int,
                    default=int(os.environ.get("DPI", "300")),
                    help="render-DPI för PDF-sidor (default: 300)")
    args = ap.parse_args()

    ensure_ocrit()
    return process(Path(args.inp), Path(args.outdir), args.dpi)


if __name__ == "__main__":
    sys.exit(main())
