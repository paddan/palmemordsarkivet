"""Tester för mcp_server.get_page — sidhämtning och sökvägsvalidering."""

from __future__ import annotations

from pathlib import Path

import pytest

import mcp_server


@pytest.fixture()
def text_dir(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "text"
    d.mkdir()
    monkeypatch.setattr(mcp_server, "_text_dir", d)
    return d


def test_get_page_returns_requested_page(text_dir: Path) -> None:
    (text_dir / "doc.txt").write_text("sida ett\fsida två\fsida tre",
                                      encoding="utf-8")
    out = mcp_server.get_page("doc.txt", 2)
    assert "sida två" in out
    assert "sida ett" not in out
    assert "[doc.txt, sida 2]" in out


def test_get_page_out_of_range(text_dir: Path) -> None:
    (text_dir / "doc.txt").write_text("bara en sida", encoding="utf-8")
    out = mcp_server.get_page("doc.txt", 5)
    assert "finns inte" in out
    assert "har 1 sidor" in out


def test_get_page_missing_file(text_dir: Path) -> None:
    out = mcp_server.get_page("saknas.txt", 1)
    assert "Hittade inte" in out


def test_get_page_empty_page(text_dir: Path) -> None:
    (text_dir / "doc.txt").write_text("text\f\ftext", encoding="utf-8")
    out = mcp_server.get_page("doc.txt", 2)
    assert "är tom" in out


def test_get_page_rejects_path_traversal(text_dir: Path) -> None:
    secret = text_dir.parent / "hemlig.txt"
    secret.write_text("hemligt", encoding="utf-8")
    out = mcp_server.get_page("../hemlig.txt", 1)
    assert "hemligt" not in out
