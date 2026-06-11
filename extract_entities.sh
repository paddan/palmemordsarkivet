#!/bin/bash
# Entitets-/relationsextraktion för kunskapsgrafen (Claude Haiku).
# Flaggor vidarebefordras till src/graph/extract_entities.py (--limit, --dry-run, ...).
set -eu
ROOT=$(cd "$(dirname "$0")" && pwd)
cd "$ROOT"
source .venv/bin/activate
exec python -m graph.extract_entities "$@"
