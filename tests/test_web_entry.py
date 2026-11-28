"""scripts/web.py: Neo4j startas före Streamlit, men aldrig blockerande."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("palme_web", ROOT / "scripts" / "web.py")
assert _spec and _spec.loader
web = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(web)


@pytest.fixture
def fake_streamlit(monkeypatch, tmp_path):
    exe = tmp_path / ".venv" / "bin" / "streamlit"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.setattr(web, "ROOT", tmp_path)


def _patch_neo4j(monkeypatch, fn):
    import operations.neo4j as op

    monkeypatch.setattr(op, "neo4j_start", fn)


def test_web_starts_streamlit_even_if_neo4j_fails(monkeypatch, capsys, fake_streamlit):
    def boom(_ctx=None):
        raise RuntimeError("podman saknas")

    _patch_neo4j(monkeypatch, boom)
    calls: list[str] = []
    monkeypatch.setattr(web.os, "execvpe", lambda f, cmd, env: calls.append(cmd[0]))
    assert web.main([]) == 0
    assert calls and calls[0].endswith("streamlit")
    assert "Kunskapsgrafen startade inte (podman saknas)" in capsys.readouterr().err


def test_web_help_does_not_start_neo4j(monkeypatch, capsys):
    def never(_ctx=None):
        raise AssertionError("neo4j får inte startas av --help")

    _patch_neo4j(monkeypatch, never)
    assert web.main(["--help"]) == 0
    assert "Användning: scripts/web.py" in capsys.readouterr().out


def test_web_starts_neo4j_before_streamlit(monkeypatch, fake_streamlit):
    order: list[str] = []
    _patch_neo4j(monkeypatch, lambda _ctx=None: order.append("neo4j") or 0)
    monkeypatch.setattr(web.os, "execvpe", lambda f, cmd, env: order.append("streamlit"))
    web.main([])
    assert order == ["neo4j", "streamlit"]


def test_start_neo4j_reports_nonzero_rc(monkeypatch, capsys):
    _patch_neo4j(monkeypatch, lambda _ctx=None: 2)
    assert web.start_neo4j() is False
    assert "Kunskapsgrafen startade inte" in capsys.readouterr().err
