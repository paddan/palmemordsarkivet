#!/bin/bash
# LLM-baserad post-korrektion av dåliga OCR-sidor med Claude Haiku.
# Läser quality_pages.jsonl och rättar sidor under score-tröskeln.
#
# Kräver CLAUDE_CODE_OAUTH_TOKEN eller ANTHROPIC_API_KEY i miljön.
# Kör ./quality.sh --per-page innan om quality_pages.jsonl saknas.
#
# Användning:
#   ./llm_correct.sh                    # rätta sidor med score < 50
#   ./llm_correct.sh --threshold 60     # striktare tröskel
#   ./llm_correct.sh --dry-run          # visa vad som skulle rättas
#   ./llm_correct.sh -- --help          # full src/llm_correct.py-hjälp

set -eu

usage() {
  cat <<EOF
Användning: $(basename "$0") [flaggor]

Wrapper-flaggor:
  --root DIR              projektrot (\$PWD om ej satt via ROOT)
  -h, --help              visa denna hjälp

Skickas vidare till src/llm_correct.py (alla okända flaggor passerar igenom):
  --threshold N           score-tröskel (default: 50)
  --model MODEL           Claude-modell (default: claude-haiku-4-5-20251001)
  --pages-jsonl FILE      quality_pages.jsonl (default: <root>/quality_pages.jsonl)
  --txt DIR               text-katalog (default: <root>/text)
  --pages-out DIR         text_pages-katalog (default: <root>/text_pages)
  --dry-run               visa vad som skulle rättas utan att göra det
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

: "${CLAUDE_CODE_OAUTH_TOKEN:=${ANTHROPIC_API_KEY:-}}"
export CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_API_KEY

exec python src/llm_correct.py --root "$ROOT" ${extra[@]+"${extra[@]}"}
