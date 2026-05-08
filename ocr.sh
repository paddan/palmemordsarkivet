#!/bin/bash
# OCR + textextraktion för palmemordsarkivet.
# - Snabbkoll: hoppar över OCR om PDF:en redan har användbart textlager
# - Kvalitetscheck på existerande textlager — vid skräp triggas --redo-ocr
# - OCR med svenska + deskew/clean/rotate för scannade sidor
# - Parallelliserar över filer med xargs -P
# - Idempotent: hoppar över filer där .txt redan finns
#
# Lägen:
#   ./ocr.sh                       # normal: OCR:a sidor utan textlager
#   REDO_TEXT_LAYER=1 ./ocr.sh     # kör om OCR på alla PDF:er som har textlager
#
# Krav: brew install ocrmypdf tesseract-lang poppler unpaper

set -u

ROOT=${ROOT:-$(cd "$(dirname "$0")" && pwd)}
IN=${IN:-$ROOT/files}
OCR=${OCR:-$ROOT/ocr}
TXT=${TXT:-$ROOT/text}
TESSDATA=${TESSDATA:-$ROOT/tessdata}
USER_WORDS=${USER_WORDS:-$TESSDATA/swe.user-words}
USER_WORDS_AUTO=${USER_WORDS_AUTO:-$TESSDATA/swe.user-words.auto}
TESS_CONFIG=${TESS_CONFIG:-$TESSDATA/tesseract.config}
PSM=${PSM:-6}                      # 6 = uniform block of text (bra för förhör)
LANGS=${LANGS:-swe}                # default svenska; sätt swe+eng för engelska sidor
JOBS=${JOBS:-4}                    # antal filer parallellt
PER_FILE_JOBS=${PER_FILE_JOBS:-2}  # OCR-trådar per fil
MIN_TEXT_CHARS=${MIN_TEXT_CHARS:-200}  # tröskel för "har redan text"
REDO_TEXT_LAYER=${REDO_TEXT_LAYER:-0}
IMAGE_DPI=${IMAGE_DPI:-300}
ERRORS_LOG=${ERRORS_LOG:-$ROOT/errors.log}

mkdir -p "$OCR" "$TXT"

# Använd projekt-lokal tessdata om den finns (swe_best + symlänkar till eng/osd)
if [ -f "$TESSDATA/swe.traineddata" ]; then
  export TESSDATA_PREFIX="$TESSDATA"
  echo "Använder $TESSDATA (swe_best)"
else
  echo "Tips: kör ./setup_tessdata.sh för att hämta swe_best.traineddata (mer noggrann modell)."
fi

[ -f "$USER_WORDS" ] || USER_WORDS=""
[ -f "$USER_WORDS_AUTO" ] || USER_WORDS_AUTO=""
[ -f "$TESS_CONFIG" ] || TESS_CONFIG=""
[ -n "$USER_WORDS" ] && echo "Använder user-words: $USER_WORDS ($(wc -l < "$USER_WORDS" | tr -d ' ') ord)"
[ -n "$USER_WORDS_AUTO" ] && echo "Använder user-words.auto: $USER_WORDS_AUTO ($(wc -l < "$USER_WORDS_AUTO" | tr -d ' ') ord)"
[ -n "$TESS_CONFIG" ] && echo "Använder tesseract-config: $TESS_CONFIG"
echo "PSM: $PSM, LANGS: $LANGS, IMAGE_DPI: $IMAGE_DPI"

for cmd in ocrmypdf pdftotext; do
  command -v "$cmd" >/dev/null || { echo "saknar verktyg: $cmd"; exit 1; }
done

log_err() {
  # $1 = filnamn, $2 = loggfil med stderr
  local base="$1" log="$2" ts
  ts=$(date -Iseconds 2>/dev/null || date +"%Y-%m-%dT%H:%M:%S%z")
  {
    echo "${ts}	ocr.sh	${base}	ocrmypdf failed"
    head -n 10 "$log" 2>/dev/null | sed 's/^/    /'
  } >> "$ERRORS_LOG"
}

text_quality_ok() {
  # Läser text på stdin, returnerar 0 om kvaliteten godkänns. Kriterier:
  #   alnum-andel >= 0.55
  #   andel 1-2-tecken-ord <= 0.30
  #   andel siffror-i-ord <= 0.10
  awk '
    BEGIN { chars=0; alnum=0; words=0; short=0; digit_in=0 }
    {
      n = length($0)
      for (i=1; i<=n; i++) {
        c = substr($0, i, 1)
        chars++
        if (c ~ /[A-Za-z0-9ÅÄÖåäö]/) alnum++
      }
      for (i=1; i<=NF; i++) {
        w = $i
        words++
        wl = length(w)
        if (wl <= 2) short++
        if (w ~ /[0-9]/ && w ~ /[A-Za-zÅÄÖåäö]/) digit_in++
      }
    }
    END {
      if (chars == 0 || words == 0) { exit 1 }
      ar = alnum / chars
      sr = short / words
      dr = digit_in / words
      if (ar >= 0.55 && sr <= 0.30 && dr <= 0.10) exit 0
      exit 1
    }
  '
}
export -f text_quality_ok

run_ocr() {
  # $1 = mode (skip|redo), $2 = input pdf, $3 = output pdf
  local mode="$1" pdf="$2" out_pdf="$3" log base
  base=$(basename "$pdf" .pdf)
  log=$(mktemp)
  local extra=(--rotate-pages --clean --tesseract-pagesegmode "$PSM" --image-dpi "$IMAGE_DPI")
  if [ "$mode" = "redo" ]; then
    # --redo-ocr är inkompatibel med --deskew (och --clean-final, --remove-background)
    extra+=(--redo-ocr)
  else
    extra+=(--skip-text --deskew)
  fi
  [ -n "${USER_WORDS:-}" ] && extra+=(--user-words "$USER_WORDS")
  [ -n "${USER_WORDS_AUTO:-}" ] && extra+=(--user-words "$USER_WORDS_AUTO")
  [ -n "${TESS_CONFIG:-}" ] && extra+=(--tesseract-config "$TESS_CONFIG")
  if ocrmypdf \
        -l "$LANGS" \
        --jobs "$PER_FILE_JOBS" \
        --quiet \
        "${extra[@]}" \
        "$pdf" "$out_pdf" 2>"$log"; then
    rm -f "$log"
    return 0
  else
    echo "[fel] $base — se loggen nedan:" >&2
    sed 's/^/    /' "$log" >&2
    log_err "$base" "$log"
    rm -f "$log"
    return 1
  fi
}

process_one() {
  local f="$1" base out_pdf out_txt existing has_text
  base=$(basename "$f" .pdf)
  out_pdf="$OCR/$base.pdf"
  out_txt="$TXT/$base.txt"

  local raw_text
  raw_text=$(pdftotext -q -layout "$f" - 2>/dev/null || true)
  existing=$(printf '%s' "$raw_text" | tr -d '[:space:]' | wc -c | tr -d ' ')
  has_text=0
  [ "$existing" -gt "$MIN_TEXT_CHARS" ] && has_text=1

  if [ "$REDO_TEXT_LAYER" = "1" ]; then
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
    if printf '%s' "$raw_text" | text_quality_ok; then
      echo "[text-finns] $base"
      cp "$f" "$out_pdf"
      pdftotext -layout "$f" "$out_txt"
      return 0
    else
      echo "[text-skräp -> redo] $base"
      rm -f "$out_pdf" "$out_txt"
      if run_ocr redo "$f" "$out_pdf"; then
        pdftotext -layout "$out_pdf" "$out_txt"
      else
        return 1
      fi
      return 0
    fi
  fi

  echo "[ocr] $base"
  if run_ocr skip "$f" "$out_pdf"; then
    pdftotext -layout "$out_pdf" "$out_txt"
  else
    return 1
  fi
}
export -f process_one run_ocr log_err
export IN OCR TXT PER_FILE_JOBS MIN_TEXT_CHARS REDO_TEXT_LAYER LANGS PSM \
       USER_WORDS USER_WORDS_AUTO TESS_CONFIG TESSDATA_PREFIX IMAGE_DPI ERRORS_LOG

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
