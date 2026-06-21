#!/bin/bash
# Extrahera granskningsbara kartobservations-kandidater ur OCR-texten.
# Flaggor vidarebefordras till src/extract_map_observations.py.
set -eu
ROOT=$(cd "$(dirname "$0")" && pwd)
cd "$ROOT"
source .venv/bin/activate
exec python -m extract_map_observations "$@"
