#!/bin/bash
# Starta webgränssnittet (Streamlit) för palmemordsarkivet.
#
# Användning:
#   ./web.sh                       # standardstart
#   ./web.sh --root DIR            # annan projektrot
#   ./web.sh -- --server.port 8502 # alla extra streamlit-flaggor efter --

set -eu

usage() {
  cat <<EOF
Användning: $(basename "$0") [flaggor] [-- streamlit-flaggor...]

  --root DIR    projektrot ($PWD om ej satt via ROOT)
  -h, --help    visa denna hjälp

Allt efter '--' skickas vidare till 'streamlit run webui.py'.
EOF
}

ROOT=${ROOT:-$(cd "$(dirname "$0")" && pwd)}
extra=()

while [ $# -gt 0 ]; do
  case "$1" in
    --root)    ROOT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    --)        shift; extra=("$@"); break ;;
    *)         extra+=("$1"); shift ;;
  esac
done

cd "$ROOT"

# shellcheck disable=SC1091
source .venv/bin/activate

if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  if [ -f "$HOME/.zshrc.local" ]; then
    # shellcheck disable=SC1091
    source "$HOME/.zshrc.local"
  fi
fi

exec streamlit run webui.py "${extra[@]}"
