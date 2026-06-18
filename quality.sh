#!/bin/bash
# Bedöm OCR-kvalitet på text/*.txt och skriv resultat till state.db.
#
# Användning:
#   ./quality.sh                            # alla filer -> quality-tabellen
#   ./quality.sh --top 30                   # visa även värsta 30 i terminalen
#   ./quality.sh --per-page                 # skriv även quality_pages-tabellen
#   ./quality.sh --root DIR -- --limit 50   # alla quality.py-flaggor efter --
#
# Alla okända flaggor skickas vidare till quality.py.

set -eu

usage() {
  cat <<EOF
Användning: $(basename "$0") [flaggor]

Wrapper-flaggor:
  --root DIR              projektrot ($PWD om ej satt via ROOT)
  -h, --help              visa denna hjälp

Skickas vidare till quality.py (alla okända flaggor passerar igenom):
  --top N                 visa även värsta N i terminalen
  --limit N               bara N första filerna (för testkörning)
  --per-page              skriv även per-sida-poäng till state.db
  --text-dir DIR          katalog med .txt-filer att bedöma (default: generated/text/)
  --files-dir DIR         katalog med original-PDF:er (default: downloaded/files/)
  --rebuild               ignorera state.db-delta och kör om alla filer

För full quality.py-hjälp: ./quality.sh -- --help
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

exec python src/quality.py ${extra[@]+"${extra[@]}"}
