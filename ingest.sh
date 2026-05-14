#!/bin/bash
# Indexera text/*.txt till LanceDB-vektor-DB:n.
#
# Användning:
#   ./ingest.sh                     # indexera nya filer
#   ./ingest.sh --rebuild           # börja om från noll
#   ./ingest.sh --limit 100         # bara N första filerna
#   ./ingest.sh -- --help           # full src/rag/ingest.py-hjälp

set -eu

usage() {
  cat <<EOF
Användning: $(basename "$0") [flaggor]

Wrapper-flaggor:
  --root DIR              projektrot ($PWD om ej satt via ROOT)
  -h, --help              visa denna hjälp

Skickas vidare till src/rag/ingest.py (alla okända flaggor passerar igenom):
  --rebuild               börja om från noll (släpper tabellen)
  --limit N               max antal filer att indexera
  --text-dir DIR          katalog med .txt-filer (default: generated/text/)
  --db-dir DIR            LanceDB-katalog (default: generated/lancedb/)
  --chunk-chars N         tecken per chunk (default: 800)
  --chunk-overlap N       överlapp mellan chunks (default: 150)
  --model NAME            embedding-modell (default: intfloat/multilingual-e5-large)
  --reindex-since TS      tvinga re-index av legacy-filer modifierade efter TS
                          (ISO 8601 t.ex. '2026-05-01' eller unix-sekunder)

För full src/rag/ingest.py-hjälp: ./ingest.sh -- --help
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

exec python src/rag/ingest.py ${extra[@]+"${extra[@]}"}
