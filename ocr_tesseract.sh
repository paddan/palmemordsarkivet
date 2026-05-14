#!/bin/bash
# OCR + textextraktion för palmemordsarkivet.
# - Snabbkoll: hoppar över OCR om PDF:en redan har användbart textlager
# - Kvalitetscheck på existerande textlager — vid skräp triggas --redo-ocr
# - OCR med svenska + deskew/clean/rotate för scannade sidor
# - Parallelliserar över filer med xargs -P
# - Idempotent: hoppar över filer där .ocr-done-markör finns i generated/ocr/
#   (.txt ensam räcker inte — merge_wpu kan radera den)
#
# Krav: brew install ocrmypdf tesseract-lang poppler unpaper

set -u

usage() {
  cat <<EOF
Användning: $(basename "$0") [flaggor]

Default-värden visas inom parentes. Alla flaggor kan också sättas via
motsvarande env-var (versaler, understreck) — flagga vinner över env-var.

  --root DIR              projektrot ($PWD om ej satt via ROOT)
  --in DIR                ingångskatalog med PDF:er (\$ROOT/downloaded/files)
  --ocr DIR               output-katalog för OCR:ade PDF:er (\$ROOT/generated/ocr)
  --txt DIR               output-katalog för .txt (\$ROOT/generated/text)
  --tessdata DIR          tessdata-katalog (\$ROOT/tessdata)
  --user-words FILE       sökväg till swe.user-words (\$TESSDATA/swe.user-words)
  --user-words-auto FILE  sökväg till swe.user-words.auto
  --tess-config FILE      tesseract.config (\$TESSDATA/tesseract.config)
  --psm N                 tesseract page segmentation mode (6)
  --langs S               tesseract-språk (swe)
  --jobs N                antal filer parallellt (4)
  --per-file-jobs N       OCR-trådar per fil (2)
  --min-text-chars N      tröskel för "har redan text" (200)
  --image-dpi N           bild-DPI för OCR (300)
  --errors-log FILE       logg-fil för fel (\$ROOT/generated/errors.log)
  -h, --help              visa denna hjälp och avsluta
EOF
}

ROOT=${ROOT:-$(cd "$(dirname "$0")" && pwd)}
IN=${IN:-}
OCR=${OCR:-}
TXT=${TXT:-}
TESSDATA=${TESSDATA:-}
USER_WORDS=${USER_WORDS:-}
USER_WORDS_AUTO=${USER_WORDS_AUTO:-}
TESS_CONFIG=${TESS_CONFIG:-}
PSM=${PSM:-6}
LANGS=${LANGS:-swe}
JOBS=${JOBS:-4}
PER_FILE_JOBS=${PER_FILE_JOBS:-2}
MIN_TEXT_CHARS=${MIN_TEXT_CHARS:-200}
IMAGE_DPI=${IMAGE_DPI:-300}
ERRORS_LOG=${ERRORS_LOG:-}

while [ $# -gt 0 ]; do
  case "$1" in
    --root)             ROOT="$2"; shift 2 ;;
    --in)               IN="$2"; shift 2 ;;
    --ocr)              OCR="$2"; shift 2 ;;
    --txt)              TXT="$2"; shift 2 ;;
    --tessdata)         TESSDATA="$2"; shift 2 ;;
    --user-words)       USER_WORDS="$2"; shift 2 ;;
    --user-words-auto)  USER_WORDS_AUTO="$2"; shift 2 ;;
    --tess-config)      TESS_CONFIG="$2"; shift 2 ;;
    --psm)              PSM="$2"; shift 2 ;;
    --langs)            LANGS="$2"; shift 2 ;;
    --jobs)             JOBS="$2"; shift 2 ;;
    --per-file-jobs)    PER_FILE_JOBS="$2"; shift 2 ;;
    --min-text-chars)   MIN_TEXT_CHARS="$2"; shift 2 ;;
    --image-dpi)        IMAGE_DPI="$2"; shift 2 ;;
    --errors-log)       ERRORS_LOG="$2"; shift 2 ;;
    -h|--help)          usage; exit 0 ;;
    *) echo "okänd flagga: $1" >&2; usage >&2; exit 2 ;;
  esac
done

IN=${IN:-$ROOT/downloaded/files}
OCR=${OCR:-$ROOT/generated/ocr}
TXT=${TXT:-$ROOT/generated/text}
TESSDATA=${TESSDATA:-$ROOT/tessdata}
USER_WORDS=${USER_WORDS:-$TESSDATA/swe.user-words}
USER_WORDS_AUTO=${USER_WORDS_AUTO:-$TESSDATA/swe.user-words.auto}
TESS_CONFIG=${TESS_CONFIG:-$TESSDATA/tesseract.config}
ERRORS_LOG=${ERRORS_LOG:-$ROOT/generated/errors.log}

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
    echo "${ts}	ocr_tesseract.sh	${base}	ocrmypdf failed"
    head -n 10 "$log" 2>/dev/null | sed 's/^/    /'
  } >> "$ERRORS_LOG"
}

detect_lang() {
  # Läser text på stdin, skriver "eng", "swe" eller "swe+eng" på stdout.
  # "swe+eng" returneras bara om stdin är tom (okänt — använd båda).
  python3 -c '
import sys
text = sys.stdin.buffer.read().decode("utf-8", errors="replace").strip()
if not text:
    print("swe+eng")
    sys.exit(0)
try:
    from langdetect import detect_langs
    langs = {l.lang: l.prob for l in detect_langs(text[:8000])}
    if langs.get("en", 0) > 0.80:
        print("eng")
    else:
        print("swe")
except Exception:
    print("swe")
'
}
export -f detect_lang

text_quality_ok() {
  # Läser text på stdin, returnerar 0 om kvaliteten godkänns. Kriterier:
  #   alnum-andel >= 0.55
  #   andel 1-2-tecken-ord <= 0.30
  #   andel siffror-i-ord <= 0.10
  # Implementerad i python för att tåla ogiltig UTF-8 (U+FFFD från pdftotext).
  python3 -c '
import re, sys
t = sys.stdin.buffer.read().decode("utf-8", errors="replace")
chars = len(t)
if not chars:
    sys.exit(1)
alnum = sum(1 for c in t if c.isalnum())
tokens = t.split()
if not tokens:
    sys.exit(1)
short = sum(1 for w in tokens if len(w) <= 2)
digit_in = sum(1 for w in tokens if re.search(r"\d", w) and re.search(r"[^\W\d_]", w))
ar = alnum / chars
sr = short / len(tokens)
dr = digit_in / len(tokens)
sys.exit(0 if (ar >= 0.55 and sr <= 0.30 and dr <= 0.10) else 1)
'
}
export -f text_quality_ok

_run_ocrmypdf() {
  # Hjälpfunktion: kör ocrmypdf med givna flaggor, returnerar 0/1.
  # $1 = log-fil, resten = flaggor + in/ut
  local log="$1"; shift
  ocrmypdf -l "$LANGS" --jobs "$PER_FILE_JOBS" --optimize 0 --quiet "$@" 2>"$log"
}

run_ocr() {
  # $1 = mode (skip|redo), $2 = input pdf, $3 = output pdf
  # Försök 1: fullt förbehandlingsläge (rotate+clean+deskew).
  # Försök 2: utan --clean om försök 1 misslyckas (unpaper kraschar på konstiga sidor).
  # Försök 3: utan --clean och --deskew.
  # Appendar retry-labels till _OCR_RETRIES (deklareras av anroparen).
  local mode="$1" pdf="$2" out_pdf="$3" log base
  base=$(basename "$pdf" .pdf)
  log=$(mktemp)
  trap 'rm -f "${log:-}"' EXIT INT TERM

  local base_flags=(--rotate-pages --tesseract-pagesegmode "$PSM" --image-dpi "$IMAGE_DPI")
  [ -n "${USER_WORDS:-}" ]      && base_flags+=(--user-words "$USER_WORDS")
  [ -n "${USER_WORDS_AUTO:-}" ] && base_flags+=(--user-words "$USER_WORDS_AUTO")
  [ -n "${TESS_CONFIG:-}" ]     && base_flags+=(--tesseract-config "$TESS_CONFIG")

  local mode_flags=()
  if [ "$mode" = "redo" ]; then
    mode_flags=(--redo-ocr)
  else
    mode_flags=(--skip-text --deskew)
  fi

  # Försök 1: med --clean
  if _run_ocrmypdf "$log" "${base_flags[@]}" --clean "${mode_flags[@]}" "$pdf" "$out_pdf"; then
    rm -f "$log"; return 0
  fi

  # Försök 2: utan --clean (unpaper-fel är vanligaste orsaken)
  _OCR_RETRIES="${_OCR_RETRIES} no-clean"
  truncate -s 0 "$log"
  if _run_ocrmypdf "$log" "${base_flags[@]}" "${mode_flags[@]}" "$pdf" "$out_pdf"; then
    rm -f "$log"; return 0
  fi

  # Försök 3: utan --clean och utan --deskew
  if [ "$mode" != "redo" ]; then
    _OCR_RETRIES="${_OCR_RETRIES} no-deskew"
    truncate -s 0 "$log"
    if _run_ocrmypdf "$log" "${base_flags[@]}" --skip-text "$pdf" "$out_pdf"; then
      rm -f "$log"; return 0
    fi
  fi

  echo "[fel] $base — se loggen nedan:" >&2
  sed 's/^/    /' "$log" >&2
  log_err "$base" "$log"
  rm -f "$log"
  return 1
}

process_one() {
  local f="$1" base out_pdf out_txt existing has_text _STATUS _OCR_RETRIES="" marker
  base=$(basename "$f" .pdf)
  out_pdf="$OCR/$base.pdf"
  out_txt="$TXT/$base.txt"
  marker="$OCR/$base.ocr-done"

  # Idempotens via markörfil — txt kan ha raderats av merge_wpu (som bara
  # raderar .txt och .pdf, inte markören). Lazy migration: skapa markör om
  # txt finns sedan innan markören introducerades.
  if [ -s "$out_txt" ] && [ ! -f "$marker" ]; then
    touch "$marker"
  fi
  if [ -f "$marker" ]; then
    printf 'hoppar' > "$PROGRESS_DIR/${base}.status"
    return 0
  fi

  local raw_text
  # Utan -layout: roterade PDF:er (Page rot: 90/270) ger annars enormt whitespace
  # som sänker alnum-ration och ger falska "text-skräp"-klassningar.
  raw_text=$(pdftotext -q "$f" - 2>/dev/null || true)
  existing=$(printf '%s' "$raw_text" | tr -d '[:space:]' | wc -c | tr -d ' ')
  has_text=0
  [ "$existing" -gt "$MIN_TEXT_CHARS" ] && has_text=1

  # Detektera språk och sätt LANGS för denna fil (subshell — påverkar inte parallella jobb)
  if [ "$has_text" = "1" ]; then
    LANGS=$(printf '%s' "$raw_text" | detect_lang)
  else
    LANGS="swe+eng"  # skannat dokument — okänt språk, kör båda
  fi

  _STATUS=""
  [ "$LANGS" != "swe" ] && _STATUS="lang:$LANGS "

  if [ "$has_text" = "1" ]; then
    if printf '%s' "$raw_text" | text_quality_ok; then
      _STATUS="${_STATUS}text-finns"
      cp "$f" "$out_pdf"
      pdftotext -layout "$f" "$out_txt"
    else
      _STATUS="${_STATUS}text-skräp→redo"
      rm -f "$out_pdf" "$out_txt"
      if run_ocr redo "$f" "$out_pdf"; then
        _STATUS="${_STATUS}${_OCR_RETRIES}"
        pdftotext -layout "$out_pdf" "$out_txt"
      else
        printf 'fel' > "$PROGRESS_DIR/${base}.status"
        return 1
      fi
    fi
  else
    _STATUS="${_STATUS}ocr"
    if run_ocr skip "$f" "$out_pdf"; then
      _STATUS="${_STATUS}${_OCR_RETRIES}"
      pdftotext -layout "$out_pdf" "$out_txt"
    else
      printf 'fel' > "$PROGRESS_DIR/${base}.status"
      return 1
    fi
  fi

  printf '%s' "$_STATUS" > "$PROGRESS_DIR/${base}.status"
  touch "$marker"
}
export -f process_one run_ocr _run_ocrmypdf log_err
export IN OCR TXT PER_FILE_JOBS MIN_TEXT_CHARS LANGS PSM \
       USER_WORDS USER_WORDS_AUTO TESS_CONFIG TESSDATA_PREFIX IMAGE_DPI ERRORS_LOG

TOTAL=$(find "$IN" -name '*.pdf' | wc -l | tr -d ' ')
PROGRESS_DIR=$(mktemp -d)
START_TS=$(date +%s)
export TOTAL PROGRESS_DIR START_TS
echo "Hittade $TOTAL PDF:er att bearbeta..."

find "$IN" -name '*.pdf' -print0 \
  | xargs -0 -n 1 -P "$JOBS" bash -c '
    process_one "$@"
    ret=$?
    base=$(basename "$1" .pdf)
    status=$(cat "$PROGRESS_DIR/${base}.status" 2>/dev/null || printf "?")
    touch "$PROGRESS_DIR/${base}.done"
    count=$(find "$PROGRESS_DIR" -name "*.done" | wc -l | tr -d " ")
    now=$(date +%s)
    elapsed=$((now - START_TS))
    if [ "$elapsed" -gt 0 ] && [ "$count" -gt 0 ]; then
      remaining=$((TOTAL - count))
      eta_s=$(( remaining * elapsed / count ))
      printf "[%s %d/%d eta %dm%02ds] %s\n" "$status" "$count" "$TOTAL" "$((eta_s/60))" "$((eta_s%60))" "$base"
    else
      printf "[%s %d/%d] %s\n" "$status" "$count" "$TOTAL" "$base"
    fi
    exit $ret
  ' _

rm -rf "$PROGRESS_DIR"

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
