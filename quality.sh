#!/bin/bash
# Bedöm OCR-kvalitet på text/*.txt och skriv quality.csv (samt ev. quality_pages.jsonl).
#
# Användning:
#   ./quality.sh                            # alla filer -> quality.csv
#   ./quality.sh --top 30                   # visa även värsta 30 i terminalen
#   ./quality.sh --per-page                 # skriv även quality_pages.jsonl
#   ./quality.sh --root DIR -- --limit 50   # alla quality.py-flaggor efter --
#
# Alla okända flaggor skickas vidare till quality.py.

set -eu

usage() {
  cat <<EOF
Användning: $(basename "$0") [flaggor] [-- quality.py-flaggor...]

  --root DIR    projektrot ($PWD om ej satt via ROOT)
  -h, --help    visa denna hjälp; för quality.py-flaggor: ./quality.sh -- --help

Övriga flaggor (t.ex. --top, --per-page, --limit, --out) skickas vidare till quality.py.
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

exec python quality.py "${extra[@]}"
