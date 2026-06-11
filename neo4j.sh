#!/bin/bash
# Starta/stoppa Neo4j för kunskapsgrafen via podman.
# Lösenordet genereras första gången och sparas i neo4j/.password —
# load_graph.sh läser det automatiskt därifrån.
#
# Användning:
#   ./neo4j.sh           # starta (skapar container + lösenord vid behov)
#   ./neo4j.sh stop
#   ./neo4j.sh status
set -eu

ROOT=$(cd "$(dirname "$0")" && pwd)
NEO4J_DIR="$ROOT/neo4j"
PASSWORD_FILE="$NEO4J_DIR/.password"
CONTAINER=palme-neo4j

cmd=${1:-start}

case "$cmd" in
  stop)
    podman stop "$CONTAINER" >/dev/null && echo "Neo4j stoppad."
    exit 0 ;;
  status)
    podman ps --filter "name=$CONTAINER" --format "{{.Names}}: {{.Status}}" | grep . \
      || echo "$CONTAINER: kör inte"
    exit 0 ;;
  start) ;;
  *) echo "okänt kommando: $cmd (start|stop|status)" >&2; exit 2 ;;
esac

# Starta podman-VM:en om den inte kör
if ! podman info >/dev/null 2>&1; then
  echo "Startar podman-maskinen…"
  podman machine start
fi

# Generera och spara lösenord första gången
if [ ! -f "$PASSWORD_FILE" ]; then
  mkdir -p "$NEO4J_DIR"
  openssl rand -hex 16 > "$PASSWORD_FILE"
  chmod 600 "$PASSWORD_FILE"
  echo "Genererade nytt lösenord → $PASSWORD_FILE"
fi
PASSWORD=$(cat "$PASSWORD_FILE")

if podman ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  echo "Neo4j kör redan."
elif podman ps -a --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  echo "Startar befintlig container…"
  podman start "$CONTAINER" >/dev/null
else
  echo "Skapar container $CONTAINER (hämtar neo4j:5 första gången)…"
  mkdir -p "$NEO4J_DIR/data"  # podman kräver att volymkatalogen finns
  podman run -d --name "$CONTAINER" --restart unless-stopped \
    -p 7474:7474 -p 7687:7687 \
    -e NEO4J_AUTH="neo4j/$PASSWORD" \
    -e NEO4J_server_memory_heap_max__size=2G \
    -v "$NEO4J_DIR/data:/data" \
    neo4j:5 >/dev/null
fi

# Vänta på att HTTP-porten svarar (max ~90 s)
printf "Väntar på Neo4j"
for _ in $(seq 1 45); do
  if curl -sf http://localhost:7474 >/dev/null 2>&1; then
    echo " — klar."
    echo "Browser:   http://localhost:7474  (användare: neo4j)"
    echo "Lösenord:  $PASSWORD"
    echo "Ladda grafen:  ./load_graph.sh"
    exit 0
  fi
  printf "."
  sleep 2
done
echo
echo "Neo4j svarade inte inom 90 s — kolla loggen: podman logs $CONTAINER" >&2
exit 1
