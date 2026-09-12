"""Gemensam uppstart för manuellt körbara scripts."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def _prioritize_source_tree() -> None:
    """Se till att projektets källträd används före installerade paket."""
    source = str(SRC)
    if source in sys.path:
        sys.path.remove(source)
    sys.path.insert(0, source)


def run(operation_id: str, argv: list[str] | None = None) -> int:
    """Kör en registrerad operation med argument från den aktuella terminalen.

    ``argv`` är till för entrypoints som har ett eget positionsargument före
    operationens flaggor (t.ex. ``neo4j.py start``); utan det läses
    ``sys.argv[1:]``.
    """
    # Gamla shell-wrappers cd:ade till repo-roten före körning — gör likadant så
    # att relativa sökvägar (t.ex. --out files) tolkas mot projektroten, inte cwd.
    os.chdir(ROOT)
    _prioritize_source_tree()
    from operations.cli import run_operation_cli

    return run_operation_cli(operation_id, sys.argv[1:] if argv is None else argv)
