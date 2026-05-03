#!/bin/bash
# Kör om OCR på filer med dåligt textlager.
#
# Plockar text-layer-filer ur quality.csv vars score är under tröskeln,
# raderar deras .txt + .pdf i ocr/, och kör ocrmypdf --redo-ocr som
# tar bort det dåliga textlagret och OCR:ar om från originalbilden.
#
# Kör:
#   python quality.py            # generera/uppdatera quality.csv
#   ./redo_ocr.sh                # tröskel 50, bara source=text-layer
#   THRESHOLD=70 ./redo_ocr.sh   # mer aggressiv
#   SOURCE=ocr ./redo_ocr.sh     # kör om dåliga Tesseract-filer också

set -u

ROOT=${ROOT:-$HOME/projects/palmemordsarkivet}
IN=${IN:-$ROOT/files}
OCR=${OCR:-$ROOT/ocr}
TXT=${TXT:-$ROOT/text}
CSV=${CSV:-$ROOT/quality.csv}
THRESHOLD=${THRESHOLD:-50}
SOURCE=${SOURCE:-text-layer}      # text-layer | ocr | any
JOBS=${JOBS:-4}
PER_FILE_JOBS=${PER_FILE_JOBS:-2}

[ -f "$CSV" ] || { echo "Saknar $CSV — kör quality.py först."; exit 1; }
command -v ocrmypdf >/dev/null || { echo "saknar ocrmypdf"; exit 1; }
command -v pdftotext >/dev/null || { echo "saknar pdftotext"; exit 1; }

# Plocka filnamnen ur CSV: kolumn 1 = file, 2 = source, 3 = score
mapfile -t TARGETS < <(
  awk -F, -v t="$THRESHOLD" -v s="$SOURCE" '
    NR == 1 { next }
    {
      file = $1; source = $2; score = $3 + 0
      if (score < t && (s == "any" || source == s)) {
        sub(/\.txt$/, "", file)
        print file
      }
    }
  ' "$CSV"
)

if [ ${#TARGETS[@]} -eq 0 ]; then
  echo "Inga filer matchade (THRESHOLD=$THRESHOLD, SOURCE=$SOURCE)."
  exit 0
fi

echo "Kör om OCR på ${#TARGETS[@]} filer (THRESHOLD=$THRESHOLD, SOURCE=$SOURCE)..."

redo_one() {
  local base="$1" pdf out_pdf out_txt log
  pdf="$IN/$base.pdf"
  out_pdf="$OCR/$base.pdf"
  out_txt="$TXT/$base.txt"

  [ -f "$pdf" ] || { echo "  SAKNAS: $pdf"; return 1; }

  echo "[redo] $base"
  rm -f "$out_pdf" "$out_txt"
  log=$(mktemp)
  if ocrmypdf \
        -l swe \
        --redo-ocr \
        --rotate-pages \
        --deskew \
        --clean \
        --jobs "$PER_FILE_JOBS" \
        --quiet \
        "$pdf" "$out_pdf" 2>"$log"; then
    pdftotext -layout "$out_pdf" "$out_txt"
    rm -f "$log"
  else
    echo "[fel] $base — se loggen nedan:" >&2
    sed 's/^/    /' "$log" >&2
    rm -f "$log"
    return 1
  fi
}
export -f redo_one
export IN OCR TXT PER_FILE_JOBS

printf '%s\n' "${TARGETS[@]}" \
  | xargs -I {} -P "$JOBS" bash -c 'redo_one "$@"' _ {}

echo
echo "Klart. Kör 'python quality.py' igen för att se förbättringen."
