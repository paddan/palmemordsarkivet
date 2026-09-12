#!/bin/sh
# Tunn genväg till scripts/graph_sync.py — vidarebefordrar alla argument.
# Ingen egen logik här: flaggor och defaults bor i Python-sidan.

set -eu

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON="$PROJECT_ROOT/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON=python3

exec "$PYTHON" "$PROJECT_ROOT/scripts/graph_sync.py" "$@"
