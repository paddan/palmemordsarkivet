#!/bin/bash
# Ställ frågor mot palmemordsarkivet via RAG.
# Kör med rerank som default (bättre precision).
#
# Användning:
#   ./ask.sh "Vem var Stig Engström?"
#   ./ask.sh --no-rerank "snabbare men sämre"
#   ./ask.sh                                    # interaktiv repl

set -eu

ROOT=${ROOT:-$(cd "$(dirname "$0")" && pwd)}
cd "$ROOT"

# Aktivera venv
# shellcheck disable=SC1091
source .venv/bin/activate

# Källa token om den inte redan är satt och .zshrc.local finns
if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  if [ -f "$HOME/.zshrc.local" ]; then
    # shellcheck disable=SC1091
    source "$HOME/.zshrc.local"
  fi
fi

# Default: --rerank, men låt --no-rerank stänga av
rerank_flag="--rerank"
args=()
for a in "$@"; do
  case "$a" in
    --no-rerank) rerank_flag="" ;;
    *)           args+=("$a") ;;
  esac
done

exec python rag/ask.py $rerank_flag "${args[@]}"
