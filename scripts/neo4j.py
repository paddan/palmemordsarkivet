"""CLI för att starta/stoppa lokal Neo4j via operationsregistret.

Går via registret (inte modulen direkt) så att CLI:n och adminsidans knappar
kör exakt samma kodväg — samma parametrar, samma loggning, samma avbrytning.
"""

from __future__ import annotations

import sys

from _bootstrap import run

# Positionsargumentet i skalet → operation-id i registret.
_OPERATIONS = {
    "start": "neo4j-start",
    "stop": "neo4j-stop",
    "status": "neo4j-status",
}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else "start"

    if cmd in ("-h", "--help"):
        print("Användning: scripts/neo4j.py [start|stop|status]")
        return 0

    operation_id = _OPERATIONS.get(cmd)
    if operation_id is None:
        print(f"okänt kommando: {cmd} (start|stop|status)", file=sys.stderr)
        return 2

    # Extra argument ignoreras, precis som tidigare — operationerna är parameterlösa.
    return run(operation_id, [])


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
