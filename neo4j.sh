#!/bin/sh
# Tunn genväg till det portabla Python-entrypointet.

set -eu

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$PROJECT_ROOT/.venv/bin/python" "$PROJECT_ROOT/scripts/neo4j.py" "$@"
