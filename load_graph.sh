#!/bin/bash
# Ladda extraherade entiteter/relationer från state.db till Neo4j.
# Kräver Neo4j igång (./neo4j.sh). Lösenord tas från NEO4J_PASSWORD i miljön,
# eller läses automatiskt från neo4j/.password (skapad av neo4j.sh).
set -eu
ROOT=$(cd "$(dirname "$0")" && pwd)
cd "$ROOT"
if [ -z "${NEO4J_PASSWORD:-}" ] && [ -f neo4j/.password ]; then
  NEO4J_PASSWORD=$(cat neo4j/.password)
  export NEO4J_PASSWORD
fi
source .venv/bin/activate
exec python -m graph.load_neo4j "$@"
