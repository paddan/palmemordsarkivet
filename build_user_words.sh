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
Användning: $(basename "$0") [flaggor]

Wrapper-flaggor:
  --root DIR              projektrot ($PWD om ej satt via ROOT)
  -h, --help              visa denna hjälp

Skickas vidare till build_user_words.py (alla okända flaggor passerar igenom):
  --text-dir DIR          katalog med .txt-filer (default: text/)
  --user-words FILE       befintlig swe.user-words att slå ihop med
  --out FILE              output-fil (default: tessdata/swe.user-words.auto)
  --min-freq N            minsta frekvens; 0 = auto (10 med hunspell, annars 30)

För full build_user_words.py-hjälp: ./build_user_words.sh -- --help
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

exec python build_user_words.py ${extra[@]+"${extra[@]}"}
