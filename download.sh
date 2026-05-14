#!/bin/bash
# Ladda ner alla PDF:er från palmemordsarkivet.se (Google Sheet).
#
# Användning:
#   ./download.sh                       # default-mapp: files/
#   ./download.sh --out files
#   ./download.sh --root DIR
#   ./download.sh -- --help             # full src/download.py-hjälp

set -eu

usage() {
  cat <<EOF
Användning: $(basename "$0") [flaggor]

Wrapper-flaggor:
  --root DIR              projektrot ($PWD om ej satt via ROOT)
  -h, --help              visa denna hjälp

Skickas vidare till src/download.py (alla okända flaggor passerar igenom):
  --out DIR               målmapp (default: downloaded/files/)
  --sheet-id ID           Google Sheets-ID (default: arkivets standardark)

För full src/download.py-hjälp: ./download.sh -- --help
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

exec python src/download.py ${extra[@]+"${extra[@]}"}
