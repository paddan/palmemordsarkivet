"""Starta Streamlit-webgränssnittet.

Nycklar läses ur processmiljön (CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY) —
inget shell-script sourcar .zshrc.local längre (portabilitet/säkerhet).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if any(a in ("-h", "--help") for a in args):
        print("Användning: scripts/web.py [-- streamlit-flaggor...]")
        return 0

    streamlit = ROOT / ".venv" / "bin" / "streamlit"
    if not streamlit.exists():
        print("streamlit saknas i .venv — kör scripts/install.py", file=sys.stderr)
        return 1

    cmd = [str(streamlit), "run", str(ROOT / "src" / "Utredning.py"), *args]
    os.execvpe(str(streamlit), cmd, os.environ)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
