#!/bin/bash
# Visa/ändra LLM-konfigurationen (generated/llm_config.json) utan webgränssnittet.
set -eu
ROOT=$(cd "$(dirname "$0")" && pwd)
cd "$ROOT"
source .venv/bin/activate
exec python -m llm_config_cli "$@"
