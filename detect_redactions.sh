#!/bin/bash
# Detekterar maskeringsblock i alla PDF:er och infogar [MASKAD] i OCR-text.
# Renderar sidor vid låg DPI (72) — tillräckligt för stora svarta block.
# Idempotent: hoppar över filer med befintlig .redact-markör i generated/text/.
#
# Användning:
#   ./detect_redactions.sh               # alla filer
#   ./detect_redactions.sh --rebuild     # ignorera .redact-markörer

set -eu

usage() {
  cat <<EOF
Användning: $(basename "$0") [flaggor]

  --root DIR              projektrot (\$PWD om ej satt via ROOT)
  --in DIR                PDF-katalog (\$ROOT/downloaded/files)
  --txt DIR               text-katalog (\$ROOT/generated/text)
  --jobs N                parallella processer (4)
  --dpi N                 render-DPI (72)
  --rebuild               kör om alla filer (ignorera .redact-markörer)
  -h, --help              visa denna hjälp
EOF
}

ROOT=${ROOT:-$(cd "$(dirname "$0")" && pwd)}
IN=${IN:-}
TXT=${TXT:-}
JOBS=${JOBS:-4}
DPI=${DPI:-72}
REBUILD=0

while [ $# -gt 0 ]; do
  case "$1" in
    --root)     ROOT="$2"; shift 2 ;;
    --in)       IN="$2"; shift 2 ;;
    --txt)      TXT="$2"; shift 2 ;;
    --jobs)     JOBS="$2"; shift 2 ;;
    --dpi)      DPI="$2"; shift 2 ;;
    --rebuild)  REBUILD=1; shift ;;
    -h|--help)  usage; exit 0 ;;
    *) echo "okänd flagga: $1" >&2; usage >&2; exit 2 ;;
  esac
done

IN=${IN:-$ROOT/downloaded/files}
TXT=${TXT:-$ROOT/generated/text}

cd "$ROOT"
source .venv/bin/activate

PYBIN="$ROOT/.venv/bin/python"
WPU_DIR="$ROOT/downloaded/wpu_files"

if [ "$REBUILD" = "1" ]; then
  echo "Tar bort .redact-markörer..."
  find "$TXT" -name '*.redact' -delete
fi

TOTAL=$(find "$TXT" -name '*.txt' | wc -l | tr -d ' ')
PROGRESS_DIR=$(mktemp -d)
START_TS=$(date +%s)
export TOTAL PROGRESS_DIR START_TS TXT DPI PYBIN IN WPU_DIR ROOT

echo "Kontrollerar maskeringar i $TOTAL textfiler (DPI=$DPI, jobs=$JOBS)..."

find "$TXT" -name '*.txt' -print0 \
  | xargs -0 -n 1 -P "$JOBS" bash -c '
    stem=$(basename "$1" .txt)
    marker="${1%.txt}.redact"

    if [ -f "$marker" ]; then
      result="hoppar"
    else
      pdf="$IN/${stem}.pdf"
      if [ ! -f "$pdf" ] && [ -d "$WPU_DIR" ]; then
        pdf="$WPU_DIR/${stem}.pdf"
      fi
      if [ ! -f "$pdf" ]; then
        result="ingen pdf"
      else
        result=$("$PYBIN" "$ROOT/src/ocr_pages.py" \
          --engine detect-only \
          --in "$pdf" \
          --txt-dir "$TXT" \
          --ocr-dir "$ROOT/generated/ocr" \
          --dpi "$DPI" 2>/dev/null || echo "fel")
      fi
    fi

    touch "$PROGRESS_DIR/${stem}.done"
    count=$(find "$PROGRESS_DIR" -name "*.done" | wc -l | tr -d " ")
    now=$(date +%s)
    elapsed=$((now - START_TS))
    if [ "$elapsed" -gt 0 ] && [ "$count" -gt 0 ]; then
      remaining=$((TOTAL - count))
      eta_s=$(( remaining * elapsed / count ))
      printf "[%s %d/%d eta %dm%02ds] %s\n" "$result" "$count" "$TOTAL" "$((eta_s/60))" "$((eta_s%60))" "$stem"
    else
      printf "[%s %d/%d] %s\n" "$result" "$count" "$TOTAL" "$stem"
    fi
  ' _

rm -rf "$PROGRESS_DIR"
echo "Klart."
