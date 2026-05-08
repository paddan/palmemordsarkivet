#!/bin/bash
# Per-sida OCR (Tesseract/Vision/Surya) — för riktad om-OCR på enstaka
# dåliga sidor. Skriver page-NNN.txt + page-NNN.json + ihopslagen <stem>.txt.
#
# Användning:
#   ./ocr_pages.sh --in files/foo.pdf --out-dir text_pages
#   ./ocr_pages.sh --in files/foo.pdf --out-dir text_pages --engine surya
#   ./ocr_pages.sh --in files/foo.pdf --out-dir text_pages --pages 3,7,12
#   ./ocr_pages.sh -- --help                  # full src/ocr_pages.py-hjälp

set -eu

usage() {
  cat <<EOF
Användning: $(basename "$0") [flaggor]

Wrapper-flaggor:
  --root DIR              projektrot ($PWD om ej satt via ROOT)
  -h, --help              visa denna hjälp

Skickas vidare till src/ocr_pages.py (alla okända flaggor passerar igenom):
  --in PDF                PDF-fil
  --out-dir DIR           output-katalog
  --engine ENGINE         tesseract | vision | surya (default: tesseract)
  --langs S               tesseract-språk (default: swe)
  --dpi N                 render-DPI (default: 300)
  --pages N,N,...         kommaseparerade sidnummer (1-baserade); default alla

För full src/ocr_pages.py-hjälp: ./ocr_pages.sh -- --help
EOF
}

ROOT=${ROOT:-$(cd "$(dirname "$0")" && pwd)}
extra=()

while [ $# -gt 0 ]; do
  case "$1" in
    --root)    ROOT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    --)        shift; extra+=("$@"); break ;;
    *)         extra+=("$1"); shift ;;
  esac
done

cd "$ROOT"

# shellcheck disable=SC1091
source .venv/bin/activate

exec python src/ocr_pages.py ${extra[@]+"${extra[@]}"}
