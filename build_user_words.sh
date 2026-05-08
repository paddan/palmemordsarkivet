#!/bin/bash
# Bygg utvidgad swe.user-words.auto från korpus i text/.
#
# Användning:
#   ./build_user_words.sh                            # bygg tessdata/swe.user-words.auto
#   ./build_user_words.sh --root DIR                 # annan projektrot
#   ./build_user_words.sh -- --min-freq 20           # alla build_user_words.py-flaggor efter --
#
# Alla okända flaggor skickas vidare till build_user_words.py.

set -eu

usage() {
  cat <<EOF
Användning: $(basename "$0") [flaggor] [-- build_user_words.py-flaggor...]

  --root DIR    projektrot ($PWD om ej satt via ROOT)
  -h, --help    visa denna hjälp; för build_user_words.py-flaggor: ./build_user_words.sh -- --help

Övriga flaggor (t.ex. --min-freq, --out, --text-dir) skickas vidare till build_user_words.py.
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

exec python build_user_words.py "${extra[@]}"
