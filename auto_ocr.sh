#!/bin/bash
# Full OCR-pipeline: Tesseract → kvalitetsbedömning → Surya på dåliga sidor
# → ny kvalitetsbedömning. Kör alla fyra stegen sekventiellt.
#
# Användning:
#   ./auto_ocr.sh                       # full pipeline med defaults
#   ./auto_ocr.sh --threshold 60        # mer aggressiv om-OCR
#   ./auto_ocr.sh --skip-redo           # bara Tesseract + bedömning
#   ./auto_ocr.sh --help                # alla flaggor

set -eu

usage() {
  cat <<EOF
Användning: $(basename "$0") [flaggor]

  --root DIR              projektrot ($PWD om ej satt via ROOT)
  --threshold N           score-tröskel för om-OCR med Surya (default: 50)
  --skip-redo             hoppa över Surya-steget (bara Tesseract + bedömning)
  --jobs N                parallella jobb i ocr.sh och redo_ocr.sh (default: 4)
  -h, --help              visa denna hjälp

Stegen som körs:
  1. ./ocr.sh                              # Tesseract på alla nya filer
  2. ./quality.sh --per-page               # quality.csv + quality_pages.jsonl
  3. ./redo_ocr.sh --mode pages            # Surya på sidor under tröskeln
  4. ./quality.sh                          # uppdaterad quality.csv

Steg 3 hoppas över om --skip-redo eller om Surya inte är installerat.
EOF
}

ROOT=${ROOT:-$(cd "$(dirname "$0")" && pwd)}
THRESHOLD=50
SKIP_REDO=0
JOBS=4

while [ $# -gt 0 ]; do
  case "$1" in
    --root)       ROOT="$2"; shift 2 ;;
    --threshold)  THRESHOLD="$2"; shift 2 ;;
    --skip-redo)  SKIP_REDO=1; shift ;;
    --jobs)       JOBS="$2"; shift 2 ;;
    -h|--help)    usage; exit 0 ;;
    *) echo "okänd flagga: $1" >&2; usage >&2; exit 2 ;;
  esac
done

cd "$ROOT"

step() {
  echo
  echo "===== $1 ====="
}

t0=$(date +%s)

step "1/4  Tesseract-OCR (./ocr.sh --jobs $JOBS)"
./ocr.sh --jobs "$JOBS"

step "2/4  Kvalitetsbedömning (./quality.sh --per-page)"
./quality.sh --per-page

if [ "$SKIP_REDO" = "1" ]; then
  echo
  echo "===== Hoppar över steg 3 (--skip-redo) ====="
elif ! "$ROOT/.venv/bin/python" -c "import surya" 2>/dev/null; then
  echo
  echo "===== Hoppar över steg 3 (Surya inte installerat) ====="
  echo "Installera med:  .venv/bin/pip install surya-ocr 'transformers<5'"
else
  step "3/4  Om-OCR med Surya på sidor < $THRESHOLD (./redo_ocr.sh --mode pages)"
  ./redo_ocr.sh --mode pages --threshold "$THRESHOLD" --jobs "$JOBS"

  step "4/4  Uppdaterad kvalitetsbedömning"
  ./quality.sh
fi

t1=$(date +%s)
elapsed=$((t1 - t0))
echo
echo "===== Klart på ${elapsed}s ====="
