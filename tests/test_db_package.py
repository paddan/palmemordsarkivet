"""Kontrakt för paketet ``db`` (src/db/).

Alla anropsställen använder ``db.<namn>`` (eller ett alias för modulen). Testet
läser src/ och scripts/ med ast och kontrollerar att varje sådant attribut
faktiskt finns på modulen, så att en uppdelning eller borttagen re-export ger
ett tydligt fel här i stället för ett AttributeError i produktion.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import db  # noqa: E402

# Namn som används som modulalias för db i anropsställena.
ALIASES = {"db", "state_db", "_state_db", "_db"}

# Attribut på alias-namn som inte tillhör db-paketet. Varje undantag har en
# motivering: LanceDB-anslutningen heter också ``db`` i RAG-koden och har
# tabellmetoderna nedan.
NOT_DB_ATTRIBUTES: dict[str, str] = {
    "create_table": "LanceDB-anslutning (rag/ingest.py)",
    "drop_table": "LanceDB-anslutning (rag/ingest.py)",
    "list_tables": "LanceDB-anslutning (rag/ingest.py, rag/ask.py, rag/mcp_server.py, Utredning.py)",
    "open_table": "LanceDB-anslutning (rag/ask.py, rag/mcp_server.py, Utredning.py, pages/6_Jämförelse.py)",
}


def _db_attribute_uses() -> dict[str, list[str]]:
    """Returnera {attributnamn: [plats, ...]} för alla db-anrop i src/ och scripts/."""
    uses: dict[str, list[str]] = {}
    for base in (ROOT / "src", ROOT / "scripts"):
        for path in sorted(base.rglob("*.py")):
            if "egg-info" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id in ALIASES
                ):
                    rel = path.relative_to(ROOT).as_posix()
                    uses.setdefault(node.attr, []).append(f"{rel}:{node.lineno}")
    return uses


def test_every_db_attribute_used_by_callers_exists() -> None:
    missing = {
        name: places
        for name, places in _db_attribute_uses().items()
        if not hasattr(db, name) and name not in NOT_DB_ATTRIBUTES
    }
    assert not missing, "db.<namn> används men finns inte i paketet db:\n" + "\n".join(
        f"  {name}: {', '.join(places[:3])}" for name, places in sorted(missing.items())
    )


def test_db_package_reexports_original_toplevel_names() -> None:
    """Viktiga namn ska vara åtkomliga direkt på paketet, även privata och konstanter."""
    for name in (
        "connect",
        "init_schema",
        "SCHEMA_VERSION",
        "DEFAULT_DB",
        "ActiveAdminJobError",
        "_json_list",
        "_row_dict",
    ):
        assert hasattr(db, name), name


@pytest.mark.parametrize("module", ["_schema", "pipeline", "graph", "casebook", "karta", "jobs", "usage"])
def test_domain_modules_exist_in_package(module: str) -> None:
    assert (ROOT / "src" / "db" / f"{module}.py").is_file()


def test_db_package_has_no_legacy_module_file() -> None:
    assert not (ROOT / "src" / "db.py").exists()
