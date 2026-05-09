#!/bin/bash
# Ställ frågor mot palmemordsarkivet via RAG.
# Kör med rerank som default (bättre precision).
#
# Användning:
#   ./ask.sh "Vem var Stig Engström?"
#   ./ask.sh --no-rerank "snabbare men sämre"
#   ./ask.sh --help                              # visar denna och src/rag/ask.py:s --help
#   ./ask.sh                                     # interaktiv repl
#
# Alla okända flaggor skickas vidare till src/rag/ask.py.

set -eu

usage() {
  cat <<EOF
Användning: $(basename "$0") [flaggor] [fråga ...]

  --root DIR     projektrot ($PWD om ej satt via ROOT)
  --no-rerank    stäng av cross-encoder-omrankning (på som default)
  --mcp          utredningsläge: Claude söker autonomt (långsammare, bättre på komplexa frågor)
  -h, --help     visa denna hjälp; för src/rag/ask.py-flaggor: ./ask.sh -- --help

Övriga flaggor (t.ex. --top-k, --top-n, --hybrid) skickas vidare till src/rag/ask.py.
EOF
}

ROOT=${ROOT:-$(cd "$(dirname "$0")" && pwd)}

# Default: --rerank, men låt --no-rerank stänga av
rerank_flag="--rerank"
args=()
show_help=0
for a in "$@"; do
  case "$a" in
    --no-rerank) rerank_flag="" ;;
    -h|--help)   show_help=1 ;;
    --root)      : ;;  # hanteras nedan
    *)           args+=("$a") ;;
  esac
done

# Hantera --root separat (kräver värde)
new_args=()
i=0
orig=("$@")
while [ $i -lt ${#orig[@]} ]; do
  case "${orig[$i]}" in
    --root) ROOT="${orig[$((i+1))]}"; i=$((i+2)) ;;
    *) new_args+=("${orig[$i]}"); i=$((i+1)) ;;
  esac
done

if [ "$show_help" = "1" ]; then
  usage
  exit 0
fi

cd "$ROOT"

# shellcheck disable=SC1091
source .venv/bin/activate

# Källa token om den inte redan är satt och .zshrc.local finns
if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  if [ -f "$HOME/.zshrc.local" ]; then
    # shellcheck disable=SC1091
    source "$HOME/.zshrc.local"
  fi
fi

exec python src/rag/ask.py $rerank_flag ${args[@]+"${args[@]}"}
