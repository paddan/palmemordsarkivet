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


def test_project_root_only_has_web_shell_shortcut() -> None:
    """Produktionsflöden ska sakna shell-wrappers utöver webgenvägen."""
    root = Path(__file__).resolve().parents[1]

    assert sorted(path.name for path in root.glob("*.sh")) == ["web.sh"]
