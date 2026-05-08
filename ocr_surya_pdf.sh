#!/bin/bash
# Surya-OCR + inbäddat osynligt textlager i sökbar PDF (motsvarar Tesseracts
# output i ocr/ men med Surya-textens kvalitet).
#
# Användning:
#   ./ocr_surya_pdf.sh --in worst_files --out ocr --text-out text_surya
#   ./ocr_surya_pdf.sh -- --help              # full src/ocr_surya_pdf.py-hjälp

set -eu

usage() {
  cat <<EOF
Användning: $(basename "$0") [flaggor]

Wrapper-flaggor:
  --root DIR              projektrot ($PWD om ej satt via ROOT)
  -h, --help              visa denna hjälp

Skickas vidare till src/ocr_surya_pdf.py (alla okända flaggor passerar igenom):
  --in DIR                katalog med ingångs-PDF:er
  --out DIR               katalog för PDF:er med inbäddad Surya-text
  --text-out DIR          valfri katalog för .txt-filer
  --dpi N                 render-DPI (default: 200)

För full src/ocr_surya_pdf.py-hjälp: ./ocr_surya_pdf.sh -- --help
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

exec python src/ocr_surya_pdf.py ${extra[@]+"${extra[@]}"}
