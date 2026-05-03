#!/bin/bash
# OCR + textextraktion för palmemordsarkivet.
# - Snabbkoll: hoppar över OCR om PDF:en redan har användbart textlager
# - OCR med svenska + deskew/clean/rotate för scannade sidor
# - Parallelliserar över filer med xargs -P
# - Idempotent: hoppar över filer där .txt redan finns
#
# Lägen:
#   ./ocr.sh                       # normal: OCR:a sidor utan textlager
#   REDO_TEXT_LAYER=1 ./ocr.sh     # kör om OCR på alla PDF:er som har textlager
#                                  # (bra för PDF:er med inbäddat OCR-skräp)
#
# Krav: brew install ocrmypdf tesseract-lang poppler unpaper

set -u

ROOT=${ROOT:-$HOME/projects/palmemordsarkivet}
IN=${IN:-$ROOT/files}
OCR=${OCR:-$ROOT/ocr}
TXT=${TXT:-$ROOT/text}
TESSDATA=${TESSDATA:-$ROOT/tessdata}
USER_WORDS=${USER_WORDS:-$TESSDATA/swe.user-words}
LANGS=${LANGS:-swe+eng}            # svensk + engelsk modell
JOBS=${JOBS:-4}                    # antal filer parallellt
PER_FILE_JOBS=${PER_FILE_JOBS:-2}  # OCR-trådar per fil
MIN_TEXT_CHARS=${MIN_TEXT_CHARS:-200}  # tröskel för "har redan text"
REDO_TEXT_LAYER=${REDO_TEXT_LAYER:-0}

mkdir -p "$OCR" "$TXT"

# Använd projekt-lokal tessdata om den finns (swe_best + symlänkar till eng/osd)
if [ -f "$TESSDATA/swe.traineddata" ]; then
  export TESSDATA_PREFIX="$TESSDATA"
  echo "Använder $TESSDATA (swe_best)"
else
  echo "Tips: kör ./setup_tessdata.sh för att hämta swe_best.traineddata (mer noggrann modell)."
fi

[ -f "$USER_WORDS" ] || USER_WORDS=""
[ -n "$USER_WORDS" ] && echo "Använder user-words: $USER_WORDS ($(wc -l < "$USER_WORDS" | tr -d ' ') ord)"

for cmd in ocrmypdf pdftotext; do
  command -v "$cmd" >/dev/null || { echo "saknar verktyg: $cmd"; exit 1; }
done

run_ocr() {
  # $1 = mode (skip|redo), $2 = input pdf, $3 = output pdf
  local mode="$1" pdf="$2" out_pdf="$3" log
  log=$(mktemp)
  local skip_flag="--skip-text"
  [ "$mode" = "redo" ] && skip_flag="--redo-ocr"
  local extra=()
  [ -n "${USER_WORDS:-}" ] && extra+=(--user-words "$USER_WORDS")
  if ocrmypdf \
        -l "$LANGS" \
        $skip_flag \
        --rotate-pages \
        --deskew \
        --clean \
        --jobs "$PER_FILE_JOBS" \
        --quiet \
        "${extra[@]}" \
        "$pdf" "$out_pdf" 2>"$log"; then
    rm -f "$log"
    return 0
  else
    echo "[fel] $(basename "$pdf" .pdf) — se loggen nedan:" >&2
    sed 's/^/    /' "$log" >&2
    rm -f "$log"
    return 1
  fi
}

process_one() {
  local f="$1" base out_pdf out_txt existing has_text
  base=$(basename "$f" .pdf)
  out_pdf="$OCR/$base.pdf"
  out_txt="$TXT/$base.txt"

  existing=$(pdftotext -q -layout "$f" - 2>/dev/null | tr -d '[:space:]' | wc -c)
  has_text=0
  [ "$existing" -gt "$MIN_TEXT_CHARS" ] && has_text=1

  if [ "$REDO_TEXT_LAYER" = "1" ]; then
    # I detta läge: bara text-layer-filer, alltid kör om med --redo-ocr
    if [ "$has_text" != "1" ]; then
      echo "[hoppar-ej-text] $base"
      return 0
    fi
    echo "[redo] $base"
    rm -f "$out_pdf" "$out_txt"
    if run_ocr redo "$f" "$out_pdf"; then
      pdftotext -layout "$out_pdf" "$out_txt"
    else
      return 1
    fi
    return 0
  fi

  # Normalt läge
  [ -s "$out_txt" ] && { echo "[hoppar] $base"; return 0; }

  if [ "$has_text" = "1" ]; then
    echo "[text-finns] $base"
    cp "$f" "$out_pdf"
    pdftotext -layout "$f" "$out_txt"
    return 0
  fi

  echo "[ocr] $base"
  if run_ocr skip "$f" "$out_pdf"; then
    pdftotext -layout "$out_pdf" "$out_txt"
  else
    return 1
  fi
}
export -f process_one run_ocr
export IN OCR TXT PER_FILE_JOBS MIN_TEXT_CHARS REDO_TEXT_LAYER LANGS USER_WORDS TESSDATA_PREFIX

find "$IN" -name '*.pdf' -print0 \
  | xargs -0 -n 1 -P "$JOBS" -I {} bash -c 'process_one "$@"' _ {}

# Slutkontroll: alla PDF:er i files/ ska finnas i ocr/
echo
echo "Kontrollerar att alla PDF:er finns i $OCR/ …"
missing=0
while IFS= read -r -d '' f; do
  base=$(basename "$f" .pdf)
  if [ ! -s "$OCR/$base.pdf" ]; then
    echo "  SAKNAS: $base.pdf"
    missing=$((missing + 1))
  fi
done < <(find "$IN" -name '*.pdf' -print0)
if [ "$missing" -eq 0 ]; then
  echo "  OK — alla $(find "$IN" -name '*.pdf' | wc -l | tr -d ' ') PDF:er finns i $OCR/."
else
  echo "  $missing PDF:er saknas i $OCR/ — kör om skriptet."
  exit 1
fi
