#!/bin/bash
# Kör projektets verifieringar lokalt. Pytest körs alltid; ruff och mypy körs
# bara när de uttryckligen begärs med --static.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

RUN_STATIC=false
for arg in "$@"; do
  case "$arg" in
    --static) RUN_STATIC=true ;;
    -h|--help)
      echo "Användning: $(basename "$0") [--static]"
      echo "  --static   kör även ruff check . och mypy src via .venv"
      exit 0 ;;
    *) echo "okänd flagga: $arg" >&2; exit 2 ;;
  esac
done

if [[ -x ".venv/bin/python" ]]; then
  PYTHON="${PYTHON:-.venv/bin/python}"
else
  PYTHON="${PYTHON:-python3}"
fi

echo "→ pytest"
"$PYTHON" -m pytest tests/

if [[ "$RUN_STATIC" != "true" ]]; then
  echo "↷ statiska kontroller hoppas över (kör ./test.sh --static)"
  exit 0
fi

run_required() {
  local tool="$1"
  shift
  local exe=""
  if [[ -x ".venv/bin/$tool" ]]; then
    exe=".venv/bin/$tool"
  else
    echo "✗ $tool saknas (installera dev-extra: .venv/bin/pip install -e '.[dev,web]')" >&2
    return 1
  fi

  echo "→ $tool $*"
  "$exe" "$@"
}

run_required ruff check .
run_required mypy src
