#!/bin/bash
# LLM-baserad post-korrektion av dåliga OCR-sidor med Claude Haiku.
# Läser dåliga sidor från quality_pages-tabellen i state.db.
#
# Kräver CLAUDE_CODE_OAUTH_TOKEN eller ANTHROPIC_API_KEY i miljön.
# Kör ./quality.sh --per-page innan om quality_pages-tabellen är tom.
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
  --jobs N                antal parallella LLM-anrop (default: env JOBS eller 4)
  --provider PROVIDER     claude (default) eller openai
  --model MODEL           modellnamn (default: claude-haiku-4-5-20251001 / gpt-4o-mini)
  --base-url URL          override API-URL, t.ex. https://api.deepseek.com/v1
                          eller http://localhost:11434/v1 för Ollama
  --api-key KEY           override API-nyckel (annars läses från env)
  --txt DIR               text-katalog (default: <root>/generated/text)
  --dry-run               visa vad som skulle rättas utan att göra det

Exempel:
  ./llm_correct.sh --threshold 60
  ./llm_correct.sh --provider openai --model gpt-4o-mini
  ./llm_correct.sh --provider openai --base-url https://api.deepseek.com/v1 --model deepseek-chat
  ./llm_correct.sh --provider openai --base-url http://localhost:11434/v1 --model llama3.1:8b
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
