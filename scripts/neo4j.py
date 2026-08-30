"""CLI för att starta/stoppa lokal Neo4j via podman."""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC in sys.path:
    sys.path.remove(_SRC)
sys.path.insert(0, _SRC)

from operations.neo4j import neo4j_start, neo4j_status, neo4j_stop  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else "start"

    if cmd in ("-h", "--help"):
        print("Användning: scripts/neo4j.py [start|stop|status]")
        return 0
    if cmd == "start":
        return neo4j_start()
    if cmd == "stop":
        return neo4j_stop()
    if cmd == "status":
        return neo4j_status()
    print(f"okänt kommando: {cmd} (start|stop|status)", file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
