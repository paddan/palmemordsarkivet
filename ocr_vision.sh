#!/bin/bash
# macOS Vision Framework via ocrit (transformer-OCR i mellansegmentet
# mellan Tesseract och Surya). Kräver: brew install insidegui/tap/ocrit
#
# Användning:
#   ./ocr_vision.sh --in files --out text_vision
#   ./ocr_vision.sh --in files/foo.pdf --out text_vision
#   ./ocr_vision.sh -- --help                 # full src/ocr_vision.py-hjälp

set -eu

usage() {
  cat <<EOF
Användning: $(basename "$0") [flaggor]

Wrapper-flaggor:
  --root DIR              projektrot ($PWD om ej satt via ROOT)
  -h, --help              visa denna hjälp

Skickas vidare till src/ocr_vision.py (alla okända flaggor passerar igenom):
  --in PATH               PDF eller katalog med PDF/bilder
  --out DIR               output-katalog för .txt
  --dpi N                 render-DPI för PDF-sidor (default: 300)

För full src/ocr_vision.py-hjälp: ./ocr_vision.sh -- --help
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

exec python src/ocr_vision.py ${extra[@]+"${extra[@]}"}
