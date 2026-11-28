"""Starta Streamlit-webgränssnittet.

Nycklar läses ur processmiljön (CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY) —
inget shell-script sourcar .zshrc.local längre (portabilitet/säkerhet).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def start_neo4j() -> bool:
    """Starta Neo4j (podman) så att Graf-sidan fungerar; webben startar oavsett utfall."""
    try:
        sys.path.insert(0, str(ROOT / "src"))
        from operations.neo4j import neo4j_start

        rc = neo4j_start(None)
        reason = f"kod {rc}"
    except Exception as exc:  # podman saknas, maskinen startar inte, tidsgräns …
        rc = 1
        reason = str(exc) or type(exc).__name__
    if rc == 0:
        return True
    print(
        f"Kunskapsgrafen startade inte ({reason}). Graf-sidan fungerar inte förrän "
        "den startas i Admin → Extraktion och graf.",
        file=sys.stderr,
    )
    return False


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if any(a in ("-h", "--help") for a in args):
        print("Användning: scripts/web.py [-- streamlit-flaggor...]")
        return 0

    streamlit = ROOT / ".venv" / "bin" / "streamlit"
    if not streamlit.exists():
        print(
            "streamlit saknas i .venv — kör ./install.sh (installerar webbextran).\n"
            "En .venv från `uv sync` eller `pip install -e .` saknar den.",
            file=sys.stderr,
        )
        return 1

    start_neo4j()
    cmd = [str(streamlit), "run", str(ROOT / "src" / "Utredning.py"), *args]
    os.execvpe(str(streamlit), cmd, os.environ)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
