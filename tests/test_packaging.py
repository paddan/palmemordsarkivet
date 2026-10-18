"""Paketeringsmetadata ska spegla faktiska moduler."""

from __future__ import annotations

import tomllib
from pathlib import Path


def test_pyproject_does_not_reference_removed_migration_or_surya_modules() -> None:
    root = Path(__file__).resolve().parents[1]
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    py_modules = set(data["tool"]["setuptools"]["py-modules"])

    assert "migrate_to_db" not in py_modules
    assert "ocr_surya" not in py_modules


def test_pyproject_includes_graph_package() -> None:
    root = Path(__file__).resolve().parents[1]
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    include = set(data["tool"]["setuptools"]["packages"]["find"]["include"])

    assert "rag*" in include
    assert "graph*" in include


def test_pyproject_includes_operations_package() -> None:
    root = Path(__file__).resolve().parents[1]
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    include = set(data["tool"]["setuptools"]["packages"]["find"]["include"])

    assert "operations*" in include


def test_pyproject_includes_karta_module() -> None:
    root = Path(__file__).resolve().parents[1]
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    py_modules = set(data["tool"]["setuptools"]["py-modules"])

    assert "karta" in py_modules


def test_py_modules_matches_toplevel_src_modules() -> None:
    """Varje modul på toppnivå i src/ ska finnas i py-modules, och tvärtom."""
    root = Path(__file__).resolve().parents[1]
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    py_modules = set(data["tool"]["setuptools"]["py-modules"])
    src_modules = {path.stem for path in (root / "src").glob("*.py")}

    saknas = sorted(src_modules - py_modules)
    overflodiga = sorted(py_modules - src_modules)
    assert not saknas and not overflodiga, (
        "[tool.setuptools] py-modules stämmer inte med src/*.py på toppnivå.\n"
        f"  Saknas i py-modules (finns i src/): {saknas}\n"
        f"  Överflödiga i py-modules (finns inte i src/): {overflodiga}"
    )


def test_pyproject_includes_db_package() -> None:
    """db är ett paket (src/db/), inte en py-module, så det ska ingå i packages.include."""
    root = Path(__file__).resolve().parents[1]
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    py_modules = set(data["tool"]["setuptools"]["py-modules"])
    include = set(data["tool"]["setuptools"]["packages"]["find"]["include"])

    assert "db" not in py_modules
    assert "db*" in include
    assert (root / "src" / "db" / "__init__.py").is_file()
