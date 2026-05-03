#!/bin/bash
# OCR + textextraktion för palmemordsarkivet.
# - Snabbkoll: hoppar över OCR om PDF:en redan har användbart textlager
# - OCR med svenska + deskew/clean/rotate för scannade sidor
# - Parallelliserar över filer (GNU parallel)
# - Idempotent: hoppar över filer där .txt redan finns
#
# Krav: brew install ocrmypdf tesseract-lang poppler unpaper parallel

set -u

IN=${IN:-$HOME/projects/palmemordsarkivet/files}
OCR=${OCR:-$HOME/projects/palmemordsarkivet/ocr}
TXT=${TXT:-$HOME/projects/palmemordsarkivet/text}
JOBS=${JOBS:-4}                    # antal filer parallellt
PER_FILE_JOBS=${PER_FILE_JOBS:-2}  # OCR-trådar per fil
MIN_TEXT_CHARS=${MIN_TEXT_CHARS:-200}  # tröskel för "har redan text"

mkdir -p "$OCR" "$TXT"

for cmd in ocrmypdf pdftotext; do
  command -v "$cmd" >/dev/null || { echo "saknar verktyg: $cmd"; exit 1; }
done

process_one() {
  local f="$1" base out_pdf out_txt existing
  base=$(basename "$f" .pdf)
  out_pdf="$OCR/$base.pdf"
  out_txt="$TXT/$base.txt"

  [ -s "$out_txt" ] && { echo "[hoppar] $base"; return 0; }

  # Snabbkoll: har originalet redan användbart textlager?
  existing=$(pdftotext -q -layout "$f" - 2>/dev/null | tr -d '[:space:]' | wc -c)
  if [ "$existing" -gt "$MIN_TEXT_CHARS" ]; then
    echo "[text-finns] $base"
    pdftotext -layout "$f" "$out_txt"
    return 0
  fi

  echo "[ocr] $base"
  local log
  log=$(mktemp)
  if ocrmypdf \
        -l swe \
        --skip-text \
        --rotate-pages \
        --deskew \
        --clean \
        --jobs "$PER_FILE_JOBS" \
        --quiet \
        "$f" "$out_pdf" 2>"$log"; then
    pdftotext -layout "$out_pdf" "$out_txt"
    rm -f "$log"
  else
    echo "[fel] $base — se loggen nedan:" >&2
    sed 's/^/    /' "$log" >&2
    rm -f "$log"
    return 1
  fi
}
export -f process_one
export IN OCR TXT PER_FILE_JOBS MIN_TEXT_CHARS

find "$IN" -name '*.pdf' -print0 \
  | xargs -0 -n 1 -P "$JOBS" -I {} bash -c 'process_one "$@"' _ {}
