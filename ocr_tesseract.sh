#!/bin/bash
# OCR + textextraktion för palmemordsarkivet.
# - Snabbkoll: hoppar över OCR om PDF:en redan har användbart textlager
# - Kvalitetscheck på existerande textlager — vid skräp triggas --redo-ocr
# - OCR med svenska + deskew/clean/rotate för scannade sidor
# - Parallelliserar över filer med xargs -P
# - Idempotent: hoppar över filer markerade som klara i state.db (tesseract_done_at)
#
# Krav: brew install ocrmypdf tesseract-lang poppler unpaper

set -u

trap 'trap - INT TERM; echo; echo "Avbruten — avslutar alla OCR-processer..."; kill 0 2>/dev/null; exit 130' INT TERM

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
  --files-from FILE       lista med filstammar att bearbeta (en per rad, .txt trimmas);
                          om utelämnad bearbetas alla PDF:er i --in
  --retry-failed          nollställ tesseract_failed-flaggor i state.db så att misslyckade filer körs om
  --retry-blacklist       nollställ tesseract_blacklisted_at-flaggor så att permanent uteslutna filer körs om
  -h, --help              visa denna hjälp och avsluta
EOF
}

ROOT=${ROOT:-$(cd "$(dirname "$0")" && pwd)}
PYBIN=${PYBIN:-}
DB_HELPER=${DB_HELPER:-}
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
RETRY_FAILED=${RETRY_FAILED:-0}
RETRY_BLACKLIST=${RETRY_BLACKLIST:-0}
FILES_FROM=${FILES_FROM:-}

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
    --files-from)       FILES_FROM="$2"; shift 2 ;;
    --retry-failed)     RETRY_FAILED=1; shift ;;
    --retry-blacklist)  RETRY_BLACKLIST=1; shift ;;
    -h|--help)          usage; exit 0 ;;
    *) echo "okänd flagga: $1" >&2; usage >&2; exit 2 ;;
  esac
done

PYBIN=${PYBIN:-$ROOT/.venv/bin/python}
DB_HELPER=${DB_HELPER:-$ROOT/src/ocr_db_helper.py}
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
  local f="$1" base out_pdf out_txt existing has_text _STATUS _OCR_RETRIES=""
  base=$(basename "$f" .pdf)
  out_pdf="$OCR/$base.pdf"
  out_txt="$TXT/$base.txt"

  # Idempotens via state.db — txt kan ha raderats av merge_wpu (som bara
  # raderar .txt och .pdf). Lazy migration: markera som klar om txt finns men
  # DB saknar post (filer OCR:ade innan DB-migreringen).
  _done=0
  if "$PYBIN" "$DB_HELPER" check-done "$base"; then
    _done=1
  elif [ -s "$out_txt" ]; then
    "$PYBIN" "$DB_HELPER" mark-done "$base" "$f" "$out_txt"
    _done=1
  fi
  if [ "$_done" = "1" ]; then
    printf 'hoppar' > "$PROGRESS_DIR/${base}.status"
    return 0
  fi

  # Hoppa över permanent blacklistade filer — bara --retry-blacklist tar in dem igen.
  if "$PYBIN" "$DB_HELPER" check-blacklisted "$base"; then
    printf 'blacklist-skip' > "$PROGRESS_DIR/${base}.status"
    return 0
  fi

  # Hoppa över filer som tidigare misslyckats — använd --retry-failed för att köra om.
  if "$PYBIN" "$DB_HELPER" check-failed "$base"; then
    printf 'fel-skip' > "$PROGRESS_DIR/${base}.status"
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
        "$PYBIN" "$DB_HELPER" mark-failed "$base" "$f"
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
      "$PYBIN" "$DB_HELPER" mark-failed "$base" "$f"
      printf 'fel' > "$PROGRESS_DIR/${base}.status"
      return 1
    fi
  fi

  printf '%s' "$_STATUS" > "$PROGRESS_DIR/${base}.status"
  # txt-sökvägen stämplar text_mtime — utan den ser normalize/quality-deltat
  # aldrig nya Tesseract-filer (text_mtime förblir NULL).
  "$PYBIN" "$DB_HELPER" mark-done "$base" "$f" "$out_txt"
}
export -f process_one run_ocr _run_ocrmypdf log_err
export IN OCR TXT PER_FILE_JOBS MIN_TEXT_CHARS LANGS PSM \
       USER_WORDS USER_WORDS_AUTO TESS_CONFIG TESSDATA_PREFIX IMAGE_DPI ERRORS_LOG \
       PYBIN DB_HELPER

if [ "$RETRY_FAILED" = "1" ]; then
  count=$("$PYBIN" "$DB_HELPER" clear-failed)
  if [ "${count:-0}" -gt 0 ]; then
    echo "Nollställde $count tesseract_failed-flaggor i state.db."
  else
    echo "Inga misslyckade OCR-jobb i state.db."
  fi
fi

if [ "$RETRY_BLACKLIST" = "1" ]; then
  count=$("$PYBIN" "$DB_HELPER" clear-blacklisted)
  if [ "${count:-0}" -gt 0 ]; then
    echo "Återaktiverade $count blacklistade OCR-jobb i state.db."
  else
    echo "Inga blacklistade OCR-jobb i state.db."
  fi
fi

# Bygg PDF-lista: antingen från --files-from eller find med förfiltrering.
PDFLIST=$(mktemp)
if [ -n "$FILES_FROM" ]; then
  [ -f "$FILES_FROM" ] || { echo "Saknar --files-from-fil: $FILES_FROM" >&2; exit 1; }
  while IFS= read -r line; do
    stem="${line%.txt}"
    [ -n "$stem" ] || continue
    pdf="$IN/$stem.pdf"
    [ -f "$pdf" ] && echo "$pdf" >> "$PDFLIST"
  done < "$FILES_FROM"
else
  # Förfiltrera via state.db — hoppa stems som redan markerats klara, misslyckade eller blacklistade.
  # En enda DB-query ersätter N filsystemskontroller.
  ALL_TOTAL=$(find "$IN" -name '*.pdf' | wc -l | tr -d ' ')
  _SKIP_FILE=$(mktemp)
  { "$PYBIN" "$DB_HELPER" list-done
    "$PYBIN" "$DB_HELPER" list-failed
    "$PYBIN" "$DB_HELPER" list-blacklisted
  } | sort -u > "$_SKIP_FILE"
  while IFS= read -r -d '' pdf; do
    base="${pdf##*/}"; base="${base%.pdf}"
    grep -qxF "$base" "$_SKIP_FILE" || echo "$pdf" >> "$PDFLIST"
  done < <(find "$IN" -name '*.pdf' -print0)
  rm -f "$_SKIP_FILE"
fi

TOTAL=$(wc -l < "$PDFLIST" | tr -d ' ')
PROGRESS_DIR=$(mktemp -d)
START_TS=$(date +%s)
export TOTAL PROGRESS_DIR START_TS
if [ -n "$FILES_FROM" ]; then
  echo "Bearbetar $TOTAL filer från $FILES_FROM..."
elif [ "$TOTAL" -lt "$ALL_TOTAL" ]; then
  echo "Hittade $TOTAL nya PDF:er ($((ALL_TOTAL - TOTAL)) redan klara)..."
else
  echo "Hittade $TOTAL PDF:er att bearbeta..."
fi

tr '\n' '\0' < "$PDFLIST" \
  | xargs -0 -n 1 -P "$JOBS" bash -c '
    process_one "$@"
    ret=$?
    base=$(basename "$1" .pdf)
    status=$(cat "$PROGRESS_DIR/${base}.status" 2>/dev/null || printf "?")
    touch "$PROGRESS_DIR/${base}.done"
    count=$(find "$PROGRESS_DIR" -name "*.done" | wc -l | tr -d " ")
    now=$(date +%s)
    elapsed=$((now - START_TS))
    if [ "$status" != "hoppar" ] && [ "$status" != "fel-skip" ]; then
      if [ "$elapsed" -gt 0 ] && [ "$count" -gt 0 ]; then
        remaining=$((TOTAL - count))
        eta_s=$(( remaining * elapsed / count ))
        printf "[%s %d/%d eta %dm%02ds] %s\n" "$status" "$count" "$TOTAL" "$((eta_s/60))" "$((eta_s%60))" "$base"
      else
        printf "[%s %d/%d] %s\n" "$status" "$count" "$TOTAL" "$base"
      fi
    fi
    exit $ret
  ' _

rm -f "$PDFLIST"
rm -rf "$PROGRESS_DIR"

# Slutkontroll körs bara vid full körning (inte --files-from).
if [ -z "$FILES_FROM" ]; then
  echo
  echo "Kontrollerar att alla PDF:er finns i $OCR/ …"
  missing=0
  known_failed=0
  known_blacklisted=0
  merged_away=0
  _DONE_FILE=$(mktemp)
  _FAILED_FILE=$(mktemp)
  _BLACKLIST_FILE=$(mktemp)
  "$PYBIN" "$DB_HELPER" list-done        | sort > "$_DONE_FILE"
  "$PYBIN" "$DB_HELPER" list-failed      | sort > "$_FAILED_FILE"
  "$PYBIN" "$DB_HELPER" list-blacklisted | sort > "$_BLACKLIST_FILE"
  while IFS= read -r -d '' f; do
    base=$(basename "$f" .pdf)
    if [ ! -s "$OCR/$base.pdf" ]; then
      if grep -qxF "$base" "$_BLACKLIST_FILE"; then
        known_blacklisted=$((known_blacklisted + 1))
      elif grep -qxF "$base" "$_FAILED_FILE"; then
        known_failed=$((known_failed + 1))
      elif grep -qxF "$base" "$_DONE_FILE"; then
        merged_away=$((merged_away + 1))
      else
        echo "  SAKNAS: $base.pdf"
        missing=$((missing + 1))
      fi
    fi
  done < <(find "$IN" -name '*.pdf' -print0)
  rm -f "$_DONE_FILE" "$_FAILED_FILE" "$_BLACKLIST_FILE"
  total=$(find "$IN" -name '*.pdf' | wc -l | tr -d ' ')
  if [ "$missing" -eq 0 ]; then
    msg="  OK — $total PDF:er bearbetade"
    [ "$merged_away" -gt 0 ]      && msg="$msg ($merged_away ersatta av palme-versionen)"
    [ "$known_failed" -gt 0 ]     && msg="$msg, $known_failed misslyckanden hoppades över"
    [ "$known_blacklisted" -gt 0 ] && msg="$msg, $known_blacklisted blacklistade hoppades över"
    echo "$msg."
  else
    echo "  $missing PDF:er saknas i $OCR/ — kör om skriptet."
    [ "$known_failed" -gt 0 ]      && echo "  $known_failed tidigare misslyckanden hoppades över."
    [ "$known_blacklisted" -gt 0 ] && echo "  $known_blacklisted blacklistade hoppades över."
    [ "$merged_away" -gt 0 ]       && echo "  $merged_away ersatta av palme-versionen (merge_wpu)."
    exit 1
  fi
fi
