#!/bin/sh
# Tunn genväg till scripts/download_wpu.py — vidarebefordrar alla argument.
# Ingen egen logik här: flaggor och defaults bor i Python-sidan.

set -eu

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON="$PROJECT_ROOT/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON=python3

exec "$PYTHON" "$PROJECT_ROOT/scripts/download_wpu.py" "$@"
