#!/bin/bash
# Regelbaserad normalisering av OCR-textfiler.
# Rättar ligaturer, mjuka bindestreck, styrtecken och whitespace-artefakter.
#
# Användning:
#   ./normalize.sh                  # normalisera alla text/*.txt
#   ./normalize.sh --dry-run        # visa vad som skulle ändras
#   ./normalize.sh -- --help        # full src/normalize_text.py-hjälp

set -eu

usage() {
  cat <<EOF
Användning: $(basename "$0") [flaggor]

Wrapper-flaggor:
  --root DIR              projektrot (\$PWD om ej satt via ROOT)
  -h, --help              visa denna hjälp

Skickas vidare till src/normalize_text.py (alla okända flaggor passerar igenom):
  --txt DIR               text-katalog (default: <root>/generated/text)
  --dry-run               visa vad som skulle ändras utan att skriva
  --stats                 visa per-fil-statistik för ändrade filer
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

exec python src/normalize_text.py --root "$ROOT" ${extra[@]+"${extra[@]}"}
