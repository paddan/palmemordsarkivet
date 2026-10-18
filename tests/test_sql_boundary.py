"""Skyddstest: SQL ska bara skrivas i paketet src/db/.

Konsumentmoduler anropar namngivna funktioner i ``db`` i stället för att köra
``conn.execute(...)`` själva. Testet söker igenom ``src/**/*.py`` och misslyckas
med en lista över avvikande anrop.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
DB_PACKAGE = SRC / "db"

# Mönster för SQL-anrop mot en anslutning.
SQL_CALL = re.compile(r"\.(execute|executemany|executescript)\(")

# Uttryckliga, kommenterade undantag: sökvägen relativt src/ och en motivering.
# Tom just nu — inga legitima SQLite-anrop utanför db.py. LanceDB-anrop
# (t.ex. ``table.delete``) matchar inte mönstret.
ALLOWED: dict[str, str] = {}


def _violations() -> list[str]:
    found: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if path.is_relative_to(DB_PACKAGE):
            continue
        rel = path.relative_to(SRC).as_posix()
        if rel in ALLOWED:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if SQL_CALL.search(line):
                found.append(f"{rel}:{lineno}: {line.strip()}")
    return found


def test_sql_only_in_db_module():
    violations = _violations()
    assert not violations, (
        "SQL-anrop utanför src/db/ (flytta till en namngiven funktion i db-paketet):\n"
        + "\n".join(violations)
    )
