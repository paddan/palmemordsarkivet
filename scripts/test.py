"""Kör projektets verifieringar (pytest + valfritt ruff/mypy)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _venv_tool(name: str) -> str | None:
    exe = ROOT / ".venv" / "bin" / name
    return str(exe) if exe.exists() else None


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    run_static = False
    for arg in args:
        if arg == "--static":
            run_static = True
        elif arg in ("-h", "--help"):
            print("Användning: scripts/test.py [--static]")
            print("  --static   kör även ruff check . och mypy src via .venv")
            return 0
        else:
            print(f"okänd flagga: {arg}", file=sys.stderr)
            return 2

    python = _venv_tool("python") or "python3"

    print("→ pytest")
    rc = subprocess.run([python, "-m", "pytest", "tests/"], cwd=ROOT).returncode
    if rc != 0:
        return rc

    if not run_static:
        print("↷ statiska kontroller hoppas över (kör scripts/test.py --static)")
        return 0

    for tool in ("ruff", "mypy"):
        exe = _venv_tool(tool)
        if exe is None:
            print(f"✗ {tool} saknas (installera dev-extra)", file=sys.stderr)
            return 1
        print(f"→ {tool}")
        # --explicit-package-bases: annars hittar mypy samma fil två gånger
        # ("ingest" och "rag.ingest") och avbryter med module mapping-fel.
        args_ = ["check", "."] if tool == "ruff" else ["--explicit-package-bases", "src"]
        rc = subprocess.run([exe, *args_], cwd=ROOT).returncode
        if rc != 0:
            return rc
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
