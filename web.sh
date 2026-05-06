#!/bin/bash
# Starta webgränssnittet (Streamlit) för palmemordsarkivet.
# Användning: ./web.sh

set -eu

ROOT=${ROOT:-$(cd "$(dirname "$0")" && pwd)}
cd "$ROOT"

# shellcheck disable=SC1091
source .venv/bin/activate

if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  if [ -f "$HOME/.zshrc.local" ]; then
    # shellcheck disable=SC1091
    source "$HOME/.zshrc.local"
  fi
fi

exec streamlit run webui.py "$@"
