#!/bin/bash
# Slå ihop per-sida-text (text_pages/<stem>/page-*.txt) in i text/<stem>.txt.
#
# Användning:
#   ./merge_pages.sh --all                 # alla befintliga text_pages/-mappar
#   ./merge_pages.sh --stem "1 — PM ..."   # en specifik fil
#   ./merge_pages.sh --help                # full hjälp

set -eu

ROOT=${ROOT:-$(cd "$(dirname "$0")" && pwd)}
cd "$ROOT"

# shellcheck disable=SC1091
source .venv/bin/activate

exec python src/merge_pages.py "$@"
