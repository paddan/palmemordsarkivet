#!/bin/bash
# Kör hela pipeline:n: download → OCR → (LLM-korrigering) → ingest.
#
# Steg:
#   1. download.sh               — hämta PDF:er från palmemordsarkivet.se
#   2. download_wpu.sh           — hämta PDF:er från wpu.nu (hoppa med --skip-wpu)
#   3. ocr.sh                    — Tesseract → detect_redactions → normalize →
#                                  quality → ev. Surya → quality
#   4. llm_correct.sh            — LLM-korrigering av dåliga sidor (kräver --with-llm)
#   5. ingest.sh                 — indexera till LanceDB
#
# Användning:
#   ./run_pipeline.sh
#   ./run_pipeline.sh --skip-wpu --skip-redo
#   ./run_pipeline.sh --rebuild-index

set -eu

trap 'trap - INT TERM; echo; echo "Avbruten."; kill 0 2>/dev/null; exit 130' INT TERM

usage() {
  cat <<EOF
Användning: $(basename "$0") [flaggor]

  --skip-wpu          hoppa wpu.nu-nedladdning (steg 2)
  --skip-redo         hoppa Surya-steget i OCR (snabbare, skickas till ocr.sh)
  --with-llm          kör llm_correct.sh efter OCR (betald, kräver API-nyckel)
  --rebuild-index     bygg LanceDB-index från noll (skickas till ingest.sh)
  --jobs N            parallella processer i OCR (standard: 4)
  -h, --help          visa denna hjälp
EOF
}

ROOT=${ROOT:-$(cd "$(dirname "$0")" && pwd)}
SKIP_WPU=0
SKIP_REDO=0
WITH_LLM=0
REBUILD_INDEX=0
JOBS=${JOBS:-4}

while [ $# -gt 0 ]; do
  case "$1" in
    --skip-wpu)      SKIP_WPU=1; shift ;;
    --skip-redo)     SKIP_REDO=1; shift ;;
    --with-llm)      WITH_LLM=1; shift ;;
    --rebuild-index) REBUILD_INDEX=1; shift ;;
    --jobs)          JOBS="$2"; shift 2 ;;
    -h|--help)       usage; exit 0 ;;
    *) echo "okänd flagga: $1" >&2; usage >&2; exit 2 ;;
  esac
done

cd "$ROOT"

step() {
  echo
  echo "━━━━━ $1 ━━━━━"
}

t0=$(date +%s)

step "1/5  Ladda ner PDF:er (./download.sh)"
./download.sh || { rc=$?; [ "$rc" -eq 2 ] || exit "$rc"; }

if [ "$SKIP_WPU" = "0" ]; then
  step "2/5  Ladda ner wpu.nu-PDF:er (./download_wpu.sh)"
  ./download_wpu.sh || { rc=$?; [ "$rc" -eq 2 ] || exit "$rc"; }
else
  echo
  echo "━━━━━ Hoppar steg 2 (--skip-wpu) ━━━━━"
fi

step "3/5  OCR + normalisering + kvalitetsbedömning (./ocr.sh)"
ocr_flags=()
[ "$SKIP_REDO" = "1" ] && ocr_flags+=(--skip-redo)
[ "$JOBS" != "4" ] && ocr_flags+=(--jobs "$JOBS")
./ocr.sh ${ocr_flags[@]+"${ocr_flags[@]}"}

if [ "$WITH_LLM" = "1" ]; then
  step "4/5  LLM-korrigering av låga sidor (./llm_correct.sh)"
  ./llm_correct.sh
else
  echo
  echo "━━━━━ Hoppar steg 4 (llm_correct — lägg till --with-llm för att aktivera) ━━━━━"
fi

step "5/5  Indexera till LanceDB (./ingest.sh)"
ingest_flags=()
[ "$REBUILD_INDEX" = "1" ] && ingest_flags+=(--rebuild)
./ingest.sh ${ingest_flags[@]+"${ingest_flags[@]}"}

t1=$(date +%s)
elapsed=$((t1 - t0))
m=$((elapsed / 60))
s=$((elapsed % 60))
echo
echo "━━━━━ Pipeline klar på ${m}m${s}s ━━━━━"
