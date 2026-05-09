#!/bin/bash
# Slå samman wpu.nu-text med palmemordsarkivet-text (välj bäst kvalitet).
#
# Kräver att files_wpu/ är nedladdat (./download_wpu.sh) och att
# text/ finns (./ocr_tesseract.sh eller ./ocr.sh).
#
# Användning:
#   ./merge_wpu.sh                   # kör merging
#   ./merge_wpu.sh --dry-run         # visa vad som skulle hända
#   ./merge_wpu.sh --rebuild         # kör om alla, ignorera .done-marker
#   ./merge_wpu.sh -- --help         # full hjälp från merge_wpu.py

set -eu

ROOT=${ROOT:-$(cd "$(dirname "$0")" && pwd)}
extra=()

while [ $# -gt 0 ]; do
  case "$1" in
    --root)    ROOT="$2"; shift 2 ;;
    -h|--help)
      echo "Användning: $(basename "$0") [--dry-run] [--rebuild] [--margin N]"
      echo "Kör ./merge_wpu.sh -- --help för fullständig hjälp."
      exit 0 ;;
    --) shift; extra+=("$@"); break ;;
    *)  extra+=("$1"); shift ;;
  esac
done

cd "$ROOT"

# shellcheck disable=SC1091
source .venv/bin/activate

exec python src/merge_wpu.py ${extra[@]+"${extra[@]}"}
