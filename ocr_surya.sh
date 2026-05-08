#!/bin/bash
# Surya-OCR (transformer-baserad) till .txt — alternativ till Tesseract på
# degraderade scans. Långsam men hög kvalitet.
#
# Användning:
#   ./ocr_surya.sh --in files --out text_surya
#   ./ocr_surya.sh --in worst_files --out text_surya --dpi 300
#   ./ocr_surya.sh -- --help                 # full src/ocr_surya.py-hjälp

set -eu

usage() {
  cat <<EOF
Användning: $(basename "$0") [flaggor]

Wrapper-flaggor:
  --root DIR              projektrot ($PWD om ej satt via ROOT)
  -h, --help              visa denna hjälp

Skickas vidare till src/ocr_surya.py (alla okända flaggor passerar igenom):
  --in DIR                katalog med ingångs-PDF:er
  --out DIR               output-katalog för .txt
  --dpi N                 render-DPI (default: 200)

För full src/ocr_surya.py-hjälp: ./ocr_surya.sh -- --help
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

exec python src/ocr_surya.py ${extra[@]+"${extra[@]}"}
