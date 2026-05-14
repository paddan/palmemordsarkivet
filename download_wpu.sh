#!/bin/bash
# Ladda ner filer från wpu.nu som saknas i palmemordsarkivet.
#
# Användning:
#   ./download_wpu.sh                   # ladda ner saknade filer → downloaded/wpu_files/
#   ./download_wpu.sh --dry-run         # visa lista utan att ladda ner
#   ./download_wpu.sh --da-only         # bara filer med DA-nummer
#   ./download_wpu.sh --rebuild         # ladda ner igen även om filen finns
#   ./download_wpu.sh -- --help         # full src/download_wpu.py-hjälp

set -eu

ROOT=${ROOT:-$(cd "$(dirname "$0")" && pwd)}
extra=()

while [ $# -gt 0 ]; do
  case "$1" in
    --root)    ROOT="$2"; shift 2 ;;
    -h|--help)
      echo "Användning: $(basename "$0") [--dry-run] [--id-only] [--rebuild] [--out DIR]"
      echo "Kör ./download_wpu.sh -- --help för fullständig hjälp."
      exit 0 ;;
    --) shift; extra+=("$@"); break ;;
    *)  extra+=("$1"); shift ;;
  esac
done

cd "$ROOT"

# shellcheck disable=SC1091
source .venv/bin/activate

exec python src/download_wpu.py ${extra[@]+"${extra[@]}"}
